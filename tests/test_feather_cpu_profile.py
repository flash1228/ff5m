## Lifecycle contracts for bounded on-printer CPU profiling.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import cProfile
import json
import pathlib
import pstats
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock


ROOT = pathlib.Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / ".py/klipper/plugins"))

from feather_ui_test.feature import UITestFeature
from feather_ui_test.profiling import CPUProfile, MAX_PROFILE_SECONDS
from ff5m_ui.print_state import PrintState
from feather.features.manager import LazyFeatureManager
import feather_screen


class Reactor:
    NEVER = float("inf")

    def __init__(self):
        self.timers = []

    def monotonic(self):
        return 100.0

    def register_timer(self, callback, waketime):
        timer = SimpleNamespace(callback=callback, waketime=waketime)
        self.timers.append(timer)
        return timer

    def update_timer(self, timer, waketime):
        timer.waketime = waketime

    def unregister_timer(self, timer):
        self.timers.remove(timer)


def command(values=None):
    responses = []
    return SimpleNamespace(get=(values or {}).get, error=RuntimeError,
                           respond_info=responses.append, responses=responses)


class CPUProfileTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = pathlib.Path(temporary.name)
        self.reactor = Reactor()
        self.safe = mock.Mock(return_value=True)
        self.profile = CPUProfile(self.reactor, self.safe, str(self.root))
        self.addCleanup(self.profile.stop, save=False)

    def assert_stopped(self):
        self.assertFalse(self.profile.running)
        self.assertIsNone(sys.getprofile())
        self.assertEqual(self.reactor.timers, [])
        # Newer hosts use sys.monitoring; a new standard profiler must also be available.
        probe = cProfile.Profile()
        probe.enable()
        probe.disable()

    def test_standard_stats_are_saved_and_stop_is_idempotent(self):
        self.profile.start(2)

        def known_leaf():
            return sum(range(100))

        self.assertEqual(known_leaf(), 4950)
        result = self.profile.stop()
        self.assert_stopped()
        self.assertTrue(result["saved"])
        directory = pathlib.Path(result["directory"])
        stats = pstats.Stats(str(directory / "profile.pstats"))
        leaf = next(value for key, value in stats.stats.items() if key[2] == "known_leaf")
        self.assertEqual(leaf[1], 1)
        rows = json.loads((directory / "functions.json").read_text())
        self.assertTrue(any(row["function"] == "known_leaf" for row in rows))
        self.assertEqual(json.loads((directory / "metadata.json").read_text())["clock"], "thread_cpu")
        self.assertIs(self.profile.stop(), result)

    def test_deadline_saves_and_releases_hook_and_timer(self):
        self.profile.start(2)
        timer = self.reactor.timers[0]
        self.assertEqual(timer.callback(timer.waketime), self.reactor.NEVER)
        self.assert_stopped()
        self.assertEqual(self.profile.result["reason"], "duration")
        self.assertTrue(self.profile.result["saved"])

    def test_unsafe_deadline_cancels_without_artifact_io(self):
        self.profile.start(2)
        self.safe.return_value = False
        timer = self.reactor.timers[0]
        timer.callback(timer.waketime)
        self.assert_stopped()
        self.assertFalse(self.profile.result["saved"])
        self.assertEqual(list(pathlib.Path(self.profile.directory).iterdir()), [])

    def test_failed_safety_query_also_releases_profiler(self):
        self.profile.start(2)
        self.safe.side_effect = RuntimeError("status unavailable")
        self.reactor.timers[0].callback(102.0)
        self.assert_stopped()
        self.assertEqual(self.profile.result["reason"], "safety_check_failed")

    def test_invalid_duration_and_unsafe_start_have_no_effect(self):
        for duration in (0, -1, .5, float("nan"), float("inf"), MAX_PROFILE_SECONDS + 1):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                self.profile.start(duration)
            self.assert_stopped()
        self.safe.return_value = False
        with self.assertRaisesRegex(RuntimeError, "idle"):
            self.profile.start(2)
        self.assertEqual(list(self.root.iterdir()), [])
        self.assert_stopped()

    def test_foreign_profiler_is_preserved(self):
        def foreign(*args):
            pass
        sys.setprofile(foreign)
        try:
            with self.assertRaisesRegex(RuntimeError, "already running"):
                self.profile.start(2)
            self.assertIs(sys.getprofile(), foreign)
        finally:
            sys.setprofile(None)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_another_standard_profiler_is_preserved(self):
        foreign = cProfile.Profile()
        foreign.enable()
        try:
            with self.assertRaises((RuntimeError, ValueError)):
                self.profile.start(2)

            def foreign_leaf():
                return 7

            self.assertEqual(foreign_leaf(), 7)
        finally:
            foreign.disable()
        entries = pstats.Stats(foreign).stats
        self.assertEqual(next(value[1] for key, value in entries.items() if key[2] == "foreign_leaf"), 1)
        self.assertFalse(self.profile.running)
        self.assertEqual(self.reactor.timers, [])

    def test_partly_enabled_profiler_is_disabled_when_start_fails(self):
        real = cProfile.Profile()

        def broken_enable():
            real.enable()
            raise RuntimeError("enable failed")

        broken = SimpleNamespace(enable=broken_enable, disable=real.disable)
        with mock.patch("cProfile.Profile", return_value=broken):
            with self.assertRaisesRegex(RuntimeError, "enable failed"):
                self.profile.start(2)
        self.assert_stopped()

    def test_dump_failure_is_reported_after_hook_cleanup(self):
        self.profile.start(2)
        with mock.patch("cProfile.Profile.dump_stats", side_effect=OSError("write failed")), \
                self.assertLogs("root", level="ERROR"):
            result = self.profile.stop()
        self.assert_stopped()
        self.assertFalse(result["saved"])
        self.assertEqual(result["error"], "OSError")

    def test_another_thread_cannot_stop_the_main_thread_profile(self):
        self.profile.start(2)
        errors = []

        def stop_elsewhere():
            try:
                self.profile.stop(save=False)
            except RuntimeError as error:
                errors.append(str(error))

        thread = threading.Thread(target=stop_elsewhere)
        thread.start()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertTrue(self.profile.running)

        def still_profiled():
            return 8

        self.assertEqual(still_profiled(), 8)
        result = self.profile.stop()
        rows = json.loads((pathlib.Path(result["directory"]) / "functions.json").read_text())
        self.assertTrue(any(row["function"] == "still_profiled" and row["calls"] == 1 for row in rows))
        self.assert_stopped()


class CPUProfileFacadeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = temporary.name
        self.host = SimpleNamespace(
            reactor=Reactor(), print_state=PrintState.IDLE, command_depth=0,
            _safety_print_active=mock.Mock(return_value=False),
            _safety_heaters_active=mock.Mock(return_value=False),
            _safety_motion_active=mock.Mock(return_value=False),
            pause_resume=SimpleNamespace(get_status=lambda eventtime: {"is_paused": False}),
        )
        self.feature = UITestFeature(self.host)
        self.addCleanup(self.feature.deactivate)

    def start(self):
        def factory(reactor, safe):
            return CPUProfile(reactor, safe, self.root)
        with mock.patch("feather_ui_test.profiling.CPUProfile", side_effect=factory):
            self.feature.start_cpu_profile(command(), "2")
        return self.feature.cpu_profile

    def test_profile_does_not_block_benchmark_input_or_theme_updates(self):
        profile = self.start()
        self.assertFalse(self.feature.running)
        self.assertFalse(self.feature.input_blocked)
        self.assertFalse(self.feature.theme_update_blocked)
        self.assertFalse(self.feature.blocks_action("nav.back"))
        self.assertTrue(self.feature.get_status()["cpu_profile"]["running"])
        with self.assertRaisesRegex(RuntimeError, "CPU profile"):
            self.feature.run(command(), "UI", "", 0)
        self.feature.deactivate()
        self.assertFalse(profile.running)
        self.assertFalse(profile.result["saved"])

    def test_heating_and_print_transition_cancel_before_artifact_io(self):
        profile = self.start()
        self.host._safety_heaters_active.return_value = True
        self.feature.update(101.0)
        self.assertFalse(profile.running)
        self.assertFalse(profile.result["saved"])
        self.host._safety_heaters_active.return_value = False
        profile = self.start()
        self.feature.on_print_state_changed(PrintState.IDLE, PrintState.PRINTING, "printing")
        self.assertFalse(profile.running)
        self.assertEqual(profile.result["reason"], "printer_state_changed")
        self.assertFalse(profile.result["saved"])

    def test_failed_start_does_not_publish_candidate(self):
        self.host._safety_heaters_active.return_value = True
        with self.assertRaisesRegex(RuntimeError, "idle"):
            self.start()
        self.assertIsNone(self.feature.cpu_profile)

    def test_manual_stop_still_cancels_when_safety_query_fails(self):
        profile = self.start()
        self.host._safety_print_active.side_effect = RuntimeError("status unavailable")
        self.feature.stop_cpu_profile(command())
        self.assertFalse(profile.running)
        self.assertEqual(profile.result["reason"], "safety_check_failed")

    def test_ui_test_finalization_prevents_profile_overlap(self):
        self.feature.current_run = SimpleNamespace(running=False, finalizing=True, deactivate=lambda: None)
        with self.assertRaisesRegex(RuntimeError, "UI test"):
            self.feature.start_cpu_profile(command(), "2")
        self.assertIsNone(self.feature.cpu_profile)

    def test_profile_stop_is_lazy_and_start_loads_only_when_requested(self):
        controller = feather_screen.FeatherScreen.__new__(feather_screen.FeatherScreen)
        controller.feature_manager = LazyFeatureManager(controller, feather_screen.FEATURE_SPECS)
        controller.cmd_FEATHER_UI_TEST(command({"ACTION": "PROFILE_STOP"}))
        self.assertIsNone(controller.feature_manager.peek("ui_test"))
        calls = []
        with mock.patch.object(UITestFeature, "start_cpu_profile", lambda self, *args: calls.append(args)):
            gcmd = command({"ACTION": "PROFILE_START", "DURATION": "4.5"})
            controller.cmd_FEATHER_UI_TEST(gcmd)
        self.assertIsNotNone(controller.feature_manager.peek("ui_test"))
        self.assertEqual(calls, [(gcmd, "4.5")])
