import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image
from photo_metadata import capture_time, creation_time


class CaptureTimeTests(unittest.TestCase):
    def photo(self, folder, stamp=None, offset=None):
        path = Path(folder, 'photo.jpg')
        exif = Image.Exif()
        if stamp is not None:
            exif[34665] = {36867: stamp, **({36881: offset} if offset else {})}
        Image.new('RGB', (2, 2)).save(path, exif=exif)
        return path

    def test_camera_wall_time_and_offset_are_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(capture_time(self.photo(folder, '2020:02:29 13:14:15')), '2020-02-29T13:14:15')
            self.assertEqual(capture_time(self.photo(folder, '2020:02:29 13:14:15', '+08:00')), '2020-02-29T13:14:15+08:00')

    def test_heic_camera_time(self):
        with tempfile.TemporaryDirectory() as folder:
            jpeg = self.photo(folder, '2020:02:29 13:14:15')
            path = Path(folder, 'photo.heic')
            with Image.open(jpeg) as image:
                image.save(path, format='HEIF', exif=image.getexif())
            self.assertEqual(capture_time(path), '2020-02-29T13:14:15')

    def test_missing_invalid_and_non_images_are_unknown(self):
        with tempfile.TemporaryDirectory() as folder:
            self.assertIsNone(capture_time(self.photo(folder)))
            self.assertIsNone(capture_time(self.photo(folder, '2023:02:29 12:00:00')))
            path = Path(folder, 'broken.jpg')
            path.write_bytes(b'not an image')
            self.assertIsNone(capture_time(path))
            self.assertIsNone(capture_time(Path(folder, 'missing.jpg')))

    def test_symlink_is_not_followed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.photo(folder, '2020:02:29 13:14:15')
            link = Path(folder, 'link.jpg')
            link.symlink_to(path)
            self.assertIsNone(capture_time(link))

    def test_birthtime_is_independent_of_mtime(self):
        with tempfile.TemporaryDirectory() as folder:
            path = self.photo(folder)
            before = creation_time(path)
            import os
            os.utime(path, (1, 1))
            self.assertEqual(creation_time(path), before)

    def test_unavailable_creation_does_not_use_ctime(self):
        with patch('os.stat', return_value=type('Stat', (), {'st_ctime': 123})()), patch('ctypes.CDLL') as libc:
            libc.return_value.statx = None
            self.assertIsNone(creation_time('unused'))
