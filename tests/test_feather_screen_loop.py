## The product screen drawn by the renderer's frame loop.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import unittest

# Puts the plugins on the import path first.
from tests.test_feather_screen import FEATHER, Reactor, StatusObject
from tests.test_feather_screen_controller import ScenarioController
from tests.feather_render_test_helper import shown_actions
from tests.feather_typer_loop import TyperLoop


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

    def test_a_restarted_typer_shows_home_again(self):
        controller = home_controller()
        restarted = []

        def repaint():
            restarted.append(True)
            controller._render_screen(force=True)

        typer = TyperLoop(controller.renderer, {"exit_at": 1}, {}, restart_handler=repaint)
        self.addCleanup(typer.close)
        typer.start()
        controller._render_screen()

        typer.take(lambda events: restarted)

        self.assertTrue(typer.drew("192.168.2.124", launch=1))
        self.assertIn("nav.heat", shown_actions(controller._screen_root))


if __name__ == "__main__":
    unittest.main()
