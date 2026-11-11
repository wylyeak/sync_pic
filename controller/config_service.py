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


def connection():
    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(self.timeout)
            self.sock.connect(str(socket_path()))
    return Connection('localhost', timeout=15)


def call(method, body=None, path='/settings'):
    client = connection()
    try:
        client.request(method, path, body=json.dumps(body) if body is not None else None,
                           headers={'Content-Type': 'application/json'})
        response = client.getresponse()
        data = json.loads(response.read(MAX_BODY + 1))
        return data, response.status
    finally:
        client.close()


def preview(relative, etag=None):
    client = connection()
    try:
        client.request('POST', '/preview', body=json.dumps({'path': relative, 'etag': etag}),
                       headers={'Content-Type': 'application/json'})
        response = client.getresponse()
        return client, response
    except BaseException:
        client.close()
        raise


@contextmanager
def serve(controller):
    # Import lazily: dashboard only needs the socket client, no photo access.
    from photo_preview import open_image, PreviewError
    preview_slots = threading.BoundedSemaphore(3)
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

        def do_POST(self):
            if self.path == '/scan' and hasattr(controller, 'request_scan'):
                data, status = controller.request_scan()
                return self.respond(data, status)
            if self.path != '/preview' or not hasattr(controller, 'source'):
                return self.respond({'error': '预览不可用'}, 404)
            if not preview_slots.acquire(blocking=False):
                return self.respond({'error': '预览繁忙'}, 503)
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY:
                    raise ValueError()
                body = json.loads(self.rfile.read(size))
                if type(body) is not dict or set(body) != {'path', 'etag'}:
                    raise ValueError()
                stream, size, mime, etag = open_image(controller.source, body['path'])
                with stream:
                    self.send_response(304 if body['etag'] == etag else 200)
                    self.send_header('Content-Type', mime)
                    self.send_header('ETag', etag)
                    self.send_header('Content-Length', str(0 if body['etag'] == etag else size))
                    self.end_headers()
                    if body['etag'] != etag:
                        while block := stream.read(65536):
                            self.wfile.write(block)
            except PreviewError as exc:
                return self.respond({'error': '照片预览不可用'}, exc.status)
            except (ValueError, TypeError):
                return self.respond({'error': '预览参数无效'}, 400)
            except OSError:
                pass  # Browser cancelled or a source became unavailable during transfer.
            finally:
                preview_slots.release()

        def do_PUT(self):
            self.handle_settings(write=True)

        def respond(self, data, status):
            raw = json.dumps(data, ensure_ascii=False).encode()
            try:
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except OSError:
                pass  # Normal cancellation while a browser moves to another file.

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
