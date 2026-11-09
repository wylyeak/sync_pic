"""Startup-only TOML configuration with redacted logging."""
from pathlib import Path
import copy
import re
import os
try:
    import tomllib
except ImportError:
    import tomli as tomllib
from target_guard import PROTECTED_SOURCE, ALLOWED_TARGET

DEFAULT = Path(os.environ.get('PICSYNC_CONFIG', Path(__file__).with_name('config.toml')))
DEFAULT_VALUES = {'directories': {'source': PROTECTED_SOURCE, 'target': ALLOWED_TARGET}, 'scan': {'interval': 3600, 'progress_every': 100}, 'mapping': {'max_bytes': 16106127360, 'order': 'mtime_asc', 'min_battery_percent': 10, 'resume_battery_percent': 20}, 'monitor': {'interval': 60, 'idle_interval': 300}, 'observation': {'confirmations': 3, 'interval': 300}, 'syncthing': {'config': '/etc/syncthing/config.xml', 'folder': 'photos', 'device': '', 'timeout': 10}, 'pixel': {'container': 'pixel-web', 'adb_path': 'adb', 'adb_backend': 'host', 'serial': '', 'adb_timeout': 40}, 'mysql': {'host': '127.0.0.1', 'port': 3306, 'user': 'picsync', 'database': 'picsync', 'password': ''}, 'bark': {'enabled': False, 'key': '', 'timeout': 15, 'retries': 2, 'blocked_after': 1200, 'error_interval': 3600, 'error_threshold': 2}, 'logging': {'path': 'logs/file_sync.log', 'level': 'INFO', 'max_bytes': 10485760, 'backups': 5}}

def load(path=DEFAULT):
    result = copy.deepcopy(DEFAULT_VALUES)
    with Path(path).open('rb') as stream:
        overrides = tomllib.load(stream)
    for group, values in overrides.items():
        if group not in result or not isinstance(values, dict):
            raise ValueError('Unknown configuration group: ' + group)
        for key, value in values.items():
            if key not in result[group] or type(value) is not type(result[group][key]):
                raise ValueError('Invalid configuration: ' + group + '.' + key)
            result[group][key] = value
    for env, group, key in (
        ('PICSYNC_MYSQL_HOST', 'mysql', 'host'),
        ('PICSYNC_MYSQL_USER', 'mysql', 'user'),
        ('PICSYNC_MYSQL_PASSWORD', 'mysql', 'password'),
        ('PICSYNC_MYSQL_DATABASE', 'mysql', 'database'),
        ('PICSYNC_BARK_KEY', 'bark', 'key'),
        ('PICSYNC_ADB_PATH', 'pixel', 'adb_path'),
        ('PICSYNC_SERIAL', 'pixel', 'serial'),
        ('PICSYNC_SYNCTHING_CONFIG', 'syncthing', 'config'),
        ('PICSYNC_SYNCTHING_DEVICE', 'syncthing', 'device'),
    ):
        if env in os.environ:
            result[group][key] = os.environ[env]
    if 'PICSYNC_MYSQL_PORT' in os.environ:
        result['mysql']['port'] = int(os.environ['PICSYNC_MYSQL_PORT'])
    if result['directories'] != dict(source=PROTECTED_SOURCE, target=ALLOWED_TARGET):
        raise ValueError('Source and deletion target are fixed safety boundaries')
    for group, values in result.items():
        for key, value in values.items():
            if type(value) is int and value <= 0 and key != 'retries':
                raise ValueError('Configuration must be positive: ' + group + '.' + key)
    if result['observation']['confirmations'] < 3 or result['observation']['interval'] < 300:
        raise ValueError('At least three observations, 300 seconds apart are required')
    if not result['mapping']['min_battery_percent'] <= result['mapping']['resume_battery_percent'] <= 100:
        raise ValueError('Require 1 <= min_battery_percent <= resume_battery_percent <= 100')
    if result['mapping']['order'] != 'mtime_asc' or result['bark']['retries'] < 0:
        raise ValueError('Invalid ordering or retry count')
    if not 1 <= result['mysql']['port'] <= 65535:
        raise ValueError('Invalid MySQL port')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}', result['mysql']['database']):
        raise ValueError('Invalid MySQL database')
    if result['logging']['level'] not in ('DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'):
        raise ValueError('Invalid log level')
    return copy.deepcopy(result)


def redacted(config):
    result = copy.deepcopy(config)
    result['mysql']['password'] = '***'
    result['bark']['key'] = '***'
    return result
