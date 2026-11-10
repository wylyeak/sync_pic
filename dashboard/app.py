"""Photo synchronization dashboard with whitelisted runtime settings."""
from contextlib import contextmanager
from functools import lru_cache, wraps
import hashlib
import gzip
import json
import logging
from pathlib import Path
import re
import os
import html as html_escape
from urllib.parse import urlsplit
import time
try:
    import tomllib
except ImportError:
    import tomli as tomllib
import pymysql
from flask import Flask, jsonify, request, send_from_directory, g, has_request_context
from performance import TTLCache, ReadPool
import config_service

ROOT = Path(__file__).resolve().parent
app = Flask(__name__, static_folder='static')
app.json.ensure_ascii = False
app.config['MAX_CONTENT_LENGTH'] = 16384


@lru_cache(maxsize=1)
def build_info():
    # Baked into both release images, independent of startup time and private config.
    path = ROOT / 'build-info.json'
    if not path.exists():
        return dict(version='development', revision='unknown', timezone='Asia/Shanghai')
    return json.loads(path.read_text())

PHASES = {'mapping': '建立硬链接', 'sync': '等待同步', 'backup': '等待相册备份',
          'release': '相册释放', 'release_uncertain': '释放结果确认', 'deletions': '等待删除同步',
          'observe': '残留观察', 'cleanup': '手动补充清理', 'final_sync': '最终删除同步', 'complete': '已完成'}
ROUND_SELECT = """SELECT b.id,b.phase,UNIX_TIMESTAMP(b.updated_at) updated_at,
    UNIX_TIMESTAMP(b.started_at) started_at,UNIX_TIMESTAMP(b.ended_at) ended_at,
    UNIX_TIMESTAMP(b.phase_started_at) phase_started_at,UNIX_TIMESTAMP(b.next_check) next_check,
    b.observations,b.file_count,b.planned_files,b.backed_up,b.deferred,b.mapping_skipped,
    b.timing_partial,b.release_action,b.summary,
    (SELECT JSON_OBJECTAGG(p.phase,p.seconds) FROM batch_phase_stats p WHERE p.batch_id=b.id) phase_seconds
    FROM backup_batches b"""
JSON_FIELDS = ('started_at', 'ended_at', 'backed_up', 'phase_seconds', 'phase_started_at',
               'observations', 'next_check', 'summary', 'timing_partial', 'deferred_names', 'confirmed_names')

@lru_cache(maxsize=1)
def settings():
    with Path(os.environ.get('PICSYNC_DASHBOARD_CONFIG', ROOT / 'config.toml')).open('rb') as stream:
        result = tomllib.load(stream)
    for field in ('host', 'user', 'password', 'database'):
        if 'PICSYNC_MYSQL_' + field.upper() in os.environ:
            result['mysql'][field] = os.environ['PICSYNC_MYSQL_' + field.upper()]
    if 'PICSYNC_MYSQL_PORT' in os.environ:
        result['mysql']['port'] = int(os.environ['PICSYNC_MYSQL_PORT'])
    return result

cache = TTLCache()

def connect_readonly():
    c = settings()['mysql']
    conn = pymysql.connect(**{k: c[k] for k in ('host', 'port', 'user', 'password', 'database')},
        charset='utf8mb4', autocommit=True, cursorclass=pymysql.cursors.DictCursor,
        connect_timeout=5, read_timeout=10, write_timeout=5)
    try:
        with conn.cursor() as cur:
            cur.execute("SET time_zone='+00:00'")
            cur.execute('SET SESSION TRANSACTION READ ONLY')
    except Exception:
        conn.close()
        raise
    return conn

pool = ReadPool(connect_readonly)

@contextmanager
def db():
    started = time.perf_counter()
    with pool.connection() as conn:
        if has_request_context():
            g.pool_ms = getattr(g, 'pool_ms', 0) + (time.perf_counter() - started) * 1000
        with conn.cursor() as cur:
            original = cur.execute
            def execute(*args, **kwargs):
                tick = time.perf_counter()
                try:
                    return original(*args, **kwargs)
                finally:
                    if has_request_context():
                        g.db_ms = getattr(g, 'db_ms', 0) + (time.perf_counter() - tick) * 1000
            cur.execute = execute
            try:
                yield cur
            finally:
                cur.execute = original

class UncachedResponse(Exception):
    def __init__(self, response):
        self.body = response.get_data()
        self.status = response.status_code
        self.mimetype = response.mimetype

def cached_response(ttl):
    def decorate(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            key = ('response', request.path, tuple(sorted(request.args.items())))
            def load():
                response = app.make_response(view(*args, **kwargs))
                if response.status_code != 200:
                    raise UncachedResponse(response)
                return response.get_data()
            try:
                result = cache.get(key, ttl, load)
            except UncachedResponse as response:
                return app.response_class(response.body, status=response.status, mimetype=response.mimetype)
            response = app.response_class(result.value, mimetype='application/json')
            response.headers['X-Cache'] = result.state
            response.headers['X-Data-Age'] = str(round(time.monotonic() - result.created_at, 2))
            return response
        return wrapped
    return decorate

def cached_batch(batch_id):
    def load():
        with db() as cur:
            cur.execute(ROUND_SELECT + ' WHERE id=%s', (batch_id,))
            row = cur.fetchone()
        return row
    return cache.get(('batch', batch_id), 5, load).value

def runtime():
    try:
        data = json.loads(Path(settings()['dashboard']['telemetry']).read_text())
    except (OSError, ValueError):
        return {'available': False, 'message': '控制器尚未发布状态', 'events': []}
    data['available'] = True
    stamp = data.get('controller', {}).get('checked_at')
    data['controller_stale'] = stamp is None or time.time() - stamp > 180
    return data

def round_row(row):
    if row is None:
        return None
    row = dict(row)
    if 'data' in row:
        data = json.loads(row.pop('data'))
        proof = data.get('backup_confirmed')
        row.update(started_at=data.get('started_at'), ended_at=data.get('ended_at'),
            file_count=len(data.get('members', {})), backed_up=len(proof) if proof is not None else None,
            phase_seconds=data.get('phase_seconds', {}), phase_started_at=data.get('phase_started_at'),
            observations=data.get('observations', 0), next_check=data.get('next_check'),
            summary=data.get('summary'), timing_partial=data.get('timing_partial', False),
            deferred=len(data.get('deferred', [])), deferred_names=data.get('deferred', []),
            confirmed_names=list(proof) if proof is not None else None)
    for field in JSON_FIELDS:
        value = row.get(field)
        if isinstance(value, str):
            try:
                row[field] = json.loads(value)
            except ValueError:
                row[field] = None
    for field in ('started_at','ended_at','phase_started_at','next_check','updated_at'):
        if row.get(field) is not None and not isinstance(row[field], str):
            row[field] = float(row[field])
    row['phase_label'] = PHASES.get(row['phase'], row['phase'])
    row['file_count'] = row.get('file_count') or 0
    row['deferred'] = row.get('deferred') or 0
    row['phase_seconds'] = row.get('phase_seconds') or {}
    confirmed = row.pop('confirmed_names', None)
    deferred = row.pop('deferred_names', None) or []
    if confirmed is not None:
        row['backed_up'] = len(set(confirmed) - set(deferred))
    if row.get('summary') is not None:
        row['backed_up'] = row['summary'].get('backed_up')
    return row

def page_args():
    try:
        page = min(10000, max(1, int(request.args.get('page', '1'))))
        size = min(100, max(1, int(request.args.get('size', '30'))))
    except ValueError:
        raise ValueError('分页参数无效')
    return page, size

def paginated(cur, table, fields, conditions, params, order):
    page, size = page_args()
    where = ' WHERE ' + ' AND '.join(conditions) if conditions else ''
    def count():
        cur.execute('SELECT COUNT(*) total FROM ' + table + where, params)
        return cur.fetchone()['total']
    total = cache.get(('count', table, tuple(conditions), tuple(params)), 10, count).value
    cur.execute('SELECT ' + fields + ' FROM ' + table + where + ' ORDER BY ' + order + ' LIMIT %s OFFSET %s',
                params + [size, (page - 1) * size])
    return dict(items=cur.fetchall(), total=total, page=page, page_size=size)

@app.before_request
def start_timing():
    g.started_at = time.perf_counter()

@app.after_request
def headers(response):
    response.headers['Cache-Control'] = ('public, max-age=31536000, immutable' if request.path.startswith('/static/') and request.args.get('v') else
        'public, max-age=3600' if request.path.startswith('/static/') else 'no-store' if request.path.startswith('/api/') else 'no-cache')
    response.headers['Server-Timing'] = 'app;dur=%.2f, db;dur=%.2f, pool;dur=%.2f' % (
        (time.perf_counter() - getattr(g, 'started_at', time.perf_counter())) * 1000, getattr(g, 'db_ms', 0), getattr(g, 'pool_ms', 0))
    if request.path.startswith('/api/') and 'gzip' in request.accept_encodings and response.status_code == 200 and len(response.get_data()) > 512:
        response.set_data(gzip.compress(response.get_data(), compresslevel=3))
        response.headers['Content-Encoding'] = 'gzip'
        response.vary.add('Accept-Encoding')
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'self'; base-uri 'none'"
    return response

@lru_cache(maxsize=4)
def static_version(filename):
    return hashlib.sha256((Path(app.static_folder) / filename).read_bytes()).hexdigest()[:12]

@app.route('/')
def index():
    html = (Path(app.static_folder) / 'index.html').read_text()
    info = build_info()
    for token, value in [('__BUILD_VERSION__', info['version']),
                         ('__BUILD_REVISION__', info['revision']),
                         ('__BUILD_SHORT_REVISION__', info['revision'][:7])]:
        html = html.replace(token, html_escape.escape(value, quote=True))
    for key, token in [('pixel_url', '__PIXEL_URL__'), ('syncthing_url', '__SYNCTHING_URL__')]:
        url = os.environ.get('PICSYNC_' + key.upper(), settings().get('links', {}).get(key, ''))
        if url and (urlsplit(url).scheme not in ('http', 'https') or not urlsplit(url).netloc):
            raise ValueError('Shortcut URLs must be absolute HTTP(S) URLs')
        html = html.replace('href="' + token + '"', 'href="' + html_escape.escape(url, quote=True) + '"' if url else 'hidden href="#"')
    for filename in ('app.js', 'style.css'):
        version = static_version(filename)
        html = html.replace('/static/' + filename, '/static/' + filename + '?v=' + version)
    return app.response_class(html, mimetype='text/html')

@app.route('/health')
def health():
    return jsonify(ok=True)

@app.route('/api/version')
def version():
    return jsonify(build_info())

@app.route('/api/settings', methods=['GET', 'PUT'])
def runtime_settings():
    run = runtime()
    effective = run.get('settings', {})
    base = effective.get('values')
    if not base:
        return jsonify(error='等待控制器发布配置后重试'), 503
    if request.method == 'PUT':
        origin = request.headers.get('Origin')
        if (origin and (urlsplit(origin).scheme not in ('http', 'https') or urlsplit(origin).netloc != request.host)) or request.headers.get('X-PicSync-Settings') != '1' or not request.is_json:
            return jsonify(error='配置保存必须来自本站页面'), 403
        if run.get('controller_stale'):
            return jsonify(error='控制器离线，暂不能保存配置'), 503
        body = request.get_json()
        if type(body) is not dict or set(body) != {'values', 'revision'}:
            return jsonify(error='请求格式无效'), 400
    else:
        body = None
    try:
        data, status = config_service.call(request.method, body)
        data['controller_stale'] = run.get('controller_stale', True)
        return jsonify(data), status
    except (OSError, ValueError):
        return jsonify(error='控制器配置服务暂不可用，请稍后重试'), 503

@app.route('/api/overview')
def overview():
    def inventory_counts():
        inventory = {name: {'files': 0, 'bytes': 0} for name in ('pending', 'linked', 'deleted')}
        breakdown = {name: {'files': 0, 'bytes': 0} for name in ('manual','syncthing','unknown')}
        with db() as cur:
            cur.execute("SELECT status,deletion_origin,COUNT(*) files,COALESCE(SUM(size_bytes),0) bytes "
                        "FROM files WHERE status<>'pending' OR source_present=1 GROUP BY status,deletion_origin")
            for row in cur.fetchall():
                group = inventory[row['status']]
                group['files'] += row['files']
                group['bytes'] += int(row['bytes'])
                if row['status'] == 'deleted':
                    origin = row['deletion_origin'] if row['deletion_origin'] in breakdown else 'unknown'
                    breakdown[origin]['files'] += row['files']
                    breakdown[origin]['bytes'] += int(row['bytes'])
        return inventory, breakdown, time.time()
    inv = cache.get(('inventory',), 15, inventory_counts, stale_for=45)
    inventory, breakdown, inventory_updated_at = inv.value
    origins = {name: group['files'] for name,group in breakdown.items()}
    def round_snapshot():
        with db() as cur:
            cur.execute(ROUND_SELECT + " WHERE phase<>'complete' ORDER BY b.updated_at,b.id LIMIT 1")
            active = round_row(cur.fetchone())
            cur.execute(ROUND_SELECT + ' ORDER BY b.updated_at DESC,b.id DESC LIMIT 5')
            recent = [round_row(row) for row in cur.fetchall()]
            cur.execute("SELECT COUNT(*) total,SUM(phase='complete') completed FROM backup_batches")
            batches = cur.fetchone()
        return active, recent, batches
    snapshot = cache.get(('round-snapshot',), 5, round_snapshot, stale_for=25)
    active, recent, batches = snapshot.value
    return jsonify(time=time.time(), statistics_stale=time.monotonic()-inv.created_at>30, inventory_updated_at=inventory_updated_at, inventory=inventory, deletion_origins=origins, deletion_breakdown=breakdown, active=active,
                   recent=recent, batches=batches, runtime=runtime(), phases=PHASES,
                   refresh_seconds=settings()['dashboard'].get('refresh_seconds', 10))

@app.route('/api/runtime')
def live_runtime():
    return jsonify(time=time.time(), runtime=runtime())

@app.route('/api/batches')
@cached_response(5)
def batches():
    page, size = page_args()
    with db() as cur:
        cur.execute('SELECT COUNT(*) total FROM backup_batches')
        total = cur.fetchone()['total']
        cur.execute(ROUND_SELECT + ' ORDER BY b.updated_at DESC,b.id DESC LIMIT %s OFFSET %s', (size, (page - 1) * size))
        rows = [round_row(row) for row in cur.fetchall()]
    return jsonify(items=rows, total=total, page=page, page_size=size)

@app.route('/api/batches/<batch_id>')
@cached_response(5)
def batch_detail(batch_id):
    if not re.fullmatch('[a-f0-9]{32}', batch_id):
        return jsonify(error='批次编号无效'), 400
    raw = cached_batch(batch_id)
    if raw is None:
        return jsonify(error='批次不存在'), 404
    row = round_row(raw)
    return jsonify(row)

FILE_FIELDS = ('id,source_path,size_bytes,status,source_present,mtime_ns,first_seen_at,last_seen_at,'
               'linked_at,deleted_at,deletion_origin,deletion_reason,backup_state,backup_checked_at,last_error')

@app.route('/api/files')
@cached_response(3)
def files():
    conditions, params = [], []
    status = request.args.get('status', '')
    if status:
        if status not in ('pending', 'linked', 'deleted'):
            return jsonify(error='文件状态无效'), 400
        conditions.append('status=%s')
        params.append(status)
        if status == 'pending':
            conditions.append('source_present=1')
    query = request.args.get('q', '').strip()[:300]
    if query:
        conditions.append('INSTR(source_path,%s)>0')
        params.append(query)
    if request.args.get('batch'):
        batch_id = request.args['batch']
        if not re.fullmatch('[a-f0-9]{32}', batch_id):
            return jsonify(error='批次编号无效'), 400
        row = cached_batch(batch_id)
        if row is None:
            return jsonify(error='批次不存在'), 404
        conditions.append('source_key IN (SELECT source_key FROM controller_file_batches WHERE batch_id=%s)')
        params.append(batch_id)
    with db() as cur:
        table = 'files FORCE INDEX (files_source_key)' if request.args.get('batch') else 'files'
        result = paginated(cur, table, FILE_FIELDS, conditions, params, 'mtime_ns DESC,id DESC')
    return jsonify(result)

@app.route('/api/files/<int:file_id>')
@cached_response(3)
def file_detail(file_id):
    with db() as cur:
        cur.execute('SELECT ' + FILE_FIELDS + ',source_key FROM files WHERE id=%s', (file_id,))
        row = cur.fetchone()
        if row is None:
            return jsonify(error='文件不存在'), 404
        key = row.pop('source_key')
        cur.execute('SELECT batch_id FROM controller_file_batches WHERE source_key=%s AND BINARY source_path=BINARY %s',
                    (key, row['source_path']))
        owner = cur.fetchone()
        row['batch_id'] = owner['batch_id'] if owner else None
        cur.execute('SELECT id,origin,state,reason,created_at,finished_at,error_message FROM deletion_events '
                    'WHERE source_key=%s AND BINARY source_path=BINARY %s ORDER BY id DESC LIMIT 20',
                    (key, row['source_path']))
        row['audits'] = cur.fetchall()
    return jsonify(row)

@app.route('/api/audits')
@cached_response(3)
def audits():
    conditions, params = [], []
    for key, allowed in [('origin', ('manual', 'syncthing', 'unknown')), ('state', ('intent', 'deleted', 'error', 'unknown'))]:
        value = request.args.get(key, '')
        if value:
            if value not in allowed:
                return jsonify(error='审计筛选无效'), 400
            conditions.append(key + '=%s')
            params.append(value)
    query = request.args.get('q', '').strip()[:300]
    if query:
        conditions.append('INSTR(source_path,%s)>0')
        params.append(query)
    with db() as cur:
        result = paginated(cur, 'deletion_events', 'id,source_path,origin,state,reason,created_at,finished_at,error_message',
                           conditions, params, 'id DESC')
    return jsonify(result)

@app.errorhandler(pymysql.MySQLError)
def database_error(exc):
    logging.error('Dashboard database unavailable: %s', type(exc).__name__)
    return jsonify(error='数据库暂时不可用，稍后将自动重试', runtime=runtime()), 503

@app.errorhandler(ValueError)
def invalid_input(exc):
    return jsonify(error='请求参数无效'), 400

@app.errorhandler(500)
def internal_error(exc):
    logging.error('Dashboard request failed: %s', type(exc).__name__)
    return jsonify(error='监控数据暂时无法读取'), 500
