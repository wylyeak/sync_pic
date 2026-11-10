import os
from pathlib import Path
import tempfile
import unittest
from PIL import Image
from photo_preview import open_image, PreviewError


class PhotoPreviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)

    def photo(self, name='photo.jpg', color='red'):
        path = self.root / name
        Image.new('RGB', (12, 8), color).save(path)
        return path

    def test_original_is_streamed_unchanged_without_server_conversion(self):
        path = self.photo()
        before = path.read_bytes()
        stream, size, mime, etag = open_image(self.root, path.name)
        with stream:
            self.assertEqual(stream.read(), before)
        self.assertEqual(size, len(before))
        self.assertEqual(mime, 'image/jpeg')
        self.assertTrue(etag.startswith('"'))
        self.assertEqual(path.read_bytes(), before)

    def test_heic_original_is_not_converted(self):
        from pillow_heif import register_heif_opener
        register_heif_opener()
        Image.new('RGB', (12, 8)).save(self.root/'photo.heic', format='HEIF')
        stream, size, mime, _ = open_image(self.root, 'photo.heic')
        with stream:
            self.assertEqual(stream.read(), (self.root/'photo.heic').read_bytes())
        self.assertEqual(mime, 'image/heic')

    def test_traversal_absolute_paths_and_symlinks_are_rejected(self):
        self.photo()
        for name in ('../photo.jpg', '/etc/passwd', 'a//b.jpg', './photo.jpg', 'a/../photo.jpg', '', '\x00'):
            with self.assertRaises(PreviewError):
                open_image(self.root, name)
        (self.root/'link.jpg').symlink_to(self.root/'photo.jpg')
        (self.root/'link-dir').symlink_to(self.root, target_is_directory=True)
        for name in ('link.jpg', 'link-dir/photo.jpg'):
            with self.assertRaises(PreviewError):
                open_image(self.root, name)

    def test_missing_non_image_and_svg_rejected(self):
        with self.assertRaises(PreviewError) as error:
            open_image(self.root, 'missing.jpg')
        self.assertEqual(error.exception.status, 404)
        for name, data in [('secret.jpg', b'private text'), ('script.svg', b'<svg onload="alert(1)"></svg>')]:
            (self.root/name).write_bytes(data)
            with self.assertRaises(PreviewError) as error:
                open_image(self.root, name)
            self.assertEqual(error.exception.status, 415)

    def test_etag_changes_and_named_pipe_does_not_block(self):
        self.photo()
        stream, _, _, before = open_image(self.root, 'photo.jpg');stream.close()
        self.photo(color='blue')
        stream, _, _, after = open_image(self.root, 'photo.jpg');stream.close()
        self.assertNotEqual(before, after)
        os.mkfifo(self.root/'pipe')
        with self.assertRaises(PreviewError):
            open_image(self.root, 'pipe')
