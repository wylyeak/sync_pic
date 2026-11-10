"""Anchored read-only image streams. No server image decoding or conversion."""
import hashlib
import os
import stat
from target_guard import _open_absolute


class PreviewError(Exception):
    def __init__(self, status):
        self.status = status


def image_type(header):
    if header.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    if header.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    if header[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if header[:4] == b'RIFF' and header[8:12] == b'WEBP':
        return 'image/webp'
    if header[4:8] == b'ftyp':
        brands = [header[i:i+4] for i in (8, *range(16, len(header)-3, 4))]
        if any(b in (b'avif', b'avis') for b in brands):
            return 'image/avif'
        if any(b in (b'heic', b'heix', b'hevc', b'hevx', b'mif1', b'msf1') for b in brands):
            return 'image/heic'
    raise PreviewError(415)


def open_image(root, relative):
    if (not isinstance(relative, str) or not relative or '\x00' in relative
            or relative.startswith('/') or any(p in ('', '.', '..') for p in relative.split('/'))):
        raise PreviewError(400)
    fd = None
    try:
        fd = _open_absolute(os.path.abspath(root))
        for part in relative.split('/')[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        child = os.open(relative.split('/')[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
        os.close(fd)
        fd = child
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 100 * 1024 * 1024:
            raise PreviewError(415)
        mime = image_type(os.read(fd, 64))
        os.lseek(fd, 0, os.SEEK_SET)
        signature = ':'.join(str(v) for v in (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns))
        etag = '"'+hashlib.sha256(signature.encode()).hexdigest()+'"'
        stream = os.fdopen(fd, 'rb')
        fd = None
        return stream, info.st_size, mime, etag
    except OSError:
        raise PreviewError(404) from None
    finally:
        if fd is not None:
            os.close(fd)
