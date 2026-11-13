# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from tests.test_forge_shaper import TestAxis, make_plugin, plugin, sample
from tests.test_shaper_console import Command, shaper_macros
from tests.test_toolhead_yield import BufferingHarness


class SweepingVibrationsTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.instance, self.settings, self.gcode, self.standard, self.objects = make_plugin(
            self.directory, os.environ.get("FORGE_SHAPER_HELPER", sys.executable))
        self.instance.printer.is_shutdown = lambda: False
        self.instance.reactor.NOW = 0.
        self.standard.hz_per_sec = 1.
        self.standard.accel_per_hz = 75.
        self.machine = BufferingHarness()
        self.head = self.objects["toolhead"] = self.machine.toolhead
        self.head.commanded_pos = [0., 0., 100., 0.]
        self.head.special_queuing_state = "Flushed"
        self.head.printer = self.instance.printer
        self.head.Coord = lambda *axes: axes
        self.bounds = dict(homed_axes="xyz", axis_minimum=(-125., -125., -10.),
                           axis_maximum=(125., 125., 230.))
        self.head.kin.get_status.side_effect = lambda _: self.bounds
        self.shaper = self.objects["input_shaper"]
        self.shaper.disable_shaping = lambda: setattr(self.shaper, "enabled", False)
        self.configured = {"kinematics": "corexy", "max_velocity": 600., "max_accel": 8700.}
        self.resonance_config = {"accel_per_hz": "75"}
        self.objects["configfile"].get_status = lambda _: {
            "config": {"resonance_tester": dict(self.resonance_config)},
            "settings": {"printer": dict(self.configured), "resonance_tester": {"accel_per_hz": 75.}}}
        disk = mock.patch.object(plugin, "disk_directory", return_value=str(self.directory))
        disk.start()
        self.addCleanup(disk.stop)

    def run_sweep(self, check=lambda: None):
        command = Command()
        self.head.wait_moves()
        self.head.dwell(.5)
        sweep = plugin.SweepingVibrations(self.standard, self.configured, command, 10., self.standard.accel_per_hz)
        return sweep.run(self.head, TestAxis("x"), command, check)

    def stall_once(self, seconds, after):
        # Resume one pause late, as when another task blocks the reactor.
        # Timers due meanwhile, including the low-buffer flush, still run.
        pause = self.machine.reactor.pause

        def late(waketime):
            moves = self.machine.moves
            if moves and self.machine.now >= moves[0][0] + after:
                self.machine.reactor.pause = pause
                waketime = max(waketime, self.machine.now) + seconds
            return pause(waketime)

        self.machine.reactor.pause = late

    def assert_continuous(self, moves):
        for previous, current in zip(moves, moves[1:]):
            time, x, velocity = self.end_of_move(previous)
            self.assertAlmostEqual(time, current[0])
            self.assertAlmostEqual(x, current[4])
            self.assertAlmostEqual(velocity, current[7] * current[-3])

    @staticmethod
    def end_of_move(move):
        time, up, coast, down, x, _, _, dx, _, _, v0, peak, accel = move
        v1 = peak - accel * down
        distance = (v0 + peak) * up / 2 + peak * coast + (peak + v1) * down / 2
        return time + up + coast + down, x + dx * distance, dx * v1

    def test_one_pulse_pair_matches_upstream_acceleration_and_brakes_to_rest(self):
        self.standard.freq_end = 5.
        start, end = self.run_sweep()
        first, second, brake = self.machine.moves
        # At 5 Hz each quarter lasts 50 ms. The first two acceleration
        # values are 375 + 400 and -375 + 400 mm/s^2.
        self.assertAlmostEqual(first[0], start)
        self.assertAlmostEqual(first[1], .05)
        self.assertAlmostEqual(first[-1], 775.)
        self.assertAlmostEqual(second[0], start + .05)
        self.assertAlmostEqual(second[1], .05)
        self.assertAlmostEqual(second[-1], 25.)
        # Brake from 38.75 + 1.25 = 40 mm/s at the sweep's own peak
        # acceleration, 75 * 5 + 400 = 775 mm/s^2.
        self.assertAlmostEqual(brake[0], start + .1)
        self.assertAlmostEqual(brake[-2], 40.)
        self.assertAlmostEqual(brake[-1], 775.)
        self.assertAlmostEqual(end, start + .1 + 40. / 775.)
        stop_time, stop_x, stop_v = self.end_of_move(brake)
        self.assertAlmostEqual(end, stop_time)
        self.assertAlmostEqual(stop_v, 0.)
        self.assertAlmostEqual(self.head.get_position()[0], stop_x)
        self.assertTrue(self.shaper.enabled)

    def test_command_uses_explicit_config_or_60_despite_stock_injected_75(self):
        self.standard.freq_end = 5.
        for configured, first_acceleration in ((None, 700.), ("60", 700.), ("75", 775.), ("90", 850.)):
            with self.subTest(configured=configured):
                self.resonance_config.clear()
                if configured is not None:
                    self.resonance_config["accel_per_hz"] = configured
                capture = types.SimpleNamespace(samples=0, error=None, close=mock.Mock(),
                    finish=mock.Mock(side_effect=RuntimeError("Capture complete")))
                with mock.patch.object(plugin, "CaptureClient", return_value=capture):
                    with self.assertRaisesRegex(RuntimeError, "Capture complete"):
                        self.instance.cmd_CALIBRATE(Command(AXIS="x"))
                self.assertAlmostEqual(self.machine.moves[-3][-1], first_acceleration)
                self.assertAlmostEqual(self.machine.moves[-3][1], .05)
                self.assertEqual(self.standard.accel_per_hz, 75.)
                self.assertTrue(self.shaper.enabled)
                self.assertFalse(self.instance.busy)

    def test_standard_command_keeps_its_own_75_default(self):
        self.resonance_config.clear()
        capture = types.SimpleNamespace(samples=0, error=None, close=mock.Mock())
        with mock.patch.object(plugin, "CaptureClient", return_value=capture), mock.patch.object(
                self.standard, "run_test", side_effect=RuntimeError("Standard reached")) as pulses:
            with self.assertRaisesRegex(RuntimeError, "Standard reached"):
                self.instance.cmd_CALIBRATE(Command(AXIS="x", SWEEP="0"))
        pulses.assert_called_once()
        self.assertEqual(self.standard.accel_per_hz, 75.)

    def test_full_default_sweep_keeps_time_position_and_velocity_continuous(self):
        start, end = self.run_sweep()
        self.assertAlmostEqual(end - start, 95., delta=.2)
        self.assert_continuous(self.machine.moves)
        self.assertAlmostEqual(self.end_of_move(self.machine.moves[-1])[2], 0.)
        self.assertGreater(len(self.machine.pauses), 1)
        # The first low-frequency phase flips from +400 to -400 inside
        # a high-frequency pulse, 212.132 ms from the start.
        phase = next(i for i, move in enumerate(self.machine.moves) if abs(move[0] - start - .212132034) < 1e-7)
        before, after = self.machine.moves[phase - 1:phase + 1]
        signed_accel = lambda move: move[7] * move[-1] * (1 if move[1] else -1)
        self.assertAlmostEqual(signed_accel(after) - signed_accel(before), -800.)

    def test_invalid_flag_motion_limits_and_clearance_reject_before_capture(self):
        # The default sweep peaks at 100 Hz * 75 + 400 = 7900 mm/s^2.
        cases = (
            ({}, "SWEEP", {"SWEEP": "2"}),
            ({"max_velocity": 35.}, "configured velocity or acceleration limit", {}),
            ({"max_accel": 7000.}, "configured velocity or acceleration limit", {}),
            ({"homed_axes": "xy"}, "Home all axes", {}),
            ({"axis_maximum": (125., 4., 230.)}, "Not enough travel", {}),
        )
        for overrides, error, params in cases:
            with self.subTest(error=error, overrides=overrides):
                self.configured.update(max_velocity=overrides.get("max_velocity", 600.),
                                       max_accel=overrides.get("max_accel", 8700.))
                for key in ("homed_axes", "axis_maximum"):
                    if key in overrides:
                        self.bounds[key] = overrides[key]
                with mock.patch.object(plugin, "CaptureClient") as capture:
                    with self.assertRaisesRegex(RuntimeError, error):
                        self.instance.cmd_CALIBRATE(Command(**params))
                capture.assert_not_called()
                self.assertEqual(self.machine.moves, [])
                # A rejected parameter never starts a job; any later rejection
                # still ends it by powering the motors down.
                self.assertEqual(self.gcode.commands, [] if params else ["M84"])
                self.assertTrue(self.shaper.enabled)
                self.bounds.update(homed_axes="xyz", axis_maximum=(125., 125., 230.))
                self.gcode.commands.clear()

    def test_runtime_acceleration_limit_below_the_peak_does_not_limit_the_sweep(self):
        # A print that ended with M204 S5000 leaves this runtime limit behind.
        self.head.max_velocity, self.head.max_accel = 300., 5000.
        with mock.patch.object(plugin, "CaptureClient", side_effect=RuntimeError("Capture reached")):
            with self.assertRaisesRegex(RuntimeError, "Capture reached"):
                self.instance.cmd_CALIBRATE(Command(AXIS="x"))
        self.assertEqual(self.head.get_max_velocity(), (300., 5000.))

        start, end = self.run_sweep()
        first, second = self.machine.moves[:2]
        self.assertAlmostEqual(first[0], start)
        self.assertAlmostEqual(first[-1], 775.)
        self.assertAlmostEqual(second[-1], 25.)
        self.assertGreater(max(move[-1] for move in self.machine.moves), 5000.)
        self.assertAlmostEqual(end - start, 95., delta=.2)
        self.assertEqual(self.head.get_max_velocity(), (300., 5000.))
        self.assertEqual(self.head.requested_accel_to_decel, 5000.)

    def test_reactor_stall_shorter_than_the_margin_keeps_the_sweep_running(self):
        self.stall_once(.75, after=10.)
        start, end = self.run_sweep()
        self.assertAlmostEqual(end - start, 95., delta=.2)
        self.assert_continuous(self.machine.moves)
        self.assertAlmostEqual(self.end_of_move(self.machine.moves[-1])[2], 0.)
        self.assertTrue(self.shaper.enabled)

    def test_reactor_stall_that_exhausts_the_buffer_aborts_at_a_complete_stop(self):
        self.stall_once(3., after=10.)
        with self.assertRaisesRegex(RuntimeError, "Motion buffer ran dry"):
            self.run_sweep()
        moves = self.machine.moves
        self.assertLess(self.end_of_move(moves[-1])[0] - moves[0][0], 20.)
        self.assert_continuous(moves)
        _, stop_x, stop_v = self.end_of_move(moves[-1])
        self.assertAlmostEqual(stop_v, 0.)
        self.assertAlmostEqual(self.head.get_position()[0], stop_x)
        self.assertTrue(self.shaper.enabled)

    def test_clearance_includes_braking_after_the_last_pulse(self):
        # At 5 Hz the pulses end 0.96875 + 1.96875 = 2.9375 mm from the
        # point at 40 mm/s; braking at 775 mm/s^2 adds 40^2 / 1550 = 1.03 mm.
        self.standard.freq_end = 5.
        self.bounds["axis_maximum"] = (3.5, 125., 230.)
        with self.assertRaisesRegex(RuntimeError, "Not enough travel"):
            self.instance.cmd_CALIBRATE(Command(AXIS="x"))
        self.assertEqual(self.machine.moves, [])

    def test_invalid_sweep_settings_name_the_offending_setting(self):
        cases = (("freq_start", float("nan"), "FREQ_START"), ("freq_end", float("nan"), "FREQ_END"),
                 ("hz_per_sec", .05, "HZ_PER_SEC"))
        for name, value, error in cases:
            with self.subTest(setting=name):
                original = getattr(self.standard, name)
                setattr(self.standard, name, value)
                with mock.patch.object(plugin, "CaptureClient") as capture:
                    with self.assertRaisesRegex(RuntimeError, error):
                        self.instance.cmd_CALIBRATE(Command())
                setattr(self.standard, name, original)
                capture.assert_not_called()
                self.assertEqual(self.machine.moves, [])
                self.assertEqual(self.gcode.commands, ["M84"])
                self.gcode.commands.clear()

    def test_invalid_explicit_acceleration_is_rejected_before_capture(self):
        self.resonance_config["accel_per_hz"] = "nan"
        with mock.patch.object(plugin, "CaptureClient") as capture:
            with self.assertRaisesRegex(RuntimeError, "accel_per_hz"):
                self.instance.cmd_CALIBRATE(Command())
        capture.assert_not_called()
        self.assertEqual(self.machine.moves, [])

    def test_probe_point_below_configured_safe_z_is_rejected(self):
        self.standard.get_start_test_points = lambda: [(0., 0., 15.)]
        self.objects["mod_params"] = types.SimpleNamespace(variables={"safe_z": 20.})
        with self.assertRaisesRegex(RuntimeError, "at or above Safe Z"):
            self.instance.cmd_CALIBRATE(Command())
        self.assertEqual(self.machine.moves, [])

    def test_toolhead_travels_sideways_only_at_the_higher_of_current_and_test_heights(self):
        point = (0., 0., 100.)
        self.standard.get_start_test_points = lambda: [point]
        for sweep in ("0", "1"):
            for current_z in (2., 150.):
                with self.subTest(sweep=sweep, current_z=current_z):
                    del self.machine.moves[:]
                    self.head.commanded_pos = [20., 30., current_z, 0.]
                    with mock.patch.object(plugin, "CaptureClient", side_effect=RuntimeError("Capture reached")):
                        with self.assertRaisesRegex(RuntimeError, "Capture reached"):
                            self.instance.cmd_CALIBRATE(Command(SWEEP=sweep, AXIS="x"))
                    sideways = [move for move in self.machine.moves if move[7] or move[8]]
                    self.assertTrue(sideways)
                    for move in sideways:
                        # Trapq rows: start z at index 6, z direction at index 9.
                        self.assertEqual(move[9], 0.)
                        self.assertGreaterEqual(move[6], max(current_z, point[2]) - 1e-6)
                    for actual, expected in zip(self.head.get_position(), point):
                        self.assertAlmostEqual(actual, expected)

    def test_cancellation_drains_a_rest_boundary_and_restores_shaping(self):
        def cancel_when_buffering(_):
            if self.machine.moves:
                self.gcode.handlers["FORGE_SHAPER_CANCEL"](Command())
                return self.machine.reactor.NEVER
            return self.machine.now + .05

        self.machine.reactor.register_timer(cancel_when_buffering, self.machine.now + .05)
        capture = types.SimpleNamespace(samples=0, error=None, close=mock.Mock())
        with mock.patch.object(plugin, "CaptureClient", return_value=capture):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                self.instance.cmd_CALIBRATE(Command(SWEEP="1", AXIS="x"))
        self.assertEqual(self.instance.state, "cancelled")
        self.assertFalse(self.instance.busy)
        end, stop_x, stop_v = self.end_of_move(self.machine.moves[-1])
        self.assertLess(end - self.machine.moves[0][0], 3.)
        self.assertAlmostEqual(stop_v, 0.)
        self.assertAlmostEqual(self.head.get_position()[0], stop_x)
        self.assertGreaterEqual(self.machine.now, end)
        self.assertTrue(self.shaper.enabled)
        self.assertEqual(self.head.get_max_velocity(), (600., 8700.))
        self.assertEqual(self.head.requested_accel_to_decel, 5000.)
        self.assertEqual(self.gcode.commands, ["M84"])
        self.assertEqual(self.instance.get_history(), [])
        capture.close.assert_called_once()

    def test_shutdown_while_draining_the_last_batch_invalidates_the_measurement(self):
        self.standard.freq_end = 5.
        self.instance.printer.is_shutdown = lambda: bool(
            self.machine.moves and self.machine.now >= self.machine.moves[0][0])
        with self.assertRaisesRegex(RuntimeError, "Printer shut down"):
            self.run_sweep()
        self.assertTrue(self.shaper.enabled)

    def test_both_macros_use_sweeping_vibrations_by_default_and_accept_opt_out(self):
        self.standard.get_start_test_points = lambda: [(0., 0., 0.)]
        macro, _ = shaper_macros(self.instance, self.gcode, self.objects)
        for name in ("FORGE_SHAPER", "FORGE_SHAPER_CALIBRATE"):
            with self.subTest(command=name), mock.patch.object(plugin, "CaptureClient") as capture:
                with self.assertRaisesRegex(RuntimeError, "at or above Safe Z"):
                    macro.run(name)
                self.instance.printer.send_event("gcode:command_error")
                capture.assert_not_called()
            with self.subTest(command=name + " SWEEP=0"), mock.patch.object(
                    plugin, "CaptureClient", side_effect=RuntimeError("Standard capture reached")) as capture:
                with self.assertRaisesRegex(RuntimeError, "Standard capture reached"):
                    macro.run(name + " SWEEP=0 AXIS=x")
                self.instance.printer.send_event("gcode:command_error")
                capture.assert_called_once()

    @unittest.skipUnless(os.environ.get("FORGE_SHAPER_HELPER"), "Set FORGE_SHAPER_HELPER for native analysis")
    def test_console_macro_saves_a_sweeping_result_and_applies_its_recommendation(self):
        self.standard.freq_end = 9.
        self.head.commanded_pos = [20., 0., 2., 0.]
        chip = self.objects["resonance_tester"].accel_chips[0][1]
        restore = self.shaper.enable_shaping

        def finish_sensor_read():
            start = next(move[0] for move in self.machine.moves if move[8])
            for offset in range(0, 2400, 80):
                chip.api_dump.client.send({"params": {"data": [
                    (start + sample(i)[0],) + sample(i)[1:] for i in range(offset, offset + 80)]}})
                self.instance.reactor.pause(self.instance.reactor.monotonic() + .001)
            restore()

        self.shaper.enable_shaping = finish_sensor_read
        macro, messages = shaper_macros(self.instance, self.gcode, self.objects)
        macro.run("FORGE_SHAPER_CALIBRATE AXIS=y")
        first_xy = next(i for i, move in enumerate(self.machine.moves) if move[7] or move[8])
        self.assertGreater(first_xy, 0)
        self.assertTrue(all(move[9] > 0. for move in self.machine.moves[:first_xy]))
        self.assertGreaterEqual(self.machine.moves[first_xy][6], 10.)
        self.assertTrue(any("Y: recommended" in message for message in messages))
        record = self.instance.record
        self.assertEqual(record.results["y"]["samples"], 2400)
        self.assertEqual(record.results["y"]["max_freq"], 13.5)
        self.assertEqual(self.instance.history.load(record.id)[0], record.results)
        self.assertEqual(self.instance.get_history()[0]["id"], record.id)
        self.assertEqual(self.settings, [('input_shaper', 'shaper_type_y', 'mzv'),
                                         ('input_shaper', 'shaper_freq_y', '42.6')])
        # Applied after the sweep re-enabled shaping, so Klipper keeps the new parameters.
        self.assertEqual(self.shaper.live, {'y': {'type': 'mzv', 'freq': 42.6}})
        self.assertTrue(self.shaper.enabled)
