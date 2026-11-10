"""Whitelisted runtime settings, atomically persisted without credentials."""
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile
try:
    import tomllib
except ImportError:
    import tomli as tomllib

# label, minimum, maximum; booleans use None bounds.
FIELDS = {
 'scan': {'interval': ('源目录扫描间隔（秒）', 1, 604800), 'progress_every': ('扫描进度记录间隔（文件数）', 1, 100000)},
 'mapping': {'max_bytes': ('每批最大容量（字节）', 1, 10995116277760), 'min_battery_percent': ('暂停电量（%）', 1, 100), 'resume_battery_percent': ('恢复电量（%）', 1, 100)},
 'monitor': {'interval': ('工作检查间隔（秒）', 1, 86400), 'idle_interval': ('空闲检查间隔（秒）', 1, 86400)},
 'observation': {'confirmations': ('清理前观察次数', 3, 1000), 'interval': ('观察间隔（秒）', 300, 604800)},
 'bark': {'enabled': ('启用 Bark 通知', None, None), 'timeout': ('通知超时（秒）', 1, 300), 'retries': ('通知重试次数', 0, 10), 'blocked_after': ('阻塞通知等待（秒）', 1, 604800), 'error_interval': ('异常通知间隔（秒）', 1, 604800), 'error_threshold': ('连续异常通知门槛', 1, 1000)},
}

def path():
    return Path(os.environ.get('PICSYNC_LIVE_CONFIG', '/state/settings/controller.toml'))

def revision(raw):
    return hashlib.sha256(raw).hexdigest()

def read():
    try:
        raw = path().read_bytes()
    except FileNotFoundError:
        raw = b''
    values = tomllib.loads(raw.decode())
    validate(values)
    return values, revision(raw)

def validate(values):
    if type(values) is not dict:
        raise ValueError('配置必须为对象')
    for group, fields in values.items():
        if group not in FIELDS or type(fields) is not dict:
            raise ValueError('不支持的配置分组')
        for key, value in fields.items():
            if key not in FIELDS[group]:
                raise ValueError('不支持的配置项')
            label, low, high = FIELDS[group][key]
            if low is None:
                if type(value) is not bool:
                    raise ValueError(label + '必须为开关')
            elif type(value) is not int or not low <= value <= high:
                raise ValueError(f'{label}必须为 {low}–{high} 的整数')
    battery = values.get('mapping', {})
    if 'min_battery_percent' in battery and 'resume_battery_percent' in battery and battery['min_battery_percent'] > battery['resume_battery_percent']:
        raise ValueError('恢复电量不能低于暂停电量')

def public(config):
    return {group: {key: config[group][key] for key in fields} for group, fields in FIELDS.items()}

def merge(base, values):
    result = copy.deepcopy(base)
    for group, fields in values.items():
        result[group].update(fields)
    validate(public(result))
    return result

class Conflict(ValueError):
    pass

def save(values, expected, base):
    validate(values)
    target = path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with target.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current, version = read()
        if expected != version:
            raise Conflict('配置已被其他页面修改，请重新加载后保存')
        combined = merge(merge(base, current), values)
        # Persist a complete, validated snapshot of editable values only.
        snapshot = public(combined)
        raw = ''.join('[' + group + ']\n' + ''.join(key + ' = ' + json.dumps(value) + '\n' for key, value in fields.items()) + '\n' for group, fields in snapshot.items()).encode()
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.settings-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return snapshot, revision(raw)
