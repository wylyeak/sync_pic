"""MySQL inventory. Times are UTC; deleted_at is when deletion was observed."""
import datetime
import hashlib
import json
import logging
import os
import re
from contextlib import contextmanager

import pymysql
from target_guard import TargetDirectory

DatabaseError = pymysql.MySQLError


class HashCollisionError(DatabaseError):
    """A hash matched a different path; skip that item without changing it."""



def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def path_key(path):
    return hashlib.sha256(os.fsencode(path)).hexdigest()


def relative_path(path, root):
    return validate_relative(os.path.relpath(os.path.abspath(path), os.path.abspath(root)))


def validate_relative(path):
    if not path or os.path.isabs(path) or path == '.' or os.path.normpath(path) != path:
        raise ValueError('Expected a normalized relative file path: {!r}'.format(path))
    if path == '..' or path.startswith('..' + os.sep):
        raise ValueError('Path escapes the configured directory: {!r}'.format(path))
    return path


def file_path(root, relative):
    return os.path.join(root, validate_relative(relative))


class Inventory:
    def __init__(self, database, **connection):
        if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', database):
            raise ValueError('MySQL database name must contain only letters, digits or underscores')
        self.database, self.connection_options = database, dict(connection)
        self.conn = pymysql.connect(charset='utf8mb4', autocommit=True,
                                    cursorclass=pymysql.cursors.DictCursor,
                                    connect_timeout=10, read_timeout=60, write_timeout=60,
                                    **connection)
        try:
            self.execute('CREATE DATABASE IF NOT EXISTS `{}` CHARACTER SET utf8mb4 COLLATE utf8mb4_bin'.format(database))
            self.conn.select_db(database)
            self.execute('''CREATE TABLE IF NOT EXISTS files (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                source_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, source_path TEXT NOT NULL,
                root_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, source_root TEXT NOT NULL,
                target_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, target_path TEXT NOT NULL,
                size_bytes BIGINT UNSIGNED, source_present TINYINT NOT NULL DEFAULT 1,
                first_seen_at VARCHAR(40) NOT NULL, last_seen_at VARCHAR(40),
                status ENUM('pending','linked','deleted') NOT NULL DEFAULT 'pending',
                linked_at VARCHAR(40), deleted_at VARCHAR(40), last_error TEXT,
                mtime_ns BIGINT, source_dev BIGINT UNSIGNED, source_ino BIGINT UNSIGNED,
                UNIQUE INDEX files_source_key(source_key),
                INDEX files_pending_order(root_key,status,source_present,mtime_ns,source_key),
                INDEX files_target(target_key)
            ) ENGINE=InnoDB''')
            self.execute('''CREATE TABLE IF NOT EXISTS controller_file_batches (
                source_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
                source_path TEXT NOT NULL,
                batch_id VARCHAR(32) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                INDEX controller_files_batch(batch_id)
            ) ENGINE=InnoDB''')
            # Additive migration: old deleted rows remain unknown, never guessed.
            deletion_columns = {
                'deletion_origin': "VARCHAR(20) NOT NULL DEFAULT 'unknown'",
                'deletion_reason': 'TEXT NULL',
                'backup_checked_at': 'VARCHAR(40) NULL',
                'backup_state': "VARCHAR(20) NOT NULL DEFAULT 'unknown'",
                'backup_evidence': 'JSON NULL',
                'deletion_checked_at': 'VARCHAR(40) NULL',
            }
            for attempt in range(3):
                existing_columns = {row['Field'] for row in self.query('SHOW COLUMNS FROM files')}
                missing = [(name, definition) for name, definition in deletion_columns.items()
                           if name not in existing_columns]
                if not missing:
                    break
                try:
                    # One rebuild at most on servers without instant ADD COLUMN.
                    ddl = pymysql.connect(charset='utf8mb4', autocommit=True, database=self.database,
                        connect_timeout=10, read_timeout=300, write_timeout=60, **self.connection_options)
                    try:
                        with ddl.cursor() as cursor:
                            cursor.execute('ALTER TABLE files ' + ','.join(
                                'ADD COLUMN `{}` {}'.format(name, definition) for name, definition in missing))
                    finally:
                        ddl.close()
                    break
                except pymysql.err.OperationalError as exc:
                    if exc.args[0] != 1060 or attempt == 2:
                        raise
            self.execute('''CREATE TABLE IF NOT EXISTS deletion_events (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                source_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
                source_path TEXT NOT NULL, target_path TEXT NOT NULL,
                origin VARCHAR(20) NOT NULL, state VARCHAR(20) NOT NULL,
                reason TEXT, evidence JSON, created_at VARCHAR(40) NOT NULL,
                finished_at VARCHAR(40), error_message TEXT,
                INDEX events_source(source_key), INDEX events_state(state)
            ) ENGINE=InnoDB''')
            self.execute("""CREATE TABLE IF NOT EXISTS hash_conflicts (
                id BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
                conflict_type VARCHAR(32) NOT NULL, operation VARCHAR(32) NOT NULL,
                hash_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, existing_path TEXT NOT NULL,
                incoming_path TEXT NOT NULL, detected_at VARCHAR(40) NOT NULL,
                details JSON, INDEX conflict_hash(hash_key)
            ) ENGINE=InnoDB""")
            self.migrate_hash_columns()
            self.ensure_file_id()
            self.execute('''CREATE OR REPLACE VIEW pending_files AS SELECT * FROM files
                WHERE status='pending' AND source_present=1''')
            self.execute('''CREATE TEMPORARY TABLE source_scan (
                source_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY, source_path TEXT NOT NULL,
                target_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin, target_path TEXT, size_bytes BIGINT UNSIGNED,
                mtime_ns BIGINT, source_dev BIGINT UNSIGNED, source_ino BIGINT UNSIGNED
            ) ENGINE=InnoDB''')
            self.execute('CREATE TEMPORARY TABLE target_conflicts (hash_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY) ENGINE=InnoDB')
            self.execute('''CREATE TEMPORARY TABLE target_scan (
                target_key CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, target_path TEXT NOT NULL,
                size_bytes BIGINT UNSIGNED,
                dev BIGINT UNSIGNED, ino BIGINT UNSIGNED, INDEX(target_key)
            ) ENGINE=InnoDB''')
        except BaseException:
            self.conn.close()
            raise

    def execute(self, sql, params=()):
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.rowcount

    def query(self, sql, params=()):
        with self.conn.cursor() as cursor:
            cursor.execute(sql, params)
            return cursor.fetchall()

    def many(self, sql, rows):
        if rows:
            with self.conn.cursor() as cursor:
                cursor.executemany(sql, rows)

    @contextmanager
    def transaction(self):
        self.conn.begin()
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def migrate_hash_columns(self):
        """Losslessly convert old SHA-256 bytes to lowercase hex, keeping indexes.

        Stop older writers before upgrading. Shadow columns retain the originals
        until the final ALTER; interrupted preparation can safely run again.
        """
        lock = 'file_sync_schema:' + hashlib.sha256(self.database.encode()).hexdigest()[:32]
        if not self.query('SELECT GET_LOCK(%s,10) AS acquired', (lock,))[0]['acquired']:
            raise DatabaseError('Another connection is upgrading the file_sync schema')
        try:
            for table, keys in (('files', ('source_key', 'root_key', 'target_key')),
                                ('hash_conflicts', ('hash_key',))):
                columns = {row['Field']: row for row in self.query('SHOW COLUMNS FROM `{}`'.format(table))}
                legacy = [key for key in keys if columns[key]['Type'].lower() == 'binary(32)']
                if not legacy:
                    continue
                indexes = {}
                for row in self.query('SHOW INDEX FROM `{}`'.format(table)):
                    indexes.setdefault(row['Key_name'], []).append(row)
                affected = {name: sorted(rows, key=lambda row: row['Seq_in_index'])
                            for name, rows in indexes.items()
                            if any(row['Column_name'] in legacy for row in rows)}
                for key in legacy:
                    shadow = key + '_hex'
                    if shadow not in columns:
                        self.execute('ALTER TABLE `{}` ADD COLUMN `{}` CHAR(64) '
                                     'CHARACTER SET ascii COLLATE ascii_bin NULL'.format(table, shadow))
                    self.execute('UPDATE `{}` SET `{}`=LOWER(HEX(`{}`))'.format(table, shadow, key))
                clauses = [('DROP PRIMARY KEY' if name == 'PRIMARY' else 'DROP INDEX `{}`'.format(name))
                           for name in affected]
                for key in legacy:
                    nullable = 'NULL' if columns[key]['Null'] == 'YES' else 'NOT NULL'
                    clauses.extend(('DROP COLUMN `{}`'.format(key),
                                    'CHANGE COLUMN `{0}_hex` `{0}` CHAR(64) CHARACTER SET ascii '
                                    'COLLATE ascii_bin {1}'.format(key, nullable)))
                for name, rows in affected.items():
                    parts = []
                    for row in rows:
                        part = '`{}`'.format(row['Column_name'])
                        if row['Sub_part'] is not None:
                            part += '({})'.format(row['Sub_part'])
                        if row['Collation'] == 'D':
                            part += ' DESC'
                        parts.append(part)
                    kind = 'PRIMARY KEY' if name == 'PRIMARY' else (
                        ('UNIQUE INDEX ' if rows[0]['Non_unique'] == 0 else 'INDEX ') + '`' + name + '`')
                    clauses.append('ADD {} ({})'.format(kind, ','.join(parts)))
                self.execute('ALTER TABLE `{}` '.format(table) + ', '.join(clauses))
                logging.getLogger(__name__).info('Converted %s hash columns to readable hex: %s', table, ', '.join(legacy))
        finally:
            self.query('SELECT RELEASE_LOCK(%s)', (lock,))

    def ensure_file_id(self):
        lock = 'file_sync_schema:' + hashlib.sha256(self.database.encode()).hexdigest()[:32]
        if not self.query('SELECT GET_LOCK(%s,10) AS acquired', (lock,))[0]['acquired']:
            raise DatabaseError('Another connection is upgrading the file_sync schema')
        try:
            if not self.query("SHOW COLUMNS FROM files LIKE 'id'"):
                self.execute("""ALTER TABLE files DROP PRIMARY KEY,
                    ADD COLUMN id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT FIRST,
                    ADD PRIMARY KEY(id), ADD UNIQUE INDEX files_source_key(source_key)""")
        finally:
            self.query('SELECT RELEASE_LOCK(%s)', (lock,))

    def collision(self, kind, key, existing, incoming, operation, details=None):
        # An independent autocommit connection preserves the audit even if the
        # caller rolls back its current file transaction.
        try:
            audit = pymysql.connect(database=self.database, charset='utf8mb4',
                                    autocommit=True, connect_timeout=10,
                                    read_timeout=60, write_timeout=60, **self.connection_options)
            try:
                with audit.cursor() as cursor:
                    cursor.execute("""INSERT INTO hash_conflicts
                        (conflict_type,operation,hash_key,existing_path,incoming_path,detected_at,details)
                        VALUES (%s,%s,%s,%s,%s,%s,%s)""",
                        (kind, operation, key, existing, incoming, now(),
                         json.dumps(details, ensure_ascii=False) if details is not None else None))
            finally:
                audit.close()
        except DatabaseError:
            logging.getLogger(__name__).exception('Could not persist hash conflict')
        message = '{} hash conflict: {!r} and {!r}; skipping'.format(kind, existing, incoming)
        logging.getLogger(__name__).warning(message)
        return HashCollisionError(message)

    def migrate_relative(self, source, target):
        """Atomically convert existing absolute records; preserve status and timestamps.

        Stop older writers before calling this method. A database represents one
        source/target pair, even when the directories are later mounted elsewhere.
        """
        if not self.query("""SELECT 1 FROM files WHERE source_root<>'.'
                OR LEFT(source_path,1)='/' OR LEFT(target_path,1)='/' LIMIT 1"""):
            return 0
        source, target = os.path.abspath(source), os.path.abspath(target)
        with self.transaction():
            rows = self.query('SELECT * FROM files FOR UPDATE')
            seen_source, seen_target, accepted = {}, {}, []
            for row in rows:
                for column, root in (('source_path', source), ('target_path', target)):
                    path = row[column]
                    row[column] = relative_path(path, root) if os.path.isabs(path) else validate_relative(path)
                row['source_key'] = path_key(row['source_path'])
                row['target_key'] = path_key(row['target_path'])
                row['root_key'], row['source_root'] = path_key('.'), '.'
                if row['last_error']:
                    for root in (source, target):
                        row['last_error'] = row['last_error'].replace(root + os.sep, '').replace(root, '.')
                for column, paths in (('source_path', seen_source), ('target_path', seen_target)):
                    path, key = row[column], path_key(row[column])
                    if key in paths:
                        self.collision(column.split('_')[0], key, paths[key], path, 'migration',
                                       {k: v.hex() if isinstance(v, bytes) else v for k, v in row.items()})
                        break
                else:
                    seen_source[row['source_key']] = row['source_path']
                    seen_target[row['target_key']] = row['target_path']
                    accepted.append(row)
            if rows:
                columns = list(rows[0])
                sql = 'INSERT INTO files (' + ','.join('`' + c + '`' for c in columns) + ') VALUES ('
                sql += ','.join('%s' for _ in columns) + ')'
                self.execute('DELETE FROM files')
                self.many(sql, [tuple(row[c] for c in columns) for row in accepted])
        return len(accepted)

    def check_path(self, kind, path):
        key_column, path_column = {
            'source': ('source_key', 'source_path'),
            'root': ('root_key', 'source_root'),
            'target': ('target_key', 'target_path'),
        }[kind]
        rows = self.query('SELECT `{}` AS path FROM files WHERE `{}`=%s '
                          'AND BINARY `{}`<>BINARY %s LIMIT 1'.format(
                              path_column, key_column, path_column), (path_key(path), path))
        if rows:
            raise self.collision(kind, path_key(path), rows[0]['path'], path, 'path_check')

    def lock_source(self, path, details=False):
        validate_relative(path)
        rows = self.query('SELECT source_path,target_path,status,source_present,size_bytes FROM files WHERE source_key=%s FOR UPDATE',
                          (path_key(path),))
        if rows and rows[0]['source_path'] != path:
            raise self.collision('source', path_key(path), rows[0]['source_path'], path, 'source_lookup')
        return (rows[0] if rows else None) if details else bool(rows)

    def pending_summary(self):
        return self.query("""SELECT COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS size_bytes
            FROM pending_files""")[0]

    def scan_source(self, source, target, walk, progress=None, on_changes=None, progress_every=100, stop_event=None):
        source, target = os.path.abspath(source), os.path.abspath(target)
        self.execute('DELETE FROM source_scan')
        stats = {'files': 0, 'size_bytes': 0, 'existing': 0, 'new': 0, 'conflicts': 0,
                 'new_pending': 0, 'new_pending_bytes': 0}

        def store_file(row):
            stamp = now()
            with self.transaction():
                previous = self.lock_source(row[1], details=True)
                existing = int(previous is not None)
                self.check_path('target', row[3])
                self.execute('INSERT INTO source_scan VALUES (%s,%s,%s,%s,%s,%s,%s,%s)', row)
                if existing:
                    self.execute("""UPDATE files SET target_key=%s,target_path=%s,
                        size_bytes=%s,mtime_ns=%s,source_dev=%s,source_ino=%s,
                        root_key=%s,source_root=%s,source_present=1,last_seen_at=%s
                        WHERE source_key=%s AND BINARY source_path=BINARY %s""",
                        row[2:] + (path_key('.'), '.', stamp, row[0], row[1]))
                else:
                    # A concurrent insert causes a duplicate-key error, never an overwrite.
                    self.execute("""INSERT INTO files
                        (source_key,source_path,target_key,target_path,size_bytes,mtime_ns,
                         source_dev,source_ino,root_key,source_root,first_seen_at,last_seen_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        row + (path_key('.'), '.', stamp, stamp))
            stats['files'] += 1
            stats['size_bytes'] += row[4]
            stats['existing'] += existing
            stats['new'] += 1 - existing
            if not existing or (previous['status'] == 'pending' and not previous['source_present']):
                stats['new_pending'] += 1
                stats['new_pending_bytes'] += row[4]

        try:
            # Commit each file before logging; filesystem I/O holds no files locks.
            for root, _, names in walk(source):
                for name in names:
                    if stop_event is not None and stop_event.is_set():
                        raise InterruptedError('Source scan stopped')
                    path = os.path.abspath(os.path.join(root, name))
                    try:
                        stat = os.stat(path)
                    except FileNotFoundError:
                        continue
                    relative = relative_path(path, source)
                    try:
                        store_file((path_key(relative), relative, path_key(relative), relative, stat.st_size,
                                    stat.st_mtime_ns, stat.st_dev, stat.st_ino))
                    except HashCollisionError:
                        stats['files'] += 1
                        stats['size_bytes'] += stat.st_size
                        stats['conflicts'] += 1
                    if progress and stats['files'] % progress_every == 0:
                        progress(dict(stats), False)
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError('Source scan stopped')
            # Only a complete traversal can mark unseen source files as absent.
            with self.transaction():
                self.execute('''UPDATE files f LEFT JOIN source_scan s ON s.source_key=f.source_key
                    AND BINARY s.source_path=BINARY f.source_path
                    SET f.source_present=0 WHERE f.root_key=%s AND BINARY f.source_root=BINARY %s AND f.source_present=1
                    AND s.source_key IS NULL''', (path_key('.'), '.'))
            pending = self.pending_summary()
            stats['pending_files'], stats['pending_bytes'] = pending['files'], int(pending['size_bytes'])
            if progress:
                progress(dict(stats), True)
            return stats
        finally:
            # Include committed changes even when traversal fails partway through.
            if on_changes and stats['new_pending']:
                on_changes(dict(stats))

    def scan_target(self, source, target, walk, on_changes=None, deletion_classifier=None):
        source = os.path.abspath(source)
        self.execute('DELETE FROM target_scan')
        self.execute('DELETE FROM target_conflicts')
        size, batch = 0, []
        for root, _, names in walk(target):
            for name in names:
                path = os.path.abspath(os.path.join(root, name))
                try:
                    stat = os.stat(path)
                except FileNotFoundError:
                    continue
                size += stat.st_size
                relative = relative_path(path, target)
                batch.append((path_key(relative), relative, stat.st_size, stat.st_dev, stat.st_ino))
                if len(batch) >= 1000:
                    self.many('INSERT INTO target_scan VALUES (%s,%s,%s,%s,%s)', batch)
                    batch.clear()
        self.many('INSERT INTO target_scan VALUES (%s,%s,%s,%s,%s)', batch)
        duplicates = self.query('SELECT target_key FROM target_scan GROUP BY target_key HAVING COUNT(*)>1')
        for duplicate in duplicates:
            key = duplicate['target_key']
            paths = self.query('SELECT target_path FROM target_scan WHERE target_key=%s', (key,))
            for path in paths[1:]:
                self.collision('target', key, paths[0]['target_path'], path['target_path'], 'target_scan')
            self.execute('INSERT IGNORE INTO target_conflicts VALUES (%s)', (key,))
        conflicts = self.query("""SELECT f.target_key,f.target_path AS existing_path,
                t.target_path AS incoming_path FROM files f JOIN target_scan t
                ON t.target_key=f.target_key WHERE BINARY t.target_path<>BINARY f.target_path""")
        for conflict in conflicts:
            key = conflict['target_key']
            self.collision('target', key, conflict['existing_path'], conflict['incoming_path'], 'target_scan')
            self.execute('INSERT IGNORE INTO target_conflicts VALUES (%s)', (key,))
        with self.transaction():
            deleted = self.query("""SELECT COUNT(*) AS files,COALESCE(SUM(f.size_bytes),0) AS size_bytes
                FROM files f LEFT JOIN target_scan t ON t.target_key=f.target_key
                AND BINARY t.target_path=BINARY f.target_path
                WHERE f.root_key=%s AND BINARY f.source_root=BINARY %s
                AND f.status='linked' AND t.target_key IS NULL
                AND NOT EXISTS (SELECT 1 FROM target_conflicts c WHERE c.hash_key=f.target_key)""",
                (path_key('.'), '.'))[0]
            self.execute('''UPDATE files f LEFT JOIN target_scan t ON t.target_key=f.target_key
                AND BINARY t.target_path=BINARY f.target_path
                SET f.status='deleted',f.deleted_at=%s WHERE f.root_key=%s AND BINARY f.source_root=BINARY %s
                AND f.status='linked' AND t.target_key IS NULL
                AND NOT EXISTS (SELECT 1 FROM target_conflicts c WHERE c.hash_key=f.target_key)''',
                (now(), path_key('.'), '.'))
            self.execute('''UPDATE files f JOIN target_scan t ON t.target_key=f.target_key
                AND BINARY t.target_path=BINARY f.target_path
                SET f.status='linked',f.last_error=NULL WHERE f.root_key=%s AND BINARY f.source_root=BINARY %s
                AND f.source_present=1 AND f.status='pending'
                AND t.dev=f.source_dev AND t.ino=f.source_ino
                AND NOT EXISTS (SELECT 1 FROM target_conflicts c WHERE c.hash_key=f.target_key)''',
                (path_key('.'), '.'))
        if deletion_classifier:
            self.classify_deletions(target, deletion_classifier)
        if on_changes and deleted['files']:
            on_changes({'deleted_files': deleted['files'], 'deleted_bytes': int(deleted['size_bytes']),
                        'target_bytes': size})
        return size

    def batch_owners(self):
        return {r['source_path']: r['batch_id'] for r in
                self.query('SELECT source_path,batch_id FROM controller_file_batches')}

    def claim_batch_paths(self, batch_id, paths):
        # Caller transaction commits claims together with the persisted round.
        paths = sorted(set(paths))
        for path in paths:
            validate_relative(path)
        for offset in range(0, len(paths), 500):
            chunk = paths[offset:offset+500]
            self.many('''INSERT INTO controller_file_batches (source_key,source_path,batch_id) VALUES (%s,%s,%s)
                ON DUPLICATE KEY UPDATE source_key=source_key''',
                [(path_key(path), path, batch_id) for path in chunk])
            expected = {path_key(path): path for path in chunk}
            rows = self.query('SELECT source_key,source_path,batch_id FROM controller_file_batches WHERE source_key IN ('
                + ','.join(['%s'] * len(expected)) + ')', list(expected))
            for row in rows:
                if row['source_path'] != expected[row['source_key']]:
                    raise HashCollisionError('Batch file path hash collision')
                if row['batch_id'] != batch_id:
                    raise ValueError('File path already belongs to another batch')

    def pending(self, source, after=None, limit=500, unassigned_only=False):
        source = os.path.abspath(source)
        params, extra = [path_key('.'), '.'], ''
        if after is not None:
            extra = ' AND (mtime_ns > %s OR (mtime_ns=%s AND source_key>%s))'
            params.extend((after[0], after[0], path_key(after[1])))
        params.append(limit)
        if unassigned_only:
            extra += ''' AND NOT EXISTS (SELECT 1 FROM controller_file_batches b
                        WHERE b.source_key=files.source_key)'''
        return self.query('''SELECT * FROM files WHERE root_key=%s AND BINARY source_root=BINARY %s
            AND status='pending' AND source_present=1 AND mtime_ns IS NOT NULL
            AND NOT EXISTS (SELECT 1 FROM target_conflicts c WHERE c.hash_key=files.target_key)'''
            + extra + ' ORDER BY mtime_ns,source_key LIMIT %s', params)

    def list_files(self, keyword=None, status=None):
        conditions, params = [], []
        if keyword:
            conditions.append('LOCATE(%s,source_path)>0')
            params.append(keyword)
        if status:
            origins = {'manual_deleted': 'manual', 'synced_deleted': 'syncthing', 'deleted_unknown': 'unknown'}
            if status in origins:
                conditions.extend(["status='deleted'", 'deletion_origin=%s'])
                params.append(origins[status])
            else:
                conditions.append('status=%s')
                params.append(status)
            if status == 'pending':
                conditions.append('source_present=1')
        where = (' WHERE ' + ' AND '.join(conditions)) if conditions else ''
        return self.query('SELECT * FROM files' + where + ' ORDER BY mtime_ns,source_path', params)

    def classify_deletions(self, target, classifier, limit=100, relatives=None):
        """Only positive remote tombstone evidence can establish sync deletion."""
        extra, params = '', []
        if relatives is not None:
            if not relatives:
                return
            extra = ' AND source_key IN (' + ','.join(['%s'] * len(relatives)) + ')'
            params.extend(path_key(name) for name in relatives)
        rows = self.query("SELECT source_path,target_path FROM files WHERE status='deleted' "
            "AND deletion_origin='unknown'" + extra +
            " ORDER BY COALESCE(deletion_checked_at,''),deleted_at DESC LIMIT %s", params + [limit])
        for row in rows:
            if relatives is not None and row['source_path'] not in relatives:
                continue
            if os.path.lexists(file_path(target, row['target_path'])):
                continue
            try:
                evidence = classifier(row['target_path'])
            except Exception:
                # API outages must not prevent normal inventory scans.
                logging.getLogger(__name__).warning('Deletion source lookup unavailable')
                break
            self.execute('''UPDATE files SET deletion_checked_at=%s
                WHERE source_key=%s AND BINARY source_path=BINARY %s''',
                (now(), path_key(row['source_path']), row['source_path']))
            if not evidence:
                continue
            with self.transaction():
                self.lock_source(row['source_path'])
                changed = self.execute("""UPDATE files SET deletion_origin='syncthing',deletion_reason=%s
                    WHERE source_key=%s AND BINARY source_path=BINARY %s
                    AND status='deleted' AND deletion_origin='unknown'""",
                    ('remote_tombstone:' + json.dumps(evidence, ensure_ascii=False),
                     path_key(row['source_path']), row['source_path']))
                if changed:
                    self.execute('''INSERT INTO deletion_events
                        (source_key,source_path,target_path,origin,state,reason,evidence,created_at,finished_at)
                        VALUES (%s,%s,%s,'syncthing','deleted',%s,%s,%s,%s)''',
                        (path_key(row['source_path']), row['source_path'], row['target_path'],
                         'remote_tombstone', json.dumps(evidence), now(), now()))

    def record_backup_check(self, relative, evidence):
        relative = validate_relative(relative)
        with self.transaction():
            if not self.lock_source(relative):
                raise ValueError('Unknown source record')
            self.execute('''UPDATE files SET backup_state=%s,backup_checked_at=%s,backup_evidence=%s
                WHERE source_key=%s AND BINARY source_path=BINARY %s''',
                (evidence['state'], evidence['checked_at'], json.dumps(evidence), path_key(relative), relative))

    def delete_confirmed_target(self, source, target, relative, evidence, expected_stat,
                                reason='manual_after_backup'):
        with TargetDirectory(source, target) as guard:
            return self._delete_confirmed_target(source, target, relative, evidence, expected_stat, reason, guard)

    def _delete_confirmed_target(self, source, target, relative, evidence, expected_stat, reason, guard):
        """Journal intent BEFORE unlink; keep state deleted so it is not relinked.

        A filesystem/DB crash cannot be made atomic: pending intents remain visible
        for reconciliation, and retain manual provenance instead of being guessed
        as a remote deletion by a later scan.
        """
        relative = validate_relative(relative)
        if (evidence.get('state') != 'confirmed' or not evidence.get('sha256')
                or evidence.get('file') != relative or evidence.get('content_verified') is not True):
            raise ValueError('Explicit per-item backup confirmation is required')
        checked_at = datetime.datetime.fromisoformat(evidence['checked_at'])
        age = (datetime.datetime.now(datetime.timezone.utc) - checked_at).total_seconds()
        if not 0 <= age <= 120:
            raise ValueError('Backup evidence expired; inspect this file again')
        if reason == 'photos_retained':
            retention = evidence.get('retention', {})
            if (retention.get('state') != 'retained_after_release' or not retention.get('run_id')
                    or retention.get('release_action') not in ('freed', 'nothing_to_free')
                    or retention.get('file') != relative
                    or tuple(retention.get('before_fingerprint', ())) != tuple(expected_stat)):
                raise ValueError('photos_retained requires a completed release-and-sync run for this unchanged file')
        target_root = os.path.realpath(target)
        path = file_path(target_root, relative)
        if os.path.commonpath([target_root, os.path.realpath(os.path.dirname(path))]) != target_root:
            raise ValueError('Target path escapes root')
        import stat as stat_module
        current = guard.stat(relative)
        fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        if not stat_module.S_ISREG(current.st_mode) or fingerprint(current) != tuple(expected_stat):
            raise ValueError('Target changed after backup verification')
        source_path = file_path(source, relative)
        source_stat = os.stat(source_path)
        if os.path.realpath(source_path) == os.path.realpath(path):
            raise ValueError('Source and target paths are identical')
        with self.transaction():
            row = self.lock_source(relative, details=True)
            if not row or row['target_path'] != relative or row['status'] != 'linked':
                raise ValueError('Expected a linked inventory record for this exact target')
            self.execute('''INSERT INTO deletion_events
                (source_key,source_path,target_path,origin,state,reason,evidence,created_at)
                VALUES (%s,%s,%s,'manual','intent',%s,%s,%s)''',
                (path_key(relative), relative, relative, reason, json.dumps(evidence), now()))
            event_id = self.query('SELECT LAST_INSERT_ID() AS id')[0]['id']
            self.execute('''UPDATE files SET status='deleted',deleted_at=%s,
                deletion_origin='manual',deletion_reason=%s,backup_state='confirmed',backup_checked_at=%s,backup_evidence=%s
                WHERE source_key=%s AND BINARY source_path=BINARY %s''',
                (now(), reason, evidence['checked_at'], json.dumps(evidence), path_key(relative), relative))
        try:
            guard.unlink(relative, expected_stat)
            if fingerprint(os.stat(source_path)) != fingerprint(source_stat):
                raise RuntimeError('Source changed during cleanup; inspect deletion event')
        except BaseException as exc:
            self.execute("UPDATE deletion_events SET state='error',error_message=%s,finished_at=%s WHERE id=%s",
                         (type(exc).__name__, now(), event_id))
            # Restore the live status only if the original target still exists.
            # If unlink succeeded or replacement occurred, retain manual provenance.
            try:
                if fingerprint(guard.stat(relative)) == tuple(expected_stat):
                    self.execute("""UPDATE files SET status='linked',deleted_at=NULL,
                        deletion_origin='unknown',deletion_reason=NULL WHERE source_key=%s
                        AND BINARY source_path=BINARY %s AND deletion_origin='manual'""",
                        (path_key(relative), relative))
            except (OSError, ValueError):
                pass
            raise
        self.execute("UPDATE deletion_events SET state='deleted',finished_at=%s WHERE id=%s", (now(), event_id))
        return event_id

    def delete_relaxed_targets(self, source, target, snapshots, evidence, progress=None):
        with TargetDirectory(source, target) as guard:
            return self._delete_relaxed_targets(source, target, snapshots, evidence, progress, guard)

    def _delete_relaxed_targets(self, source, target, snapshots, evidence, progress, guard):
        """Explicit aggregate trust mode. Batch journal/SQL, no per-item backup claim."""
        if (evidence.get('state') != 'aggregate_confirmed' or not evidence.get('run_id')
                or evidence.get('backup_complete') is not True or evidence.get('nothing_to_free') is not True
                or evidence.get('sync_ready') is not True
                or evidence.get('sync_token_before') != evidence.get('sync_token_after')):
            raise ValueError('Relaxed cleanup requires all three aggregate gates and unchanged sync state')
        age = (datetime.datetime.now(datetime.timezone.utc)
               - datetime.datetime.fromisoformat(evidence['checked_at'])).total_seconds()
        if not 0 <= age <= 120:
            raise ValueError('Aggregate evidence expired')
        source, target = os.path.realpath(source), os.path.realpath(target)
        if os.path.commonpath([source, target]) in (source, target):
            raise ValueError('Source and target roots must be separate')
        import stat as stat_module
        fingerprint = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns)
        valid, results = {}, []
        for relative, expected in snapshots.items():
            validate_relative(relative)
            if any(part.startswith('.') or part == '@eaDir' for part in relative.split(os.sep)):
                raise ValueError('Sync metadata and hidden files must be preserved')
            path = file_path(target, relative)
            if os.path.commonpath([target, os.path.realpath(os.path.dirname(path))]) != target:
                raise ValueError('Target escapes cleanup root')
            try:
                current = guard.stat(relative)
                if not stat_module.S_ISREG(current.st_mode) or fingerprint(current) != tuple(expected):
                    results.append({'file': relative, 'action': 'changed_kept'})
                    continue
            except FileNotFoundError:
                results.append({'file': relative, 'action': 'already_missing'})
                continue
            valid[relative] = expected
        if not valid:
            return results
        names, keys = list(valid), [path_key(name) for name in valid]
        placeholders = ','.join(['%s'] * len(keys))
        reason, stamp = 'relaxed_no_free_space', now()
        with self.transaction():
            rows = self.query('SELECT source_key,source_path,target_path,status,deletion_origin,deletion_reason,deleted_at '
                              'FROM files WHERE source_key IN (' + placeholders + ') FOR UPDATE', keys)
            previous = {row['source_path']: row for row in rows}
            for row in rows:
                if row['source_path'] not in valid or row['target_path'] != row['source_path']:
                    raise ValueError('Inventory path identity mismatch')
            # Remember untracked targets as deleted too, otherwise a later
            # source inventory could recreate files explicitly removed here.
            untracked = [name for name in names if name not in previous]
            new_rows = []
            for name in untracked:
                try:
                    source_stat = os.stat(file_path(source, name))
                except FileNotFoundError:
                    source_stat = None
                new_rows.append((path_key(name), name, path_key('.'), '.', path_key(name), name,
                    source_stat.st_size if source_stat else valid[name][2], int(source_stat is not None),
                    stamp, stamp, source_stat.st_mtime_ns if source_stat else None,
                    source_stat.st_dev if source_stat else None, source_stat.st_ino if source_stat else None))
            self.many('''INSERT INTO files
                (source_key,source_path,root_key,source_root,target_key,target_path,size_bytes,source_present,
                 first_seen_at,last_seen_at,mtime_ns,source_dev,source_ino)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)''', new_rows)
            self.many('''INSERT INTO deletion_events
                (source_key,source_path,target_path,origin,state,reason,evidence,created_at)
                VALUES (%s,%s,%s,'manual','intent',%s,%s,%s)''',
                [(path_key(name), name, name, reason,
                  json.dumps(dict(evidence, file=name, target_fingerprint=list(valid[name]))), stamp)
                 for name in names])
            ids = self.query("""SELECT id,source_path FROM deletion_events WHERE origin='manual'
                AND state='intent' AND reason=%s AND JSON_UNQUOTE(JSON_EXTRACT(evidence,'$.run_id'))=%s""",
                (reason, evidence['run_id']))
            event_ids = {row['source_path']: row['id'] for row in ids}
            self.execute("""UPDATE files SET status='deleted',deleted_at=%s,deletion_origin='manual',
                deletion_reason=%s,backup_state='aggregate_confirmed',backup_checked_at=%s,backup_evidence=%s
                WHERE source_key IN (""" + placeholders + ')',
                [stamp, reason, evidence['checked_at'], json.dumps(evidence)] + keys)
        completed = []
        for index, name in enumerate(names):
            path = file_path(target, name)
            unlinked = False
            try:
                guard.unlink(name, valid[name])
                unlinked = True
                completed.append(event_ids[name])
                results.append({'file': name, 'action': 'deleted', 'deletion_origin': 'manual',
                                'backup_state': 'aggregate_confirmed', 'event_id': event_ids[name]})
            except Exception as exc:
                self.execute("UPDATE deletion_events SET state='error',error_message=%s,finished_at=%s WHERE id=%s",
                             (type(exc).__name__, now(), event_ids[name]))
                # Keep historical/manual provenance if the file has already gone.
                if not unlinked and name in previous:
                    row = previous[name]
                    self.execute('''UPDATE files SET status=%s,deleted_at=%s,deletion_origin=%s,deletion_reason=%s
                        WHERE source_key=%s AND BINARY source_path=BINARY %s''',
                        (row['status'], row['deleted_at'], row['deletion_origin'], row['deletion_reason'], path_key(name), name))
                elif not unlinked and name in untracked:
                    self.execute("""DELETE FROM files WHERE source_key=%s AND BINARY source_path=BINARY %s
                        AND deletion_origin='manual' AND JSON_UNQUOTE(JSON_EXTRACT(backup_evidence,'$.run_id'))=%s""",
                        (path_key(name), name, evidence['run_id']))
                results.append({'file': name, 'action': 'error', 'error': str(exc), 'event_id': event_ids[name]})
            if progress and (index + 1) % 100 == 0:
                progress(index + 1, len(names))
        if completed:
            self.execute("UPDATE deletion_events SET state='deleted',finished_at=%s WHERE id IN ("
                         + ','.join(['%s'] * len(completed)) + ')', [now()] + completed)
        return results

    def delete_records(self, rows):
        with self.transaction():
            valid = []
            for row in rows:
                try:
                    self.lock_source(row['source_path'])
                except HashCollisionError:
                    continue
                valid.append((path_key(row['source_path']), row['source_path']))
            self.many('DELETE FROM files WHERE source_key=%s AND BINARY source_path=BINARY %s', valid)

    def record_results(self, results):
        with self.transaction():
            valid = []
            for status, stamp, error, path in results:
                try:
                    self.lock_source(path)
                except HashCollisionError:
                    continue
                valid.append((status, stamp, error, path_key(path), path))
            self.many("""UPDATE files SET status=%s,linked_at=COALESCE(%s,linked_at),
                last_error=%s
                WHERE source_key=%s AND BINARY source_path=BINARY %s""", valid)

    def linked(self, path):
        self.record_results([('linked', now(), None, path)])

    def close(self):
        self.conn.close()
