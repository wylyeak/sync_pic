import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from target_guard import TargetDirectory


def identity(path):
    s = os.lstat(path)
    return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns


class SourceProtectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source, self.target = self.root/'source', self.root/'target'
        self.source.mkdir()
        self.target.mkdir()
        self.original = self.source/'photo.jpg'
        self.original.write_bytes(b'original remains untouched')

    def test_target_hardlink_deleted_source_preserved(self):
        target = self.target/'photo.jpg'
        os.link(self.original, target)
        with TargetDirectory(self.source, self.target) as guard:
            guard.unlink('photo.jpg', identity(target))
        self.assertFalse(target.exists())
        self.assertEqual(self.original.read_bytes(), b'original remains untouched')

    def test_source_as_target_and_overlaps_are_rejected(self):
        for source,target in [(self.source,self.source),(self.source,self.root),
                              (self.root,self.target),(self.source,self.source/'nested'),
                              (self.source,Path('/photos/source/eva'))]:
            with self.subTest(target=target), self.assertRaises(ValueError):
                TargetDirectory(source,target)

    def test_symlink_root_and_parent_and_file_rejected(self):
        alias = self.root/'alias'
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(OSError):
            TargetDirectory(self.source,alias)
        (self.target/'escape').symlink_to(self.source,target_is_directory=True)
        (self.target/'file.jpg').symlink_to(self.original)
        with TargetDirectory(self.source,self.target) as guard:
            with self.assertRaises(OSError):
                guard.unlink('escape/photo.jpg',identity(self.original))
            with self.assertRaises(ValueError):
                guard.unlink('file.jpg',identity(self.original))
        self.assertTrue(self.original.exists())

    def test_root_replacement_after_guard_created_is_rejected(self):
        with TargetDirectory(self.source,self.target) as guard:
            self.target.rename(self.root/'old-target')
            self.target.symlink_to(self.source,target_is_directory=True)
            with self.assertRaises(ValueError):
                guard.unlink('photo.jpg',identity(self.original))
        self.assertTrue(self.original.exists())

    def test_path_traversal_and_metadata_rejected(self):
        with TargetDirectory(self.source,self.target) as guard:
            for path in ['../source/photo.jpg',str(self.original),'.stignore','.stfolder/marker','a/../b']:
                with self.subTest(path=path),self.assertRaises(ValueError):
                    guard.unlink(path,identity(self.original))

    def test_different_mount_alias_is_rejected(self):
        with patch('target_guard._mount_id',side_effect=[100,200]):
            with self.assertRaises(ValueError):
                TargetDirectory(self.source,self.target)

    def test_parent_replaced_with_source_symlink_is_rejected(self):
        parent=self.target/'nested'
        parent.mkdir()
        (parent/'photo.jpg').write_bytes(b'target')
        before=identity(parent/'photo.jpg')
        with TargetDirectory(self.source,self.target) as guard:
            guard.stat('nested/photo.jpg')
            parent.rename(self.target/'old-nested')
            parent.symlink_to(self.source,target_is_directory=True)
            with self.assertRaises(OSError):
                guard.unlink('nested/photo.jpg',before)
        self.assertTrue(self.original.exists())
