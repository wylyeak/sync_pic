"""The sole filesystem deletion boundary: anchored, no-symlink target entries."""
import os
import stat

PROTECTED_SOURCE = os.environ.get('PICSYNC_SOURCE', '/photos/source')
ALLOWED_TARGET = os.environ.get('PICSYNC_TARGET', '/photos/target')
for path in (PROTECTED_SOURCE, ALLOWED_TARGET):
    if not os.path.isabs(path) or os.path.normpath(path) != path or path == '/':
        raise ValueError('Safety roots must be canonical absolute directories, never /')


def _separate(left, right):
    if os.path.commonpath([left, right]) in (left, right):
        raise ValueError('Source and target directories must not overlap')


def _open_absolute(path):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open('/', flags)
    try:
        for part in path.strip('/').split('/'):
            if part:
                new = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mount_id(fd):
    with open('/proc/self/fdinfo/{}'.format(fd)) as stream:
        for line in stream:
            if line.startswith('mnt_id:'):
                return int(line.split(':', 1)[1])
    raise ValueError('Cannot verify directory mount identity')


class TargetDirectory:
    def __init__(self, source, target):
        self.source, self.target = os.path.abspath(source), os.path.abspath(target)
        _separate(self.source, self.target)
        _separate(PROTECTED_SOURCE, self.target)
        self.source_fd = self.target_fd = None
        try:
            self.source_fd = _open_absolute(self.source)
            self.target_fd = _open_absolute(self.target)
            self.mount = _mount_id(self.source_fd)
            if _mount_id(self.target_fd) != self.mount:
                raise ValueError('Cleanup rejects separate/bind-mounted target aliases')
            a, b = os.fstat(self.source_fd), os.fstat(self.target_fd)
            if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
                raise ValueError('Target aliases source directory')
            self._check_root()
        except BaseException:
            self.close()
            raise

    def close(self):
        for key in ('source_fd', 'target_fd'):
            fd = getattr(self, key, None)
            if fd is not None:
                os.close(fd)
                setattr(self, key, None)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def _check_root(self):
        # A renamed root or substituted path must never authorize deletion.
        for path, fd in [(self.source, self.source_fd), (self.target, self.target_fd)]:
            current = os.lstat(path)
            pinned = os.fstat(fd)
            if (not stat.S_ISDIR(current.st_mode)
                    or (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino)
                    or os.readlink('/proc/self/fd/{}'.format(fd)) != path):
                raise ValueError('Cleanup root changed or is a symlink')

    def _parent(self, relative):
        if (not relative or os.path.isabs(relative) or os.path.normpath(relative) != relative
                or any(part in ('', '.', '..') for part in relative.split(os.sep))):
            raise ValueError('Invalid relative target path')
        self._check_root()
        parts = relative.split(os.sep)
        if any(part.startswith('.') or part == '@eaDir' for part in parts):
            raise ValueError('Cleanup cannot remove hidden/sync metadata entries')
        fd = os.dup(self.target_fd)
        expected = self.target
        try:
            for part in parts[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = new
                expected = os.path.join(expected, part)
                if _mount_id(fd) != self.mount:
                    raise ValueError('Cleanup cannot cross a mount boundary')
            if os.readlink('/proc/self/fd/{}'.format(fd)) != expected:
                raise ValueError('Target parent moved during cleanup')
            _separate(PROTECTED_SOURCE, expected)
            _separate(self.source, expected)
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    def stat(self, relative):
        parent, name = self._parent(relative)
        try:
            result = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISREG(result.st_mode):
                raise ValueError('Cleanup target must be a regular file')
            return result
        finally:
            os.close(parent)

    def link_source(self, relative, expected):
        """Create a hard link using pinned, no-symlink directory descriptors."""
        parent, name = self._parent(relative)
        source_parent = os.dup(self.source_fd)
        try:
            for part in relative.split(os.sep)[:-1]:
                new = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                              dir_fd=source_parent)
                os.close(source_parent)
                source_parent = new
            value = os.stat(name, dir_fd=source_parent, follow_symlinks=False)
            actual = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if not stat.S_ISREG(value.st_mode) or actual != tuple(expected):
                raise ValueError('Source changed before hard link')
            self._check_root()
            os.link(name, name, src_dir_fd=source_parent, dst_dir_fd=parent, follow_symlinks=False)
        finally:
            os.close(source_parent)
            os.close(parent)

    def unlink(self, relative, expected):
        parent, name = self._parent(relative)
        try:
            value = os.stat(name, dir_fd=parent, follow_symlinks=False)
            actual = (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
            if not stat.S_ISREG(value.st_mode) or actual != tuple(expected):
                raise ValueError('Target changed before guarded unlink')
            self._check_root()
            expected_parent = os.path.join(self.target, os.path.dirname(relative))
            if os.readlink('/proc/self/fd/{}'.format(parent)) != expected_parent.rstrip('/'):
                raise ValueError('Target parent moved before unlink')
            os.unlink(name, dir_fd=parent)
        finally:
            os.close(parent)
