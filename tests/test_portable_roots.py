import os
import subprocess
import sys
from pathlib import Path
import unittest


class PortableRootTests(unittest.TestCase):
    def run_guard(self, source, target, expression):
        env = dict(os.environ, PICSYNC_SOURCE=source, PICSYNC_TARGET=target)
        return subprocess.run([sys.executable, '-c', expression], env=env,
                              capture_output=True, text=True)

    def test_roots_are_explicitly_configurable_at_process_start(self):
        result = self.run_guard('/example/source', '/example/target',
            'from target_guard import PROTECTED_SOURCE, ALLOWED_TARGET; print(PROTECTED_SOURCE, ALLOWED_TARGET)')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '/example/source /example/target')

    def test_unsafe_root_configuration_is_rejected(self):
        for source in ('/', 'relative/path', '/example/../source'):
            with self.subTest(source=source):
                result = self.run_guard(source, '/example/target', 'import target_guard')
                self.assertNotEqual(result.returncode, 0)

    def test_toml_cannot_override_the_process_safety_boundary(self):
        import tempfile
        from configuration import load
        with tempfile.TemporaryDirectory() as d:
            path = Path(d)/'config.toml'
            path.write_text('[directories]\nsource="/other/source"\ntarget="/other/target"\n')
            with self.assertRaisesRegex(ValueError, 'safety boundaries'):
                load(path)
