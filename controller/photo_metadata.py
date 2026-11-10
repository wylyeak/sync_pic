"""Read camera capture time without substituting file timestamps."""
import datetime
import os
import re
import warnings

from PIL import Image
from pillow_heif import register_heif_opener

register_heif_opener()


def capture_time(path):
    """Keep EXIF wall time (and offset if present); missing/invalid is unknown."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as stream, warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(stream) as image:
                exif = image.getexif()
                details = exif.get_ifd(34665) if 34665 in exif else {}
                raw = details.get(36867) or exif.get(36867)
                if not isinstance(raw, str):
                    return None
                stamp = datetime.datetime.strptime(raw.strip('\x00 '), '%Y:%m:%d %H:%M:%S')
                offset = details.get(36881) or exif.get(36881)
                if isinstance(offset, str) and re.fullmatch(r'[+-](?:0\d|1[0-4]):[0-5]\d', offset):
                    return stamp.isoformat() + offset
                return stamp.isoformat()
    except (OSError, ValueError, TypeError, KeyError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        return None


def creation_time(path):
    """Use filesystem birth time only; Unix ctime is a metadata change time."""
    import ctypes
    import struct
    try:
        info = os.stat(path, follow_symlinks=False)
        birth = getattr(info, 'st_birthtime', None)
        if birth is None:
            libc = ctypes.CDLL(None, use_errno=True)
            statx = getattr(libc, 'statx', None)
            if statx is None:
                return None
            buffer = ctypes.create_string_buffer(256)
            if statx(-100, ctypes.c_char_p(os.fsencode(path)), 0x100, 0x800, buffer) != 0:
                return None
            if not struct.unpack_from('=I', buffer.raw, 0)[0] & 0x800:
                return None
            seconds, nanos = struct.unpack_from('=qI', buffer.raw, 80)
            birth = seconds + nanos / 1e9
        return datetime.datetime.fromtimestamp(birth, datetime.timezone.utc).isoformat()
    except (OSError, ValueError, OverflowError):
        return None
