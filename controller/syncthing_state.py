"""Read-only Syncthing provenance; never infer remote deletion from absence."""
import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET


class SyncthingState:
    def __init__(self, config, folder='photos', remote='', timeout=5):
        if not remote:
            raise ValueError('Explicit Syncthing device ID is required')
        root = ET.parse(config).getroot()
        self.key = root.findtext('gui/apikey')
        address = root.findtext('gui/address')
        if address != '127.0.0.1:8384' or not self.key:
            raise ValueError('Expected authenticated local Syncthing API on 127.0.0.1:8384')
        scheme = 'https' if root.find('gui').get('tls') == 'true' else 'http'
        self.base, self.folder, self.remote, self.timeout = scheme + '://' + address, folder, remote, timeout
        devices = [d.get('id') for d in root.findall('device') if d.get('id', '') == remote or d.get('id', '').split('-')[0] == remote]
        if len(devices) != 1:
            raise ValueError('Expected exactly one matching Pixel device')
        self.remote_id = devices[0]
        self.remote = self.remote_id.split('-')[0]

    def get(self, endpoint, params=None):
        query = '?' + urllib.parse.urlencode(params) if params else ''
        request = urllib.request.Request(self.base + endpoint + query, headers={'X-API-Key': self.key})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def settled_state(self):
        local = self.get('/rest/db/status', {'folder': self.folder})
        remote = self.get('/rest/db/completion', {'folder': self.folder, 'device': self.remote_id})
        connections = self.get('/rest/system/connections').get('connections', {})
        connected = connections.get(self.remote_id, {}).get('connected') is True
        ready = (connected and local.get('state') == 'idle' and local.get('needTotalItems') == 0
                 and local.get('pullErrors') == 0 and not local.get('error')
                 and remote.get('needItems') == 0 and remote.get('needDeletes') == 0
                 and remote.get('completion', 0) >= 100 and remote.get('remoteState') == 'valid')
        return {'ready': ready, 'local_state': local.get('state'), 'connected': connected,
                'need_local': local.get('needTotalItems'), 'need_remote': remote.get('needItems'),
                'completion': remote.get('completion'), 'total_bytes': remote.get('globalBytes'),
                'total_items': remote.get('globalItems'), 'need_remote_bytes': remote.get('needBytes'),
                'need_remote_deletes': remote.get('needDeletes'), 'need_local_bytes': local.get('needBytes'),
                'pull_errors': local.get('pullErrors'), 'error': local.get('error'),
                'remote_state': remote.get('remoteState'),
                'token': [local.get('version'), local.get('globalTotalItems'), remote.get('sequence')]}

    def file(self, relative):
        query = urllib.parse.urlencode({'folder': self.folder, 'file': relative})
        request = urllib.request.Request(self.base + '/rest/db/file?' + query,
                                         headers={'X-API-Key': self.key})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return {}
            raise RuntimeError('Syncthing API HTTP {}'.format(exc.code)) from None

    def remote_deletion(self, relative):
        data = self.file(relative)
        local, global_file = data.get('local', {}), data.get('global', {})
        if (local.get('name') == relative and global_file.get('name') == relative
                and local.get('deleted') is True and global_file.get('deleted') is True
                and not local.get('invalid') and not global_file.get('invalid')
                and local.get('modifiedBy') == self.remote
                and global_file.get('modifiedBy') == self.remote):
            return {'file': relative, 'modifiedBy': self.remote,
                    'version': global_file.get('version'), 'modified': global_file.get('modified'),
                    'local_deleted': True, 'global_deleted': True}
        return None
