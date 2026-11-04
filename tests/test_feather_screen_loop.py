## The product screen drawn by the renderer's frame loop.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import os
import unittest

# Puts the plugins on the import path first.
from tests.test_feather_screen import FEATHER, Reactor, StatusObject
from tests.test_feather_screen_controller import ScenarioController
from tests.feather_render_test_helper import shown_actions
from tests.feather_typer_loop import TyperLoop
from feather_ui_test import runner as UI_TEST


def home_controller():
    controller = ScenarioController.__new__(ScenarioController)
    controller._last_dashboard = None
    controller.page = FEATHER.ScreenPage.IDLE_HOME
    controller.renderer = FEATHER.FeatherRenderer()
    controller.reactor = Reactor()
    controller.extruder = StatusObject({"temperature": 20.0, "target": 0.0})
    controller.heater_bed = StatusObject({"temperature": 21.0, "target": 0.0})
    controller.toolhead = StatusObject({"homed_axes": "xyz"})
    controller.network_status = {"mode": "ETHERNET", "ssid": "", "ip": "192.168.2.124"}
    controller.last_job_name = "NONE"
    controller._current_material = lambda: "PLA"
    controller._read_text = lambda _path: ""
    controller._refresh_local_timezone = lambda: None
    return controller


class ScreenLoopTest(unittest.TestCase):
    def test_the_loop_draws_home_and_its_updates_without_the_reactor(self):
        controller = home_controller()
        typer = TyperLoop(controller.renderer)
        self.addCleanup(typer.close)
        typer.start()

        with self.assertNoLogs(level="WARNING"):
            controller._render_screen()
            root = controller._screen_root
            self.assertTrue(root.frames.attached)
            self.assertTrue(typer.drew("192.168.2.124"))
            controller.extruder.status["temperature"] = 187.0
            controller._render_screen()
            self.assertTrue(typer.drew("187"))
            typer.take(lambda events: False, timeout=0.2)

        shown = shown_actions(root)
        for action in ("nav.menu", "nav.heat", "nav.network", "nav.move"):
            self.assertIn(action, shown)

    def test_the_ui_test_taps_the_buttons_the_loop_drew(self):
        controller = home_controller()
        typer = TyperLoop(controller.renderer)
        self.addCleanup(typer.close)
        typer.start()
        controller._render_screen()
        self.assertTrue(typer.drew("192.168.2.124"))
        typer.take(lambda events: False, timeout=0.2)
        tapped = []
        controller._start_touch_action = tapped.append
        run = UI_TEST.UITestRun(controller)

        run._tap("nav.menu")

        self.assertEqual(tapped, ["nav.menu"])
        # Home shows no back button.
        with self.assertRaisesRegex(RuntimeError, "absent"):
            run._tap("nav.back")
        self.assertEqual(tapped, ["nav.menu"])

    def test_the_loop_draws_the_heat_page(self):
        controller = home_controller()
        controller.page = FEATHER.ScreenPage.CONTROL_HEAT
        controller.heating_materials = ()
        controller._last_heat = None
        controller.fan = None
        typer = TyperLoop(controller.renderer)
        self.addCleanup(typer.close)
        typer.start()

        with self.assertNoLogs(level="WARNING"):
            controller._render_screen()
            controller.extruder.status["target"] = 215.0
            controller._render_screen()
            self.assertTrue(typer.drew("215"))
            typer.take(lambda events: False, timeout=0.2)

    def test_the_loop_draws_the_filament_material_page(self):
        controller = home_controller()
        controller.page = FEATHER.ScreenPage.FILAMENT_MATERIAL
        controller.feature_manager = FEATHER.LazyFeatureManager(controller, FEATHER.FEATURE_SPECS)
        controller.heating_materials = ("PLA", "PETG")
        controller.heating_profiles = {"PLA": (210, 60), "PETG": (240, 80)}
        controller.extruder.heater = controller.heater_bed
        typer = TyperLoop(controller.renderer)
        self.addCleanup(typer.close)
        typer.start()
        with self.assertNoLogs(level="WARNING"):
            controller._render_screen()
            # Each material's button with its preheat target under the name.
            self.assertTrue(typer.drew("PETG"))
            self.assertTrue(typer.drew("240C"))
            typer.take(lambda events: False, timeout=0.2)
        shown = shown_actions(controller._screen_root)
        self.assertIn("nav.back", shown)
        self.assertEqual(len([action for action in shown if action.startswith("filament.select.")]), 2)

    def test_the_loop_draws_the_filament_action_page_and_its_updates(self):
        controller = home_controller()
        controller.page = FEATHER.ScreenPage.FILAMENT_ACTION
        controller.feature_manager = FEATHER.LazyFeatureManager(controller, FEATHER.FEATURE_SPECS)
        controller.filament_material = "PETG"
        controller.filament_from_pause = False
        controller.extruder = StatusObject({"temperature": 200.0, "target": 240.0})
        controller.extruder.min_extrude_temp = 170.0
        typer = TyperLoop(controller.renderer)
        self.addCleanup(typer.close)
        typer.start()
        with self.assertNoLogs(level="WARNING"):
            controller._render_screen()
            self.assertTrue(typer.drew("HEATING"))
            self.assertTrue(typer.drew("FEED FILAMENT"))
            # The material row and the new temperature, written by the feature's update.
            controller.extruder.status["temperature"] = 239.0
            controller.feature_manager.get("filament").update(controller.reactor.monotonic())
            self.assertTrue(typer.drew("239 / 240C"))
            self.assertTrue(typer.drew("READY"))
            typer.take(lambda events: False, timeout=0.2)
        shown = shown_actions(controller._screen_root)
        self.assertIn("nav.back", shown)
        self.assertEqual(len([action for action in shown if action.startswith(("filament.load", "filament.purge"))]), 2)

    def test_the_loop_draws_the_printing_page_and_its_ready_preview(self):
        from tests.test_feather_print_preview import DeferredPreviewWorker, PreviewHelper, _controller, _write_gcode
        with PreviewHelper():
            path = _write_gcode()
            self.addCleanup(os.unlink, path)
            worker = DeferredPreviewWorker()
            controller = _controller(path, worker)
            controller.renderer = FEATHER.FeatherRenderer()
            typer = TyperLoop(controller.renderer)
            self.addCleanup(typer.close)
            typer.start()

            with self.assertNoLogs(level="WARNING"):
                controller._render_screen()
                self.assertTrue(typer.drew("PREVIEW"))
                # The loop arranged the page: what follows still finds the preview's geometry.
                controller._render_screen()
                controller.reactor.flush_callbacks()
                worker.finish(0)
                self.assertTrue(typer.drew("--batch image "))
                typer.take(lambda events: False, timeout=0.2)

        self.assertIn("print.cancel", shown_actions(controller._screen_root))

    def test_the_loop_draws_more_over_the_printing_page_with_abort(self):
        from tests.test_feather_print_preview import DeferredPreviewWorker, PreviewHelper, _controller, _write_gcode
        with PreviewHelper():
            path = _write_gcode()
            self.addCleanup(os.unlink, path)
            controller = _controller(path, DeferredPreviewWorker())
            controller.renderer = FEATHER.FeatherRenderer()
            # As the fixture's renderer: a print in progress, the header shows ABORT.
            controller.renderer.set_header_action("global.abort", "ABORT")
            typer = TyperLoop(controller.renderer)
            self.addCleanup(typer.close)
            typer.start()

            with self.assertNoLogs(level="WARNING"):
                controller._render_screen()
                self.assertTrue(typer.drew("PREVIEW"))
                controller._open_print_more()
                self.assertTrue(typer.drew("Change Filament"))
                typer.take(lambda events: False, timeout=0.2)
            shown = shown_actions(controller._screen_root)
            # Over the printing page, whose controls it covers, with the header's ABORT kept.
            self.assertTrue({"more.close", "more.filament", "more.exclude", "global.abort"} <= set(shown))
            self.assertNotIn("print.cancel", shown)
            controller._handle_more_action("more.close")
            typer.take(lambda events: False, timeout=0.2)
            self.assertIn("print.cancel", shown_actions(controller._screen_root))

    def test_a_restarted_typer_shows_home_again(self):
        controller = home_controller()
        restarted = []

        def handler():
            restarted.append(True)
            controller._renderer_restarted()

        typer = TyperLoop(controller.renderer, {"exit_at": 1}, {}, restart_handler=handler)
        self.addCleanup(typer.close)
        typer.start()
        controller._render_screen()

        typer.take(lambda events: restarted)

        self.assertTrue(typer.drew("192.168.2.124", launch=1))
        self.assertIn("nav.heat", shown_actions(controller._screen_root))

    def test_a_restarted_typer_shows_a_frozen_terminal_error_with_its_buttons(self):
        controller = home_controller()
        controller.touch_available = True
        restarted = []

        def handler():
            restarted.append(True)
            controller._renderer_restarted()

        typer = TyperLoop(controller.renderer, {}, {}, restart_handler=handler)
        self.addCleanup(typer.close)
        typer.start()
        controller._render_screen()
        controller._show_terminal_error("MCU shutdown", "shutdown", "firmware_restart")
        self.assertTrue(typer.drew("MCU shutdown"))

        controller.renderer.restart()
        typer.take(lambda events: restarted)

        self.assertTrue(typer.drew("MCU shutdown", launch=1))
        self.assertIn("error.firmware_restart", shown_actions(controller._screen_root))
        self.assertTrue(controller.renderer.output_frozen)


if __name__ == "__main__":
    unittest.main()
