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
