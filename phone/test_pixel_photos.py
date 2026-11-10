import unittest
from types import SimpleNamespace
from unittest.mock import patch
import xml.etree.ElementTree as ET
from pixel_photos import classify, backup_status, battery_status, Phone, PKG, PhotosNotResponding


class StatusTests(unittest.TestCase):
    def test_battery_percentage_and_charging_are_independent(self):
        result = battery_status('  USB powered: true\n  level: 30\n  scale: 50\n  status: 4\n')
        self.assertEqual(result['level'], 60)
        self.assertTrue(result['power_connected'])
        self.assertFalse(result['charging'])
        self.assertEqual(result['status'], 'not_charging')

    def test_battery_full_empty_and_invalid_readings(self):
        self.assertEqual(battery_status('level: 100\nscale: 100\nstatus: 5\n')['status'], 'full')
        self.assertEqual(battery_status('level: 0\nscale: 100\n')['level'], 0)
        for text in ['level: 10\nscale: 0\n', 'level: 101\nscale: 100\n', 'offline']:
            with self.subTest(text=text), self.assertRaises((ValueError, KeyError)):
                battery_status(text)

    def test_host_adb_uses_direct_client_and_preserves_serial(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='phone',
                                      adb_backend='host', adb_path='/safe/adb-client'))
        self.assertEqual(phone.prefix, ['/safe/adb-client', '-s', 'phone'])

    def test_container_backend_remains_available(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='phone',
                                      adb_backend='container', adb_path='/container/adb'))
        self.assertEqual(phone.prefix, ['docker', 'exec', 'pixel-web', '/container/adb', '-s', 'phone'])

    def test_states(self):
        for text, expected in [('正在备份 • 还剩 137 项内容', 'syncing'),
                               ('备份完成', 'complete'), ('Backup complete', 'complete'),
                               ('已暂停备份', 'blocked'), ('等待 Wi-Fi', 'blocked'),
                               ('备份已关闭', 'blocked'), ('设备过热 • 还剩 135 项内容', 'blocked'),
                               ('137 张照片', 'unknown')]:
            with self.subTest(text=text):
                self.assertEqual(classify([text])['state'], expected)

    def test_blockers_override_completion(self):
        self.assertEqual(classify(['备份完成', '已暂停备份'])['state'], 'blocked')

    def test_photo_title_cannot_report_completion(self):
        root = ET.fromstring('<hierarchy><node text="备份完成" clickable="false"/></hierarchy>')
        self.assertEqual(backup_status(root)['state'], 'unknown')

    def test_backup_card(self):
        root = ET.fromstring('<hierarchy><node clickable="true"><node text="备份"/><node text="正在备份 • 还剩 137 项内容"/></node></hierarchy>')
        self.assertEqual(backup_status(root)['state'], 'syncing')

    def test_dump_is_fresh_and_batched(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        xml = '<hierarchy><node package="' + PKG + '"/></hierarchy>'
        with patch.object(phone, 'adb', return_value=xml) as adb:
            phone.dump()
        adb.assert_called_once()
        command = adb.call_args.args[1]
        self.assertIn('rm -f', command)
        self.assertIn('test -s', command)
        self.assertIn('uiautomator dump --compressed', command)

    def test_menu_reuses_home_and_collapses_old_navigation(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        xml = '<hierarchy><node package="' + PKG + '" resource-id="' + PKG + ':id/og_bento_toolbar_close_button"/></hierarchy>'
        with patch.object(phone, 'adb', return_value=xml) as adb:
            phone.menu()
            phone.menu()
        for call in adb.call_args_list:
            if call.args[1] == 'dumpsys':
                continue
            command = call.args[1]
            # NEW_TASK | CLEAR_TOP | SINGLE_TOP; numeric flags work on Android 10.
            self.assertIn('-f 0x34000000', command)
            self.assertNotIn('force-stop', command)

    def test_menu_already_open_needs_one_call(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        xml = '<hierarchy><node package="' + PKG + '" resource-id="' + PKG + ':id/og_bento_toolbar_close_button"/></hierarchy>'
        with patch.object(phone, 'adb', return_value=xml) as adb:
            phone.menu()
        self.assertEqual(adb.call_count, 2)  # one-time stack inspection + UI dump

    def test_legacy_duplicate_homes_are_reset_once(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        stack = ('Hist #1: ' + PKG + '/.home.HomeActivity\n') * 2
        xml = '<hierarchy><node package="' + PKG + '" resource-id="' + PKG + ':id/og_bento_toolbar_close_button"/></hierarchy>'
        with patch.object(phone, 'adb', side_effect=[stack, xml, xml]) as adb:
            phone.menu()
            phone.menu()
        self.assertIn('-f 0x10008000', adb.call_args_list[1].args[1])
        self.assertIn('-f 0x34000000', adb.call_args_list[2].args[1])

    def test_other_app_is_rejected(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        with patch.object(phone, 'adb', return_value='<hierarchy><node package="other.app"/></hierarchy>'):
            with self.assertRaises(RuntimeError):
                phone.dump()

    def test_photos_anr_waits_once_and_reads_fresh_status(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        dialog = '<hierarchy><node package="android" resource-id="android:id/aerr_wait" enabled="true" clickable="true" bounds="[0,10][100,50]"/></hierarchy>'
        window = 'Window{123 u0 Application Not Responding: ' + PKG + '}'
        healthy = '<hierarchy><node package="' + PKG + '"/></hierarchy>'
        with patch.object(phone, 'adb', side_effect=[dialog, window, '', healthy]) as adb, patch('pixel_photos.time.sleep'):
            self.assertEqual(phone.dump().find('node').get('package'), PKG)
        self.assertEqual(adb.call_args_list[2].args, ('shell', 'input', 'tap', '50', '30'))

    def test_persistent_anr_stops_without_close_or_loop(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        dialog = '<hierarchy><node package="android" resource-id="android:id/aerr_wait" enabled="true" clickable="true" bounds="[0,10][100,50]"/></hierarchy>'
        window = 'Window{123 u0 Application Not Responding: ' + PKG + '}'
        with patch.object(phone, 'adb', side_effect=[dialog, window, '', dialog, window]) as adb, patch('pixel_photos.time.sleep'):
            with self.assertRaises(PhotosNotResponding):
                phone.dump()
        self.assertEqual(adb.call_count, 5)

    def test_other_app_anr_is_never_clicked(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        dialog = '<hierarchy><node package="android" resource-id="android:id/aerr_wait"/></hierarchy>'
        with patch.object(phone, 'adb', side_effect=[dialog, 'Application Not Responding: other.app}']) as adb:
            with self.assertRaises(RuntimeError):
                phone.dump()
        self.assertEqual(adb.call_count, 2)

    def test_release_does_not_run_when_backup_is_not_complete(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        with patch.object(phone, 'status', return_value={'state': 'syncing'}), \
             patch.object(phone, 'free_page') as page:
            self.assertEqual(phone.release_space(execute=True)['action'], 'skipped')
        page.assert_not_called()

    def test_no_items_is_a_confirmed_result_only_in_execute_mode(self):
        phone = Phone(SimpleNamespace(container='pixel-web', serial='test'))
        root = ET.fromstring('<hierarchy><node clickable="true"><node text="备份"/><node text="备份已完成"/></node></hierarchy>')
        with patch.object(phone, 'status', return_value={'state': 'complete'}), \
             patch.object(phone, 'adb'), patch.object(phone, 'menu', return_value=root), \
             patch.object(phone, 'free_page', return_value=(root, None)), \
             patch.object(phone, 'tap') as tap, patch('pixel_photos.time.sleep'):
            self.assertEqual(phone.release_space(execute=False)['action'], 'preview')
            self.assertEqual(phone.release_space(execute=True)['action'], 'nothing_to_free')
        tap.assert_not_called()


class ControllerLockTests(unittest.TestCase):
    def test_execute_is_rejected_before_any_phone_call(self):
        import sys
        from unittest.mock import mock_open
        from pixel_photos import main
        with patch.object(sys, 'argv', ['pixel_photos.py', 'free-up', '--execute', '--serial', 'TEST_SERIAL']), \
                patch('builtins.open', mock_open()), \
                patch('pixel_photos.fcntl.flock', side_effect=BlockingIOError), \
                patch('pixel_photos.Phone') as phone:
            with self.assertRaisesRegex(RuntimeError, '控制器运行中'):
                main()
        phone.assert_not_called()

if __name__ == '__main__':
    unittest.main()
