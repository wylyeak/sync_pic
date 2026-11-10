import importlib.util
from pathlib import Path
import tempfile
import unittest
import live_config

spec = importlib.util.spec_from_file_location('migration', Path(__file__).resolve().parents[1] / 'scripts/migrate-controller-settings.py')
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)

class MigrationTests(unittest.TestCase):
    def test_migration_keeps_effective_values_secrets_comments_and_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = root / 'controller/config.toml'
            legacy = root / 'state/settings/controller.toml'
            original.parent.mkdir()
            legacy.parent.mkdir(parents=True)
            original.write_text('# private original\n[mapping]\nmin_battery_percent=10 # pause\n[mysql]\npassword="private-secret"\n')
            legacy.write_text('[mapping]\nmin_battery_percent=15\nresume_battery_percent=25\n')
            before = original.read_bytes()
            self.assertTrue(migration.migrate(root, root / 'backup'))
            self.assertFalse(legacy.exists())
            self.assertEqual(live_config.read(original)[0]['mapping']['min_battery_percent'], 15)
            self.assertIn('private-secret', original.read_text())
            self.assertIn('min_battery_percent=15 # pause', original.read_text())
            self.assertEqual((root / 'backup/controller-before-migration.toml').read_bytes(), before)
            self.assertFalse(migration.migrate(root, root / 'backup'))
