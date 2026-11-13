## Behavioral coverage for the component layout benchmark.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import pathlib
import sys
import unittest
from types import SimpleNamespace
from unittest import mock


PLUGINS = pathlib.Path(__file__).parents[1] / ".py" / "klipper" / "plugins"
sys.path.insert(0, str(PLUGINS))

from feather.features.benchmark import BenchmarkFeature  # noqa: E402
from feather.screen.pages.home import HomePagesMixin  # noqa: E402
from ff5m_ui.benchmark import layout_page  # noqa: E402
from ff5m_ui.benchmark.layout_page import LayoutRef  # noqa: E402
from ff5m_ui.benchmark.page import BenchmarkRef  # noqa: E402
from ff5m_ui.print_state import PrintState  # noqa: E402
from ff5m_ui.screen import ScreenPage  # noqa: E402
from ui import FeatherRenderer  # noqa: E402
from ui.engine import update_refs as engine_update_refs  # noqa: E402
from ui.font_metrics import get_font_metrics  # noqa: E402
from ui.render_receipts import RenderReceipt  # noqa: E402
from tests.feather_render_test_helper import (  # noqa: E402
    RenderCapture, refused_render)


class Reactor:
    NEVER = float("inf")

    def __init__(self):
        self.now = 0.0
        self.waketime = self.NEVER

    def monotonic(self):
        return self.now

    def register_timer(self, callback, when):
        self.waketime = when
        return callback

    def update_timer(self, callback, when):
        self.waketime = when


def benchmark_host(page):
    """A feature over a host whose paint is the screen's transaction: the
    content and its values from the feature, then one frame with the receipt."""
    host = SimpleNamespace(reactor=Reactor(), renderer=FeatherRenderer(), page=page, print_state=PrintState.IDLE)
    host._show_page = lambda shown: setattr(host, "page", shown)
    host.rendering = RenderCapture(host.renderer)
    feature = BenchmarkFeature(host)
    drawn = []

    def paint(receipt=None, receipt_phase="presented"):
        tree, title, back = feature.page_content(host.page)
        feature.write_page_values(host.page)
        if tree in drawn:
            commands = tree.update(host.renderer, tree.state)
        else:
            drawn.append(tree)
            commands = host.renderer.begin_page(title, back=back)
            commands += tree.draw(host.renderer, tree.state)
        return host.renderer.send(commands, receipt=receipt, receipt_phase=receipt_phase)

    host._paint_page_values = paint
    feature.initialize()
    return feature, host


class ComponentBenchmarkTest(unittest.TestCase):
    def feature(self, page=ScreenPage.COMPONENT_BENCHMARK):
        """A session shown, its first measured frame submitted."""
        feature, host = benchmark_host(page)
        host._paint_page_values()
        host.reactor.now += 0.001  # a frame shows a later moment of the animation
        feature._tick(host.reactor.now)
        return feature, host

    def test_status_counts_confirmed_frames_and_ends_with_the_session(self):
        feature, host = self.feature()
        initial = feature.get_status()
        self.assertTrue(initial["active"])
        self.assertEqual(initial["completed_frames"], 0)
        token = feature.submitted[0].token
        feature.on_render_sent(token, 0.01)
        self.assertEqual(feature.get_status()["completed_frames"], 0)
        feature.on_render_receipt(RenderReceipt(token, True, 10, 8, 1), 0.02)
        self.assertEqual(feature.get_status()["completed_frames"], 1)
        feature._tick(0.02)
        self.assertEqual(feature.get_status()["completed_frames"], 1)
        feature._cycle_mode(0.03)
        self.assertNotEqual(feature.get_status()["session"], initial["session"])
        self.assertEqual(feature.get_status()["completed_frames"], 0)
        feature.back(host.page)
        self.assertFalse(feature.get_status()["active"])
        self.assertIsNone(feature.get_status()["completed_frames"])

    def test_a_session_is_paced_by_typer_commits_not_the_display(self):
        feature, host = self.feature()
        self.assertFalse(host.renderer.synced)
        feature.back(host.page)
        self.assertTrue(host.renderer.synced)

        feature, host = self.feature()
        host.print_state = PrintState.PRINTING
        host.page_for_print_state = lambda: ScreenPage.PRINTING
        feature.update(1.0)
        self.assertTrue(host.renderer.synced)

    def test_animation_speed_depends_on_elapsed_time_not_frame_count(self):
        snapshots = []
        for frame_count in (40, 120):
            feature, host = self.feature()
            feature.session_started = 10.0
            feature.frame = frame_count
            host.reactor.now = 11.5
            host._paint_page_values()
            tree = feature.page_tree
            snapshots.append((tree.rect(LayoutRef.CARD), tuple(
                tree.state[key] for key in (layout_page.LayoutState.ROW_ONE,
                                           layout_page.LayoutState.ROW_TWO))))
            # Three seconds to expand and another three to return.
            host.reactor.now = 13.0
            host._paint_page_values()
            self.assertEqual(tree.rect(LayoutRef.CARD).width, 510)
            host.reactor.now = 16.0
            host._paint_page_values()
            self.assertEqual(tree.rect(LayoutRef.CARD).width, 300)
        self.assertEqual(snapshots[0], snapshots[1])
        self.assertEqual(snapshots[0][0].width, 405)

    def test_rows_remain_readable_and_restart_resets_animation(self):
        feature, host = self.feature()
        key = layout_page.LayoutState.ROW_ONE
        original = feature.page_tree.state[key]
        host.reactor.now = 1.99
        host._paint_page_values()
        self.assertEqual(feature.page_tree.state[key], original)
        host.reactor.now = 2.0
        host._paint_page_values()
        self.assertNotEqual(feature.page_tree.state[key], original)
        host.reactor.now = 8.0
        feature._cycle_mode(host.reactor.now)
        self.assertEqual(feature.page_tree.state[key], original)
        self.assertEqual(feature.page_tree.rect(LayoutRef.CARD).width, 300)
        self.assertIsNone(feature.display_stats.commit_fps)
        self.assertIsNone(feature.display_stats.python_p95_ms)

    def test_render_rate_has_no_target_or_fixed_frame_count_ceiling(self):
        for fps in (20, 40, 60, 1000):
            with self.subTest(fps=fps):
                feature, host = self.feature()
                for frame in range(1, fps * 3 + 1):
                    host.reactor.now = frame / fps
                    token = feature.submitted[0].token
                    feature.on_render_receipt(
                        RenderReceipt(token, True, 12000, 8000, 1000), host.reactor.now)
                    if frame < fps * 3:
                        feature._tick(host.reactor.now)
                feature._refresh_display(host.reactor.now)
                self.assertEqual(feature.display_stats.commit_fps, fps)
                self.assertAlmostEqual(feature.display_stats.frame_median_ms, 1000.0 / fps)
                self.assertGreaterEqual(feature.display_stats.python_p95_ms,
                                        feature.display_stats.python_ms)
                self.assertLessEqual(len(feature.receipt_times), fps + 1)
                host._paint_page_values()
                tree = feature.page_tree
                commands = tree.draw(host.renderer, tree.state)
                host.renderer.discard_surface()
                self.assertTrue(any('PY P95' in command for command in commands))
                self.assertTrue(any('UNLIMITED' in command for command in commands))

    def test_first_live_stats_normalize_a_window_shorter_than_one_second(self):
        feature, host = self.feature()
        for frame in range(1, feature.WARMUP_FRAMES + 2):
            host.reactor.now = frame / 60.0
            token = feature.submitted[0].token
            feature.on_render_receipt(
                RenderReceipt(token, True, 12000, 8000, 1000), host.reactor.now)
            if frame <= feature.WARMUP_FRAMES:
                feature._tick(host.reactor.now)
        feature._refresh_display(host.reactor.now)
        self.assertLess(host.reactor.now, 1.0)
        self.assertAlmostEqual(feature.display_stats.commit_fps, 60.0)
        self.assertIsNotNone(feature.display_stats.python_p95_ms)

    def test_the_next_frame_is_written_once_the_last_is_at_typer(self):
        feature, host = self.feature()
        first = feature.submitted[0]
        self.assertEqual(host.reactor.waketime, first.deadline)
        submitted = len(host.rendering.submitted)

        feature.on_render_sent("stale-token", 0.01)
        self.assertEqual(len(host.rendering.submitted), submitted)

        feature.on_render_sent(first.token, 0.02)
        self.assertEqual(len(host.rendering.submitted), submitted + 1)
        self.assertEqual(host.reactor.waketime, first.deadline)
        self.assertFalse(feature.receipt_times)

    def test_sent_notifications_ignore_cancelled_restarted_and_completed_frames(self):
        for operation in ("back", "reset", "restart", "receipt"):
            with self.subTest(operation=operation):
                feature, host = self.feature()
                token = feature.submitted[0].token
                if operation == "back":
                    feature.back(host.page)
                elif operation == "reset":
                    feature._cycle_mode(0.01)
                elif operation == "restart":
                    feature.on_renderer_restarted()
                else:
                    feature.on_render_receipt(RenderReceipt(token, True, 1, 1, 0), 0.01)
                    feature._tick(0.01)
                submitted = len(host.rendering.submitted)
                feature.on_render_sent(token, 0.02)
                self.assertEqual(len(host.rendering.submitted), submitted)

    def test_frames_overlap_and_receipts_count_in_order(self):
        feature, host = self.feature()
        first = feature.submitted[0].token
        feature.on_render_sent(first, 0.0)
        second = feature.submitted[1].token

        feature.on_render_receipt(RenderReceipt(second, True, 14700, 14000, 10000), 0.01)
        self.assertEqual(feature.get_status()["completed_frames"], 0)

        feature.on_render_receipt(RenderReceipt(first, True, 14700, 14000, 10000), 0.02)
        self.assertEqual(feature.get_status()["completed_frames"], 1)
        self.assertEqual(feature.submitted[0].token, second)
        self.assertEqual(host.reactor.waketime, feature.submitted[0].deadline)
        self.assertEqual(len(feature.receipt_times), 1)

    def test_an_overlapped_frame_costs_the_time_since_the_previous_one(self):
        feature, host = self.feature()
        feature.WARMUP_FRAMES = 0
        first = feature.submitted[0].token
        host.reactor.now = 0.002
        feature.on_render_sent(first, 0.002)
        feature.on_render_receipt(RenderReceipt(first, True, 14700, 14000, 10000), 0.02)
        feature.on_render_receipt(RenderReceipt(feature.submitted[0].token, True, 14700, 14000, 10000), 0.03)
        self.assertAlmostEqual(feature.samples[-1].latency_ms, 10.0)

    def test_a_refused_frame_is_tried_again(self):
        feature, host = self.feature()
        token = feature.submitted[0].token
        with refused_render(host.renderer):
            feature.on_render_sent(token, 0.0)
        self.assertEqual(feature.frame, 1)
        self.assertEqual(len(feature.submitted), 1)
        self.assertEqual(feature.display_status, "SCREEN BUSY")

        host.reactor.now = 0.02
        feature.on_render_receipt(RenderReceipt(token, True, 12000, 8000, 1000), 0.02)
        feature._tick(0.02)
        self.assertEqual(feature.frame, 2)
        self.assertEqual(host.rendering.submitted[-1].receipt, feature.submitted[0].token)

    def test_cancel_reset_and_failure_forget_the_frames_in_flight(self):
        for operation, status in (("back", None), ("reset", None), ("timeout", "RECEIPT TIMEOUT"),
                                  ("failed", "FAILED RECEIPT")):
            with self.subTest(operation=operation):
                feature, host = self.feature()
                feature.on_render_sent(feature.submitted[0].token, 0)
                old_tokens = [frame.token for frame in feature.submitted]
                if operation == "back":
                    feature.back(host.page)
                    self.assertFalse(feature.active)
                elif operation == "reset":
                    feature._cycle_mode(0.01)
                    self.assertNotIn(feature.submitted[0].token, old_tokens)
                elif operation == "timeout":
                    host.reactor.now = 1.1
                    feature._tick(host.reactor.now)
                else:
                    feature.on_render_receipt(RenderReceipt(old_tokens[0], False, 0, 0, 0), 0.01)
                if status is not None:
                    self.assertFalse(feature.active)
                    self.assertTrue(any(status in command for command in host.rendering.submitted[-1].commands))
                count = len(host.rendering.submitted)
                completed = feature.get_status()["completed_frames"]
                feature.on_render_receipt(RenderReceipt(old_tokens[-1], True, 1, 1, 0), 1.2)
                self.assertEqual(len(host.rendering.submitted), count)
                self.assertEqual(feature.get_status()["completed_frames"], completed)

    def test_pipeline_runs_at_stage_capacity_without_a_frame_rate_limit(self):
        # Deterministic stage timings, not a host-speed performance assertion: the
        # transaction costs build_seconds, Typer draws one frame at a time and the
        # loop writes a frame only while fewer than its two credits are out.
        for build_seconds, render_seconds in ((0.0185, 0.0147), (0.002, 0.003), (0.0002, 0.0003)):
            with self.subTest(build_seconds=build_seconds):
                feature, host = benchmark_host(ScreenPage.COMPONENT_BENCHMARK)
                host._paint_page_values()
                original_send = host.renderer.send
                deliveries, drawn_at = [], []

                def send(*args, **kwargs):
                    host.reactor.now += build_seconds
                    token = kwargs["receipt"]
                    sent_at = max([host.reactor.now] + drawn_at[-2:-1])
                    drawn_at.append(max([sent_at] + drawn_at[-1:]) + render_seconds)
                    deliveries.extend(((sent_at, 0, "sent", token), (drawn_at[-1], 1, "rendered", token)))
                    deliveries.sort(key=lambda delivery: delivery[:2])
                    return original_send(*args, **kwargs)

                host.renderer.send = send
                confirmed = 0
                while host.reactor.now < 1.0:
                    delivery_at = deliveries[0][0] if deliveries else float("inf")
                    host.reactor.now = max(host.reactor.now, min(delivery_at, host.reactor.waketime))
                    if delivery_at <= host.reactor.waketime:
                        _, _, kind, token = deliveries.pop(0)
                        if kind == "sent":
                            feature.on_render_sent(token, host.reactor.now)
                        else:
                            feature.on_render_receipt(RenderReceipt(token, True, 14700, 14000, 10000), host.reactor.now)
                            confirmed += 1
                    else:
                        feature._tick(host.reactor.now)
                capacity = 1.0 / max(build_seconds, render_seconds)
                self.assertAlmostEqual(confirmed, capacity, delta=capacity * 0.05 + 2)
                self.assertTrue(all(batch.receipt_phase == "staged"
                                    for batch in host.rendering.submitted if batch.receipt))

    def test_targeted_frames_match_normal_component_updates(self):
        host = SimpleNamespace(
            reactor=Reactor(), renderer=FeatherRenderer(),
            page=ScreenPage.COMPONENT_BENCHMARK,
            print_state=PrintState.IDLE,
        )
        feature = BenchmarkFeature(host)
        regular = layout_page.create_page()
        targeted = layout_page.create_page()
        regular_renderer = FeatherRenderer()
        targeted_renderer = FeatherRenderer()
        initial = feature._layout_state(0.0, True)
        regular_renderer.begin_page("Component benchmark", back=True)
        targeted_renderer.begin_page("Component benchmark", back=True)
        self.assertEqual(regular.draw(regular_renderer, initial),
                         targeted.draw(targeted_renderer, initial))

        for frame in range(1, 44):
            feature.frame = frame
            include_stats = frame % 7 == 0
            state = feature._layout_state(frame / 7.0, include_stats)
            refs = [LayoutRef.CARD]
            if include_stats:
                refs.append(BenchmarkRef.STATS)
            for page in (regular, targeted):
                page.node(LayoutRef.CARD).width(feature._layout_width(frame / 7.0))
            expected = regular.update(regular_renderer, state)
            actual = targeted.update_refs(targeted_renderer, state, refs)
            self.assertEqual(actual, expected, frame)
            for ref in (LayoutRef.CARD, LayoutRef.DESCRIPTION,
                        LayoutRef.ACTIONS, BenchmarkRef.STATS):
                self.assertEqual(targeted.rect(ref), regular.rect(ref),
                                 (frame, ref))

    def test_frames_run_no_python_inside_the_engine(self):
        feature = BenchmarkFeature(SimpleNamespace(renderer=FeatherRenderer()))
        page, renderer = layout_page.create_page(), FeatherRenderer()
        renderer.begin_page("Component benchmark", back=True)
        page.draw(renderer, feature._layout_state(0.0, True))
        renderer.discard_surface()
        calls = []

        def profile(frame, event, _arg):
            if event == "call":
                calls.append(frame.f_code.co_name)

        for frame in range(1, 22):
            feature.frame = frame
            include_stats = frame % 7 == 0
            state = feature._layout_state(frame / 7.0, include_stats)
            refs = [LayoutRef.CARD] + ([BenchmarkRef.STATS] if include_stats else [])
            page.node(LayoutRef.CARD).layout_options.width = feature._layout_width(frame / 7.0)
            metrics_key = get_font_metrics().cache_key
            sys.setprofile(profile)
            try:
                commands = engine_update_refs(page, renderer, state, refs, metrics_key)
            finally:
                sys.setprofile(None)
            renderer.discard_surface()
            self.assertTrue(commands, frame)
            self.assertEqual(calls, [], frame)

    def test_five_menu_title_taps_open_only_the_component_benchmark(self):
        class Menu(HomePagesMixin):
            def __init__(self):
                self.reactor = Reactor()
                self.renderer = FeatherRenderer()
                RenderCapture(self.renderer)
                self.opened = []

            def _require_idle(self):
                return None

            def _show_page(self, page):
                self.opened.append(page)

        menu = Menu()
        menu._render_main_menu()
        self.assertIn("menu.component_benchmark.tap", [region.action for region in menu.renderer.hitboxes])
        for index in range(4):
            menu.reactor.now = index * 0.2
            menu._handle_component_benchmark_tap()
        self.assertEqual(menu.opened, [])
        menu.reactor.now = 0.8
        menu._handle_component_benchmark_tap()
        self.assertEqual(menu.opened, [ScreenPage.COMPONENT_BENCHMARK])

        menu._render_main_menu()
        menu.reactor.now = 4.0
        menu._handle_component_benchmark_tap()
        menu.reactor.now = 6.1
        menu._handle_component_benchmark_tap()
        self.assertEqual(menu._component_benchmark_taps, 1)

    def test_frames_reflow_wrapped_text_and_button_columns_and_publish_metrics(self):
        feature, host = self.feature()
        reactor, renderer, rendering = host.reactor, host.renderer, host.rendering
        tree = feature.page_tree
        card = tree.node(LayoutRef.CARD)
        description = tree.node(LayoutRef.DESCRIPTION)
        actions = tree.node(LayoutRef.ACTIONS)
        narrow_height = tree.layout.rect(description).height
        narrow_buttons = tuple(tree.layout.rect(button) for button in actions.children)
        self.assertEqual(tree.layout.rect(card).width, 300)
        self.assertGreaterEqual(narrow_buttons[0].width, 120)
        self.assertEqual(narrow_buttons[0].y, narrow_buttons[1].y)
        self.assertGreater(narrow_buttons[2].y, narrow_buttons[0].y)

        for frame in range(65):
            previous_card = tree.layout.rect(card)
            token = rendering.submitted[-1].receipt
            self.assertEqual(token, feature.submitted[0].token)
            feature.on_render_receipt(
                RenderReceipt(token, True, 12000, 8000, 1000),
                reactor.now + 0.012)
            reactor.now += 0.15
            feature._tick(reactor.now)
            commands = rendering.submitted[-1].commands
            if frame == 0:
                current_card = tree.layout.rect(card)
                for exposed in previous_card.subtract(current_card):
                    self.assertIn(renderer.fill(*exposed), commands)
                self.assertNotIn(renderer.fill(*current_card), commands)
                self.assertFalse(any("RENDER FPS" in command for command in commands))
                self.assertFalse(any("-s 800 442" in command for command in commands))
            if frame == 15:
                self.assertGreater(tree.layout.rect(card).width, 460)
                self.assertLess(tree.layout.rect(description).height, narrow_height)
                wide_buttons = tuple(
                    tree.layout.rect(button) for button in actions.children)
                self.assertEqual({rect.y for rect in wide_buttons},
                                 {wide_buttons[0].y})
                self.assertGreater(wide_buttons[0].width,
                                   narrow_buttons[0].width)
            if frame == 19:
                card_bounds = tree.layout.rect(card)
                stats_bounds = tree.layout.rect(
                    tree.node(BenchmarkRef.STATS))
                self.assertGreater(card_bounds.width, 460)
                self.assertGreaterEqual(stats_bounds.x - card_bounds.right, 20)
                self.assertLessEqual(stats_bounds.x - card_bounds.right, 30)
                self.assertGreater(tree.layout.rect(actions.children[0]).width,
                                   wide_buttons[0].width)

        self.assertGreater(feature.display_stats.commit_fps, 0)
        self.assertGreater(feature.display_stats.frame_median_ms, 0)
        self.assertGreater(feature.display_stats.python_ms, 0)
        self.assertTrue(any(
            "RENDER FPS" in command
            for batch in rendering.batches[1:] for command in batch))
        self.assertFalse(any(
            "-s 800 442" in command
            for batch in rendering.batches[1:] for command in batch))
        feature.back(host.page)
        self.assertEqual(host.page, ScreenPage.MAIN_MENU)
        self.assertFalse(feature.active)
        self.assertFalse(feature.submitted)

    def test_timeout_stops_component_frames_and_reports_failure(self):
        feature, host = self.feature()

        host.reactor.now = feature.RECEIPT_TIMEOUT + 0.01
        feature._tick(host.reactor.now)

        self.assertFalse(feature.active)
        self.assertFalse(feature.submitted)
        self.assertEqual(feature.display_status, "RECEIPT TIMEOUT")
        self.assertEqual(host.reactor.waketime, host.reactor.NEVER)
        self.assertTrue(any("RECEIPT TIMEOUT" in command for command in host.rendering.batches[-1]))

    def test_existing_render_benchmark_still_cycles_modes(self):
        feature, host = self.feature(ScreenPage.RENDER_BENCHMARK)
        first_mode = feature.mode
        token = feature.submitted[0].token
        feature.on_render_receipt(
            RenderReceipt(token, True, 12000, 8000, 1000), 0.012)
        feature._cycle_mode(0.02)

        self.assertNotEqual(feature.mode, first_mode)
        self.assertEqual(feature.frame, 1)
        feature.back(host.page)
        self.assertEqual(host.page, ScreenPage.SETTINGS)

    def test_a_printer_no_longer_idle_gets_no_session(self):
        feature, host = benchmark_host(ScreenPage.RENDER_BENCHMARK)
        host.print_state = PrintState.PRINTING
        self.assertIsNone(feature.page_content(host.page))
        self.assertFalse(feature.active)


if __name__ == "__main__":
    unittest.main()
