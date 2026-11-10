"""Edit whitelisted settings in the original TOML, preserving private content."""
import copy
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import tomlkit
from configuration import DEFAULT_VALUES
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
    return Path(os.environ.get('PICSYNC_CONFIG', Path(__file__).with_name('config.toml')))

def revision(raw):
    return hashlib.sha256(raw).hexdigest()

def editable(document):
    values = {group: {key: document.get(group, {}).get(key, DEFAULT_VALUES[group][key])
                     for key in fields} for group, fields in FIELDS.items()}
    validate(values)
    return values


def read(config_path=None):
    raw = Path(config_path or path()).read_bytes()
    try:
        document = tomllib.loads(raw.decode())
        values = editable(document)
    except (ValueError, TypeError, AttributeError, UnicodeError):
        # Parser diagnostics may quote private configuration contents.
        raise ValueError('控制器配置文件格式或运行参数无效') from None
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

def save(values, expected, base=None, config_path=None):
    validate(values)
    target = Path(config_path or path())
    # The controller owns this directory; the dashboard never mounts it.
    # A directory lock remains stable across atomic file replacements.
    directory = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        fcntl.flock(directory, fcntl.LOCK_EX)
        if target.is_symlink():
            raise ValueError('配置文件不能是符号链接')
        original = target.read_bytes()
        current, version = read(target)
        if expected != version or revision(original) != version:
            raise Conflict('配置已被修改，请重新加载后保存')
        snapshot = public(merge(current, values))
        document = tomlkit.parse(original.decode())
        changed = False
        for group, fields in values.items():
            for key, value in fields.items():
                if current[group][key] != value:
                    if group not in document:
                        document[group] = tomlkit.table()
                    document[group][key] = value
                    changed = True
        if not changed:
            return current, version
        raw = tomlkit.dumps(document).encode()
        if editable(tomllib.loads(raw.decode())) != snapshot:
            raise ValueError('配置保存校验失败')
        fd, temporary = tempfile.mkstemp(dir=target.parent, prefix='.config-')
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            if target.read_bytes() != original:
                raise Conflict('配置已被修改，请重新加载后保存')
            os.replace(temporary, target)
            os.fsync(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return snapshot, revision(raw)
    finally:
        os.close(directory)
