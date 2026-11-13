## Lazy interactive framebuffer benchmark for Feather.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import math
import statistics
import time
from collections import deque, namedtuple

from ui.lazy import LazyModule
from feather.features.manager import FeatureHostProxy
from ff5m_ui.benchmark.actions import BenchmarkAction, BenchmarkRoute
from ff5m_ui.benchmark.constants import BENCHMARK_MODES
from ff5m_ui.screen import ScreenPage
from ff5m_ui.print_state import PrintState


benchmark_page = LazyModule("ff5m_ui.benchmark.page")
layout_page = LazyModule("ff5m_ui.benchmark.layout_page")
benchmark_state = LazyModule("ff5m_ui.benchmark.state")


BenchmarkSample = namedtuple(
    "BenchmarkSample",
    ("latency_ms", "typer_ms", "cpu_ms", "flush_ms", "python_ms"),
)

SubmittedFrame = namedtuple("SubmittedFrame", "token submitted_at deadline python_ms")

BENCHMARK_PAGES = (ScreenPage.RENDER_BENCHMARK, ScreenPage.COMPONENT_BENCHMARK)


class BenchmarkFeature(FeatureHostProxy):
    name = "benchmark"

    # A frame the screen refuses (a modal layer covers the page, output frozen).
    REFUSED_RETRY_INTERVAL = 0.1
    RECEIPT_TIMEOUT = 1.0
    WARMUP_FRAMES = 30
    WINDOW_FRAMES = 120
    STATS_PERIOD = 1.0
    REFLOW_PERIOD = 6.0
    ROW_PERIOD = 2.0
    MODES = BENCHMARK_MODES

    def __init__(self, host):
        super().__init__(host)

        self.timer = None
        self.active = False
        self.page_tree = None
        self.benchmark_page = None
        self.workload_node = None
        self.submitted = deque()
        self.last_receipt_at = 0.0
        self.session = 0
        self.frame = 0
        self.session_started = 0.0
        self.next_stats_at = 0.0
        self.samples = deque(maxlen=self.WINDOW_FRAMES)
        self.receipt_times = deque()
        self.mode = self.MODES[0]
        self.display_stats = self._empty_display_stats()
        self.display_status = self._warmup_status()
        self.stats_dirty = True

    def initialize(self):
        self.timer = self.reactor.register_timer(
            self._tick, self.reactor.NEVER)

    def render(self, page):
        # page_content shows both pages; only a printer that is no longer idle leaves them to the painter.
        raise RuntimeError("Render benchmark requires an idle printer")

    def page_content(self, page):
        if page not in BENCHMARK_PAGES:
            return None

        if self.page_tree is None:
            if self.print_state != PrintState.IDLE:
                return None
            self._start_session(page)
        return self.page_tree, self.page_tree.title, True

    def write_page_values(self, page):
        if page != self.benchmark_page:
            return

        include_stats, self.stats_dirty = self.stats_dirty, False
        if not self.active:
            # An ended session keeps its last frame and shows why it ended.
            if include_stats:
                self.page_tree.state.update(self._stats_state())
            return

        eventtime = self.reactor.monotonic()
        if page == ScreenPage.COMPONENT_BENCHMARK:
            elapsed = max(0.0, eventtime - self.session_started)
            # Setting a layout field relayouts the node by itself.
            self.workload_node.layout_options.width = self._layout_width(elapsed)
            self.page_tree.state.update(self._layout_state(elapsed, include_stats))
        else:
            self.page_tree.state.update(self._state(eventtime, include_stats))

    def _start_session(self, page):
        self.benchmark_page = page
        self.mode = self.MODES[0]
        if page == ScreenPage.RENDER_BENCHMARK:
            self.page_tree = benchmark_page.create_page()
        else:
            self.page_tree = layout_page.create_page()
            self.workload_node = self.page_tree.node(layout_page.LayoutRef.CARD)
        self.active = True
        # The benchmark measures how fast frames are built and committed, so
        # they are not held back to the display's presentation rate.
        self.renderer.synced = False
        now = self.reactor.monotonic()
        self._reset_measurements(now)
        self.reactor.update_timer(self.timer, now)

    def get_status(self):
        return {"active": self.active, "session": self.session,
                "completed_frames": self._completed_frames() if self.active else None,
                "statistics": self.display_stats._asdict()}

    def allows_action(self, page, action):
        return page == self.benchmark_page and action == "nav.back"

    def handle_action(self, page, action):
        return False

    def resolve_semantic_action(self, page, wire_id):
        if page != self.benchmark_page or self.page_tree is None:
            return None
        
        return self.page_tree.resolve_action(wire_id)

    def handle_semantic_action(self, page, action):
        if page != self.benchmark_page:
            return False
        
        if (isinstance(action, BenchmarkAction)
                and action.route == BenchmarkRoute.NEXT_MODE):
            self._cycle_mode(self.reactor.monotonic())
            return True
        
        raise KeyError("Unsupported benchmark action: %s" % action)

    def back(self, page):
        if page != self.benchmark_page:
            return False
        
        self._stop_session()
        self._show_page(
            ScreenPage.MAIN_MENU if page == ScreenPage.COMPONENT_BENCHMARK
            else ScreenPage.SETTINGS)

        return True

    def update(self, eventtime):
        if self.active and self.print_state != PrintState.IDLE:
            self._stop_session()
            self._show_page(self.page_for_print_state())

    def on_page_changed(self, old_page, new_page):
        if old_page in (ScreenPage.RENDER_BENCHMARK,
                        ScreenPage.COMPONENT_BENCHMARK) and new_page != old_page:
            self._stop_session()

    def on_print_state_changed(self, old_state, new_state, stats_state):
        if self.active and new_state != PrintState.IDLE:
            self._stop_session()

    def on_renderer_restarted(self):
        if self.page == self.benchmark_page:
            self._stop_session()

    def on_render_sent(self, token, eventtime):
        # The frame is at Typer: the next one is written now, so the render
        # thread builds it while Typer draws this one.
        if self.active and self.submitted and self.submitted[-1].token == token:
            self._submit_frame(eventtime)

    def on_render_receipt(self, receipt, eventtime):
        if not self.active or not self.submitted or receipt.token != self.submitted[0].token:
            return

        frame = self.submitted.popleft()
        if not receipt.success:
            self._fail("FAILED RECEIPT")
            return

        # Frames overlap: one's time starts when the previous one was done.
        started = max(frame.submitted_at, self.last_receipt_at)
        self.last_receipt_at = float(eventtime)
        sample = BenchmarkSample(
            latency_ms=max(0.0, float(eventtime) - started) * 1000.0,
            typer_ms=receipt.total_us / 1000.0,
            cpu_ms=receipt.cpu_us / 1000.0,
            flush_ms=receipt.flush_us / 1000.0,
            python_ms=frame.python_ms,
        )

        self.receipt_times.append(float(eventtime))
        window_start = float(eventtime) - 1.0
        while self.receipt_times and self.receipt_times[0] <= window_start:
            self.receipt_times.popleft()
        completed = self._completed_frames()
        if completed > self.WARMUP_FRAMES:
            self.samples.append(sample)
        
        if completed == self.WARMUP_FRAMES:
            self._refresh_display(eventtime)
            self.next_stats_at = float(eventtime) + self.STATS_PERIOD
        elif (completed > self.WARMUP_FRAMES
              and eventtime >= self.next_stats_at):
            self._refresh_display(eventtime)
            self.next_stats_at = float(eventtime) + self.STATS_PERIOD

        self.reactor.update_timer(
            self.timer, self.submitted[0].deadline if self.submitted else float(eventtime))

    def deactivate(self):
        self._stop_session()
        if self.timer is not None:
            try:
                self.reactor.unregister_timer(self.timer)
            except Exception:
                pass
            
            self.timer = None

    def _completed_frames(self):
        return self.frame - len(self.submitted)

    def _warmup_status(self):
        return "WARMUP %d/%d" % (
            min(self._completed_frames(), self.WARMUP_FRAMES), self.WARMUP_FRAMES)

    def _raster_name(self):
        return str(self.renderer.raster_acceleration).strip().upper()

    def _empty_display_stats(self):
        return benchmark_state.empty_stats(self._raster_name())

    def _reset_measurements(self, eventtime):
        self.submitted.clear()
        self.session += 1
        self.frame = 0
        self.last_receipt_at = 0.0
        self.session_started = float(eventtime)
        self.next_stats_at = float(eventtime) + self.STATS_PERIOD
        self.samples.clear()
        self.receipt_times.clear()
        self.display_stats = self._empty_display_stats()
        self.display_status = self._warmup_status()
        self.stats_dirty = True

    def _stop_session(self):
        self.active = False
        self.renderer.synced = True
        self.submitted.clear()
        self.samples.clear()
        self.receipt_times.clear()
        self.page_tree = None
        self.benchmark_page = None
        self.workload_node = None

        if self.timer is not None:
            try:
                self.reactor.update_timer(self.timer, self.reactor.NEVER)
            except Exception:
                pass

    def _state(self, eventtime, include_stats):
        elapsed = max(0.0, float(eventtime) - self.session_started)
        angles = (
            elapsed * 0.83,
            elapsed * 1.17,
            elapsed * 0.29,
        )

        state = benchmark_state.BenchmarkState
        values = {
            state.ANGLE_X: angles[0],
            state.ANGLE_Y: angles[1],
            state.ANGLE_Z: angles[2],
            state.MODE: self.mode,
        }

        if include_stats:
            values.update(self._stats_state())

        return values

    def _stats_state(self):
        values = benchmark_state.stats_values(self.display_stats)
        values[benchmark_state.BenchmarkState.STATUS] = self.display_status
        return values

    @classmethod
    def _layout_width(cls, elapsed):
        phase = (elapsed % cls.REFLOW_PERIOD) / cls.REFLOW_PERIOD
        return 300 + int(round(420 * min(phase, 1.0 - phase)))

    def _layout_state(self, elapsed, include_stats):
        state = layout_page.LayoutState
        rows = layout_page.ROWS
        offset = int(elapsed / self.ROW_PERIOD) % len(rows)
        values = {
            state.WIDTH: "%d PX / FRAME %d" % (
                self._layout_width(elapsed), self.frame),
            state.DESCRIPTION: layout_page.DESCRIPTION,
            state.ROW_ONE: rows[offset],
            state.ROW_TWO: rows[(offset + 1) % len(rows)],
            state.ROW_THREE: rows[(offset + 2) % len(rows)],
        }
        if include_stats:
            values.update(self._stats_state())
            values[benchmark_state.BenchmarkState.MODE] = "reflow"
        return values

    def _token(self):
        return "%x:%d" % (self.session, self.frame + 1)

    def _submit_frame(self, eventtime):
        if not self.active or self.page != self.benchmark_page:
            return self.reactor.NEVER

        token = self._token()
        submitted_at = self.reactor.monotonic()
        started = time.perf_counter()
        accepted = self._paint_page_values(receipt=token, receipt_phase="staged")
        python_ms = (time.perf_counter() - started) * 1000.0

        if not accepted:
            self.display_status = "SCREEN BUSY"
            self.stats_dirty = True
            retry_at = submitted_at + self.REFUSED_RETRY_INTERVAL
            self.reactor.update_timer(self.timer, retry_at)
            return retry_at

        self.submitted.append(SubmittedFrame(token, submitted_at, submitted_at + self.RECEIPT_TIMEOUT, python_ms))
        self.frame += 1
        deadline = self.submitted[0].deadline
        self.reactor.update_timer(self.timer, deadline)
        return deadline

    def _tick(self, eventtime):
        if not self.active:
            return self.reactor.NEVER
        
        if not self.submitted:
            return self._submit_frame(eventtime)

        if eventtime >= self.submitted[0].deadline:
            self._fail("RECEIPT TIMEOUT")
            return self.reactor.NEVER

        return self.submitted[0].deadline

    @staticmethod
    def _percentile(values, percentile):
        ordered = sorted(values)
        if not ordered:
            return 0.0
        
        index = int(math.ceil(percentile * len(ordered))) - 1
        return ordered[max(0, min(len(ordered) - 1, index))]

    def _refresh_display(self, eventtime):
        if self._completed_frames() <= self.WARMUP_FRAMES:
            self.display_status = self._warmup_status()
            self.stats_dirty = True
            return
        
        samples = tuple(self.samples)
        if not samples:
            return
        
        # Count rendered transactions independently of physical screen refresh.
        window_start = max(self.session_started, float(eventtime) - 1.0)
        duration = float(eventtime) - window_start
        if duration <= 0:
            return
        committed = sum(window_start < value <= eventtime for value in self.receipt_times)
        fps = committed / duration
        latency = tuple(item.latency_ms for item in samples)
        self.display_stats = benchmark_state.BenchmarkStats(
            commit_fps=fps,
            frame_median_ms=statistics.median(latency),
            frame_p95_ms=self._percentile(latency, 0.95),
            typer_ms=statistics.median(item.typer_ms for item in samples),
            cpu_ms=statistics.median(item.cpu_ms for item in samples),
            flush_ms=statistics.median(item.flush_ms for item in samples),
            python_ms=statistics.median(item.python_ms for item in samples),
            python_p95_ms=self._percentile(tuple(item.python_ms for item in samples), 0.95),
            raster=self._raster_name(),
        )
        
        self.display_status = "UNLIMITED"
        self.stats_dirty = True

    def _cycle_mode(self, eventtime):
        if not self.active or self.page != self.benchmark_page:
            return

        if self.benchmark_page == ScreenPage.RENDER_BENCHMARK:
            index = self.MODES.index(self.mode)
            self.mode = self.MODES[(index + 1) % len(self.MODES)]
        self._reset_measurements(eventtime)
        self._submit_frame(eventtime)

    def _fail(self, status):
        self.submitted.clear()
        self.active = False
        self.display_status = str(status)
        self.stats_dirty = True
        if self.timer is not None:
            self.reactor.update_timer(self.timer, self.reactor.NEVER)

        if self.page_tree is not None and self.page == self.benchmark_page:
            self._paint_page_values()
