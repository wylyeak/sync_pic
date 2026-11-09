import unittest
from contextlib import nullcontext
from unittest.mock import Mock
from file_sync import Controller


class BatteryTelemetryTests(unittest.TestCase):
    def controller(self):
        ctrl = Controller.__new__(Controller)
        ctrl.device = Mock(return_value=nullcontext())
        ctrl.phone_call = Mock(return_value={'level': 8, 'charging': True,
                                             'power_connected': True, 'status': 'charging'})
        ctrl.c = {'mapping': {'min_battery_percent': 10}}
        ctrl.db = Mock()
        ctrl.notify = Mock()
        ctrl.publish = Mock()
        return ctrl

    def test_failed_battery_observation_does_not_interrupt_controller(self):
        ctrl = self.controller()
        ctrl.phone_call.side_effect = RuntimeError('offline')
        ctrl.refresh_battery()
        ctrl.publish.assert_called_once_with('battery', state='error', error='RuntimeError')

    def test_polls_are_bounded_and_have_success_timestamp(self):
        ctrl = self.controller()
        ctrl.refresh_battery()
        ctrl.refresh_battery()
        ctrl.phone_call.assert_called_once_with('battery')
        self.assertGreater(ctrl.publish.call_args.kwargs['measured_at'], 0)
        self.assertEqual(ctrl.publish.call_args.kwargs['level'], 8)

    def test_mapping_boundary_and_charging(self):
        for level, allowed in [(0, False), (9, False), (10, True), (100, True)]:
            with self.subTest(level=level):
                ctrl = self.controller()
                ctrl.phone_call.return_value['level'] = level
                self.assertEqual(ctrl.mapping_allowed(), allowed)

    def test_mapping_unknown_blocks_and_recovers(self):
        ctrl = self.controller()
        ctrl.phone_call.side_effect = RuntimeError('offline')
        self.assertFalse(ctrl.mapping_allowed())
        ctrl.next_battery_check = 0
        ctrl.phone_call.side_effect = None
        ctrl.phone_call.return_value['level'] = 10
        self.assertTrue(ctrl.mapping_allowed())

    def test_low_battery_preserves_mapping_candidates(self):
        ctrl = self.controller()
        ctrl.round = {'candidates': [{'source_path': 'photo.jpg'}], 'members': {}}
        ctrl.stop = Mock()
        ctrl.stop.is_set.return_value = False
        ctrl.save = Mock()
        self.assertFalse(ctrl.map_round())
        self.assertEqual(ctrl.round['candidates'], [{'source_path': 'photo.jpg'}])
        self.assertEqual(ctrl.round['members'], {})
        ctrl.save.assert_called_once_with()

    def test_custom_threshold_and_notice(self):
        ctrl = self.controller()
        ctrl.c['mapping']['min_battery_percent'] = 20
        ctrl.phone_call.return_value['level'] = 19
        self.assertFalse(ctrl.mapping_allowed())
        self.assertIn('20%', ctrl.notify.call_args.args[1])
        ctrl.next_battery_check = 0
        ctrl.phone_call.return_value['level'] = 20
        self.assertTrue(ctrl.mapping_allowed())
        ctrl.db.execute.assert_called_once_with(
            'DELETE FROM controller_notifications WHERE event_key=%s', ('mapping-low-battery',))

    def test_unknown_battery_does_not_rearm_notice(self):
        ctrl = self.controller()
        ctrl.mapping_battery = None
        ctrl.battery_notice()
        ctrl.db.execute.assert_not_called()
        ctrl.notify.assert_not_called()

    def test_notice_failure_does_not_disable_low_battery_gate(self):
        ctrl = self.controller()
        ctrl.notify.side_effect = RuntimeError('offline')
        self.assertFalse(ctrl.mapping_allowed())

    def test_notice_persisted_until_recovery_then_can_send_again(self):
        from unittest.mock import patch
        ctrl = self.controller()
        ctrl.c['bark'] = dict(enabled=True, retries=0, timeout=1, error_interval=3600)
        ctrl.key = 'test-key'
        sent = {}
        ctrl.db.query.side_effect = lambda sql, args: ([{'sent_at': sent[args[0]]}] if args[0] in sent else [])
        def execute(sql, args):
            if sql.startswith('REPLACE'):
                sent[args[0]] = args[1]
            else:
                sent.pop(args[0], None)
        ctrl.db.execute.side_effect = execute
        ctrl.notify = Controller.notify.__get__(ctrl)
        ctrl.mapping_battery = 9
        with patch('file_sync.requests.post') as post:
            post.return_value.json.return_value = {'code': 200}
            ctrl.battery_notice()
            ctrl.battery_notice()
            del ctrl.battery_notice_active  # Simulate a restart; DB record remains.
            ctrl.battery_notice()
            self.assertEqual(post.call_count, 1)
            ctrl.mapping_battery = 10
            ctrl.battery_notice()
            ctrl.mapping_battery = 9
            ctrl.battery_notice()
            self.assertEqual(post.call_count, 2)

    def test_failed_push_retried_next_observation(self):
        from unittest.mock import patch
        ctrl = self.controller()
        ctrl.c['bark'] = dict(enabled=True, retries=0, timeout=1, error_interval=3600)
        ctrl.key = 'test-key'
        ctrl.db.query.return_value = []
        ctrl.notify = Controller.notify.__get__(ctrl)
        ctrl.mapping_battery = 9
        with patch('file_sync.requests.post', side_effect=RuntimeError('offline')) as post:
            ctrl.battery_notice()
            ctrl.battery_notice()
            self.assertEqual(post.call_count, 2)
            ctrl.db.execute.assert_not_called()
