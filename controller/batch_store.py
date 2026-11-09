"""Normalized batch persistence. Legacy JSON tables are immutable migration input."""
import datetime as dt
import hashlib
import json
import os

VERSION = 'normalized_batches_v1'
TIMES = ('started_at', 'ended_at', 'phase_started_at', 'next_check')
COLLECTIONS = ('members', 'candidates', 'backup_confirmed', 'deferred', 'mapping_skipped', 'cleanup_results', 'phase_seconds')
SCALARS = ('id', 'phase', 'observations', 'removed_count', 'timing_partial') + TIMES


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decoded(value, default=None):
    return json.loads(value) if isinstance(value, (str, bytes)) else (value if value is not None else default)


def utc(value):
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).replace(tzinfo=None) if value is not None else None


def epoch(value):
    return value.replace(tzinfo=dt.timezone.utc).timestamp() if value is not None else None


def split_files(batch):
    files = {}
    def item(name):
        return files.setdefault(name, {})
    for name, value in batch.get('members', {}).items():
        item(name)['member'] = value
    for order, row in enumerate(batch.get('candidates', [])):
        item(row['source_path'])['candidate'] = {k:v for k,v in row.items() if k != 'source_path'}
        item(row['source_path'])['candidate_order'] = order
    for name, value in batch.get('backup_confirmed', {}).items():
        item(name)['confirmed'] = value
    for name in batch.get('deferred', []):
        item(name)['deferred'] = True
    for name, error in batch.get('mapping_skipped', {}).items():
        item(name)['mapping_error'] = error
    for order, result in enumerate(batch.get('cleanup_results', [])):
        item(result['file']).setdefault('cleanup', []).append([order, result])
    return files


class BatchStore:
    def __init__(self, db):
        self.db = db
        self.cached_id = None
        self.cached_files = {}
        self.cached_phases = {}

    def schema(self):
        self.db.execute('''CREATE TABLE IF NOT EXISTS backup_batches (
            id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
            phase VARCHAR(32) NOT NULL,
            started_at DATETIME(6), ended_at DATETIME(6), phase_started_at DATETIME(6), next_check DATETIME(6),
            observations INT NOT NULL DEFAULT 0, removed_count INT NOT NULL DEFAULT 0,
            timing_partial TINYINT NOT NULL DEFAULT 0,
            file_count INT NOT NULL, planned_files INT NOT NULL, backed_up INT NULL,
            deferred INT NOT NULL, mapping_skipped INT NOT NULL,
            release_action VARCHAR(40), summary JSON, extra JSON NOT NULL,
            updated_at DATETIME(6) NOT NULL,
            INDEX batches_updated(updated_at,id), INDEX batches_phase_updated(phase,updated_at,id)
        ) ENGINE=InnoDB''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS batch_phase_stats (
            batch_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
            phase VARCHAR(32) NOT NULL, seconds DOUBLE NOT NULL,
            PRIMARY KEY(batch_id,phase),
            FOREIGN KEY(batch_id) REFERENCES backup_batches(id) ON DELETE RESTRICT
        ) ENGINE=InnoDB''')
        self.db.execute('''CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(80) PRIMARY KEY, applied_at DATETIME(6) NOT NULL
        ) ENGINE=InnoDB''')
        columns = {r['Field'] for r in self.db.query('SHOW COLUMNS FROM controller_file_batches')}
        additions = {'state':'JSON NULL', 'is_member':'TINYINT NOT NULL DEFAULT 0',
                     'is_candidate':'TINYINT NOT NULL DEFAULT 0', 'is_confirmed':'TINYINT NOT NULL DEFAULT 0',
                     'is_deferred':'TINYINT NOT NULL DEFAULT 0'}
        missing = [f'ADD COLUMN `{name}` {definition}' for name,definition in additions.items() if name not in columns]
        if missing:
            self.db.execute('ALTER TABLE controller_file_batches ' + ','.join(missing))

    def migrate(self):
        # Serialize DDL and version checks; no side effects on source/target files.
        key = 'picsync_batches:' + hashlib.sha256(self.db.database.encode()).hexdigest()[:32]
        if not self.db.query('SELECT GET_LOCK(%s,30) acquired', (key,))[0]['acquired']:
            raise RuntimeError('Batch schema migration is already running')
        try:
            self.schema()
            if self.db.query('SELECT version FROM schema_migrations WHERE version=%s', (VERSION,)):
                return
            with self.db.transaction():
                if self.db.query("SHOW TABLES LIKE 'controller_rounds'"):
                    for row in self.db.query('SELECT id,data FROM controller_rounds'):
                        batch = decoded(row['data'])
                        if batch['id'] != row['id']:
                            raise ValueError('Legacy batch identity mismatch')
                        self._write(batch)
                self.db.execute('INSERT INTO schema_migrations VALUES (%s,%s)',
                                (VERSION, dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)))
        finally:
            self.cached_id = None
            self.db.query('SELECT RELEASE_LOCK(%s)', (key,))

    def _write(self, batch):
        files = split_files(batch)
        # All per-file facts must refer to frozen members/candidates, never add
        # ownership through a stray cleanup result or backup evidence.
        names = set(batch.get('members', {})) | {r['source_path'] for r in batch.get('candidates', [])}
        if set(files) - names:
            raise ValueError('Batch evidence references an unfrozen file')
        extra = {k:v for k,v in batch.items() if k not in COLLECTIONS + SCALARS + ('summary',)}
        extra['_present'] = [k for k in COLLECTIONS + SCALARS if k in batch]
        proof = batch.get('backup_confirmed')
        count = len(set(proof) - set(batch.get('deferred', []))) if proof is not None else None
        values = [batch['id'], batch['phase']] + [utc(batch.get(k)) for k in TIMES]
        values += [batch.get('observations',0), batch.get('removed_count',0), bool(batch.get('timing_partial')),
                   len(batch.get('members',{})), len(names), count, len(batch.get('deferred',[])),
                   len(batch.get('mapping_skipped',{})), batch.get('release',{}).get('action'),
                   dumps(batch['summary']) if 'summary' in batch else None, dumps(extra),
                   dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)]
        fields = ('id','phase') + TIMES + ('observations','removed_count','timing_partial','file_count','planned_files',
                 'backed_up','deferred','mapping_skipped','release_action','summary','extra','updated_at')
        self.db.execute('INSERT INTO backup_batches (' + ','.join(fields) + ') VALUES (' + ','.join(['%s']*len(fields))
                        + ') ON DUPLICATE KEY UPDATE ' + ','.join(f'{k}=VALUES({k})' for k in fields if k != 'id'), values)
        if self.cached_id == batch['id']:
            previous, phases = self.cached_files, self.cached_phases
        else:
            previous = {r['source_path']: decoded(r['state']) for r in self.db.query(
                'SELECT source_path,state FROM controller_file_batches WHERE batch_id=%s', (batch['id'],))}
            phases = {r['phase']:r['seconds'] for r in self.db.query(
                'SELECT phase,seconds FROM batch_phase_stats WHERE batch_id=%s', (batch['id'],))}
        if set(previous) - names:
            raise ValueError('Frozen batch ownership cannot shrink')
        self.db.claim_batch_paths(batch['id'], names - set(previous))
        changed = [(hashlib.sha256(os.fsencode(name)).hexdigest(), name, batch['id'], dumps(value),
                    int('member' in value), int('candidate' in value), int('confirmed' in value),
                    int(bool(value.get('deferred'))))
                   for name,value in files.items() if value != previous.get(name)]
        # INSERT executemany batches network writes; UPDATE executemany would
        # issue one round trip per file during initial migration/mapping.
        self.db.many('''INSERT INTO controller_file_batches
            (source_key,source_path,batch_id,state,is_member,is_candidate,is_confirmed,is_deferred)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON DUPLICATE KEY UPDATE state=VALUES(state),is_member=VALUES(is_member),
            is_candidate=VALUES(is_candidate),is_confirmed=VALUES(is_confirmed),is_deferred=VALUES(is_deferred)''', changed)
        durations = batch.get('phase_seconds', {})
        if set(phases) - set(durations):
            raise ValueError('Recorded phase timing cannot disappear')
        self.db.many('''INSERT INTO batch_phase_stats VALUES (%s,%s,%s)
            ON DUPLICATE KEY UPDATE seconds=VALUES(seconds)''',
            [(batch['id'],name,seconds) for name,seconds in durations.items() if seconds != phases.get(name)])
        return files, durations

    def save(self, batch):
        try:
            with self.db.transaction():
                files, phases = self._write(batch)
        except BaseException:
            self.cached_id = None
            raise
        # Cache serialized copies only after commit; callers mutate their dicts.
        self.cached_id = batch['id']
        self.cached_files = decoded(dumps(files))
        self.cached_phases = dict(phases)

    def load(self, batch_id):
        rows = self.db.query('SELECT * FROM backup_batches WHERE id=%s', (batch_id,))
        if not rows:
            return None
        row = rows[0]
        batch = decoded(row['extra'], {})
        present = set(batch.pop('_present', []))
        for key in SCALARS:
            if key in present or key in ('id','phase'):
                batch[key] = epoch(row[key]) if key in TIMES else row[key]
        if row['summary'] is not None:
            batch['summary'] = decoded(row['summary'])
        for key in COLLECTIONS:
            if key in present:
                batch[key] = [] if key in ('candidates','deferred','cleanup_results') else {}
        candidates, cleanup = [], []
        for item in self.db.query('SELECT source_path,state FROM controller_file_batches WHERE batch_id=%s', (batch_id,)):
            name, data = item['source_path'], decoded(item['state'])
            if data is None:
                raise ValueError('Incomplete batch file migration')
            for saved,key in (('member','members'),('confirmed','backup_confirmed'),('mapping_error','mapping_skipped')):
                if saved in data:
                    batch.setdefault(key,{})[name] = data[saved]
            if data.get('deferred'):
                batch.setdefault('deferred',[]).append(name)
            if 'candidate' in data:
                candidates.append((data['candidate_order'], dict(data['candidate'], source_path=name)))
            cleanup.extend(data.get('cleanup',[]))
        if 'candidates' in present:
            batch['candidates'] = [v for _,v in sorted(candidates)]
        if 'cleanup_results' in present:
            batch['cleanup_results'] = [v for _,v in sorted(cleanup)]
        if 'deferred' in batch:
            batch['deferred'].sort()
        if 'phase_seconds' in present:
            batch['phase_seconds'] = {r['phase']:r['seconds'] for r in self.db.query(
                'SELECT phase,seconds FROM batch_phase_stats WHERE batch_id=%s', (batch_id,))}
        return batch

    def active(self):
        rows = self.db.query("SELECT id FROM backup_batches WHERE phase!='complete' ORDER BY updated_at,id LIMIT 1")
        return self.load(rows[0]['id']) if rows else None
