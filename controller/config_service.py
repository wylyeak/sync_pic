"""Controller-owned config writer, exposed only over a local Unix socket."""
from contextlib import contextmanager
import http.client
from http.server import BaseHTTPRequestHandler
import json
import os
from pathlib import Path
import socket
import socketserver
import stat
import threading
import live_config

MAX_BODY = 16384

def socket_path():
    return Path(os.environ.get('PICSYNC_CONFIG_SOCKET', '/state/status/config.sock'))


def call(method, body=None):
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(socket_path()))
    connection = Connection('localhost', timeout=15)
    try:
        connection.request(method, '/settings', body=json.dumps(body) if body is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = connection.getresponse()
        data = json.loads(response.read(MAX_BODY + 1))
        return data, response.status
    finally:
        connection.close()


@contextmanager
def serve(controller):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log request payloads or parser diagnostics.

        def handle_settings(self, write=False):
            if self.path != '/settings':
                return self.respond({'error': '接口不存在'}, 404)
            try:
                if write:
                    size = int(self.headers.get('Content-Length', '0'))
                    if not 0 < size <= MAX_BODY:
                        raise ValueError()
                    body = json.loads(self.rfile.read(size))
                    if type(body) is not dict or set(body) != {'values', 'revision'}:
                        raise ValueError()
                    values, revision = live_config.save(body['values'], body['revision'],
                                                       config_path=controller.config_path)
                else:
                    values, revision = live_config.read(controller.config_path)
                return self.respond(dict(values=values, revision=revision, fields=live_config.FIELDS,
                    applied=controller.config_revision == revision, error=None), 200)
            except live_config.Conflict:
                return self.respond({'error': '配置已被修改，请重新加载后保存'}, 409)
            except (ValueError, TypeError):
                return self.respond({'error': '配置格式、参数类型或范围无效，请检查后重试'}, 400)
            except OSError:
                return self.respond({'error': '配置文件读写失败，原配置保留'}, 503)

        def setup(self):
            self.request.settimeout(10)
            super().setup()

        def do_GET(self):
            self.handle_settings()

        def do_PUT(self):
            self.handle_settings(write=True)

        def respond(self, data, status):
            raw = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
        daemon_threads = True

    path = socket_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if not stat.S_ISSOCK(path.lstat().st_mode):
            raise ValueError('Configuration socket path is not a socket')
        path.unlink()
    server = Server(str(path), Handler)
    os.chmod(path, 0o600)
    worker = threading.Thread(target=server.serve_forever, name='config-api', daemon=True)
    worker.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=5)
        path.unlink(missing_ok=True)
