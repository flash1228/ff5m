## Active-print page and preview lifecycle for Feather.
##
## Copyright (C) 2025-2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import os
import threading

from timelapse_state import TimelapsePhase
from ui import ThemeColor
from ff5m_ui.printing import runtime as printing_ui
from ff5m_ui.screen import ScreenPage
from ff5m_ui.print_state import PrintState
from feather.previews import (
    PREVIEW_EXECUTABLE, PREVIEW_MASK_SIZE, PREVIEW_TIMEOUT, PreviewCancelled,
    colorize_preview, layer_progress, load_preview,
)


GCODE_PREVIEW_LOADER_PERIOD = 0.08
GCODE_PREVIEW_REDRAW_PERIOD = 5.0
GCODE_PREVIEW_LOADER_RADII = (7, 10, 13, 10)


def _progress_state(progress, values):
    return {
        printing_ui.PrintingState.PROGRESS: progress,
        printing_ui.PrintingState.ELAPSED: values[0],
        printing_ui.PrintingState.REMAINING: values[1],
        printing_ui.PrintingState.LAYER: values[2],
        printing_ui.PrintingState.HEIGHT: "%.2f MM" % values[3],
    }


def _loader_radius(preview):
    return GCODE_PREVIEW_LOADER_RADII[int(preview.get("loading_phase", 0)) % len(GCODE_PREVIEW_LOADER_RADII)]


def _gcode_preview_image_rect():
    box = printing_ui.rect(printing_ui.PrintingRef.PREVIEW_BOX)
    padding = max(0, printing_ui.PREVIEW_IMAGE_PADDING)
    # Keep the raster/cache contract shared with file tiles as containers grow.
    width = min(PREVIEW_MASK_SIZE[0], max(1, box.width - 2 * padding))
    height = min(PREVIEW_MASK_SIZE[1], max(1, box.height - 2 * padding))
    return (box.x + (box.width - width) // 2,
            box.y + (box.height - height) // 2, width, height)


def _render_gcode_preview(path, cancel=None):
    _box_x, _box_y, width, height = _gcode_preview_image_rect()
    return load_preview(
        path, width, height, executable=PREVIEW_EXECUTABLE,
        timeout=PREVIEW_TIMEOUT, cancel=cancel)


class PrintingPagesMixin:
    def _print_control_values(self, eventtime):
        return {
            printing_ui.PrintingState.CONTROLS_READY: self._print_controls_ready(),
            printing_ui.PrintingState.PENDING_ACTION: self.pending_action or "",
            printing_ui.PrintingState.LIVE_Z_ALLOWED: self._live_z_adjust_allowed(eventtime),
        }

    def _update_print_controls(self):
        self._request_print_values(self._print_control_values(self.reactor.monotonic()))

    def _request_print_values(self, values=None):
        """Ask the screen's next transaction to write the printing page's values,
        when the page shows and values (all of them when None) differ from what
        the page holds."""
        if not self._page_paint_allowed(ScreenPage.PRINTING, ScreenPage.PAUSED):
            return
        state = printing_ui.get_page().state
        if values is not None and all(state.get(key) == value for key, value in values.items()):
            return
        self._paint_page_values()

    def _timelapse_wait_text(self):
        if getattr(self, "pending_action", None) == "print.cancel.confirm":
            return "CANCELLING PRINT", "PLEASE WAIT WHILE THE PRINT STOPS"
        status = self._timelapse_status().get("wait_status")
        if status == "busy":
            return ("WAITING FOR THE PREVIOUS TIMELAPSE",
                    "THE PRINT STARTS WHEN THE TIMELAPSE FINISHES")
        if status == "idle":
            return "TIMELAPSE IS READY", "PREPARING TO START THE PRINT"
        return ("TIMELAPSE STATUS UNAVAILABLE",
                "CHECKING MOONRAKER. PRINT WAITS FOR CONFIRMED IDLE")

    def _timelapse_wait_text_commands(self, text):
        return [
            self.renderer.fill(35, 130, 730, 96, ThemeColor.BACKGROUND),
            self.renderer.text(
                400, 155, text[0], ThemeColor.WARNING, "JetBrainsMono Bold 12pt",
                "center", "middle", max_width=700, truncate=True),
            self.renderer.text(
                400, 200, text[1], ThemeColor.DIM, "JetBrainsMono 8pt",
                "center", "middle", max_width=700, truncate=True),
        ]

    def _render_timelapse_wait(self):
        cancelling = getattr(self, "pending_action", None) == "print.cancel.confirm"
        text = self._timelapse_wait_text()
        commands = self.renderer.begin_page(
            "CANCELLING PRINT" if cancelling else "WAITING FOR TIMELAPSE")
        commands += self._timelapse_wait_text_commands(text)
        commands += self._timelapse_wait_loader_commands()
        if not cancelling:
            commands += self.renderer.button(
                "print.cancel", 160, 310, 230, 75,
                "CANCEL PRINT", state="danger",
                font="JetBrainsMono Bold 10pt")
            commands += self.renderer.button(
                "timelapse.wait.cancel_render", 410, 310, 230, 75,
                "CANCEL TIMELAPSE", state="warning",
                font="JetBrainsMono Bold 9pt")
        if self.renderer.send(commands) is not False:
            self._last_timelapse_wait_text = text

    def _timelapse_wait_loader_commands(self):
        phase = getattr(self, "busy_phase", 0)
        return [self.renderer.fill(
            290 + index * 48, 255, 32, 12,
            ThemeColor.PRIMARY if index == phase % 5 else ThemeColor.MUTED)
            for index in range(5)]

    def _update_timelapse_wait(self):
        text = self._timelapse_wait_text()
        if text != getattr(self, "_last_timelapse_wait_text", None):
            if self.renderer.send(self._timelapse_wait_text_commands(text)) is not False:
                self._last_timelapse_wait_text = text
        self.busy_phase = (getattr(self, "busy_phase", 0) + 1) % 5
        self.renderer.send(
            self._timelapse_wait_loader_commands(),
            kind="animation", key="timelapse-wait-loader")

    def _sync_timelapse_wait_page(self):
        if (self.page in (ScreenPage.PRINTING, ScreenPage.PAUSED)
                and self._timelapse_phase() == TimelapsePhase.WAITING):
            self._show_page(ScreenPage.TIMELAPSE_WAIT)
        elif self.page == ScreenPage.TIMELAPSE_WAIT:
            if getattr(self, "pending_action", None) == "print.cancel.confirm":
                return
            target = self.page_for_print_state()
            if target != ScreenPage.TIMELAPSE_WAIT:
                self._show_page(target)

    def _print_content(self):
        paused = self.page_for_print_state() == ScreenPage.PAUSED
        return printing_ui.get_page(), "PAUSED" if paused else "PRINTING", False

    def _write_print_values(self, eventtime, due):
        """Inside the screen's transaction: every value of the printing page when
        due, else only the loader's disc, which its timer turns."""
        page = printing_ui.get_page()
        if not due:
            preview = getattr(self, "_gcode_preview", None)
            if preview is not None and preview.get("status") == "loading":
                page.state.update({printing_ui.PrintingState.LOADER_RADIUS: _loader_radius(preview)})
            return
        paused = self.page_for_print_state() == ScreenPage.PAUSED
        print_stats = getattr(self, "print_stats", None)
        stats = (print_stats.get_status(eventtime)
                 if print_stats is not None else None)
        preview = self._prepare_gcode_preview()
        if preview is None or preview["status"] != "loading":
            self._stop_gcode_preview_loader()
        filename = self.virtual_sdcard.file_path() or "Unknown"
        values = {
            printing_ui.PrintingState.FILENAME: os.path.basename(filename),
            printing_ui.PrintingState.STATUS:
                self._display_status_parts(eventtime),
            printing_ui.PrintingState.PAUSED: paused,
        }
        progress, progress_values = self._current_print_progress_values(eventtime, stats)
        values.update(self._print_control_values(eventtime))
        values.update(_progress_state(progress, progress_values))
        values.update(self._gcode_preview_values(preview))
        page.state.update(values)

    def _gcode_preview_values(self, preview):
        """The preview's state as the page shows it: its status, the ready
        rasters inside the preview box, and the loader's disc."""
        if preview is None:
            return {printing_ui.PrintingState.PREVIEW_STATUS: "none",
                    printing_ui.PrintingState.PREVIEW_BANDS: ()}
        bands = ()
        if preview["status"] == "ready" and preview.get("image") is not None:
            x, y, _width, _height = _gcode_preview_image_rect()
            box = printing_ui.rect(printing_ui.PrintingRef.PREVIEW_BOX)
            bands = tuple((x - box.x, y - box.y, blob) for blob in preview.get("render_blobs", ()))
        return {printing_ui.PrintingState.PREVIEW_STATUS: preview["status"],
                printing_ui.PrintingState.PREVIEW_BANDS: bands,
                printing_ui.PrintingState.LOADER_RADIUS: _loader_radius(preview)}

    def _gcode_preview_render_spec(self, stats=None):
        layer_state = None
        if stats is not None and self._print_controls_ready():
            layer_state = layer_progress(stats)
            eventtime = self.reactor.monotonic()
            if layer_state is None and self._restored_print_active(eventtime):
                # Recovery seeks past the file header, so layer metadata may
                # be unavailable. Use the same estimate as the progress bar.
                progress = self._print_progress(eventtime, stats)
                layer_state = (progress, 1.0, progress)
        pending = self.renderer.color(ThemeColor.SECONDARY)
        printed = self.renderer.color(ThemeColor.PRIMARY)
        layer_key = (None if layer_state is None
                     else (layer_state[0], layer_state[1]))
        return (layer_key, pending, printed), layer_state, pending, printed

    def _prepare_gcode_preview(self):
        self._cancel_file_preview()
        path = self.virtual_sdcard.file_path()
        key = self._gcode_preview_key_for(path)
        if key is None:
            self._cancel_gcode_preview()
            self._stop_gcode_preview_loader()
            return None

        preview = getattr(self, "_gcode_preview", None)
        if preview is not None and preview["key"] == key:
            if preview.get("status") == "loading":
                self._start_gcode_preview_loader()
            return preview

        self._cancel_gcode_preview()
        preview = {
            "key": key,
            "cancel": threading.Event(),
            "cache_key": self._gcode_preview_cache_key(path),
            "status": "loading",
            "image": None,
            "render_key": None,
            "render_blobs": (),
            "recolor_pending": False,
            "redraw_after": 0.0,
            "loading_phase": 0,
        }
        self._gcode_preview = preview
        if self.file_worker is None:
            preview["status"] = "failed"
            self._stop_gcode_preview_loader()
            return preview
        # Decoding competes with the reactor thread for the GIL. Submit it in
        # a later reactor dispatch so the page frame that needs it, and any
        # motion timer due after that frame, run first.
        self.reactor.register_callback(
            lambda _eventtime: self._submit_gcode_preview(preview))
        self._start_gcode_preview_loader()
        return preview

    def _submit_gcode_preview(self, preview):
        if getattr(self, "_gcode_preview", None) is not preview:
            return
        path = preview["key"][0]
        print_stats = getattr(self, "print_stats", None)
        stats = (print_stats.get_status(self.reactor.monotonic())
                 if print_stats is not None else None)
        render_spec = self._gcode_preview_render_spec(stats)
        render_key, layer_state, pending, printed = render_spec
        cached_image = self._cached_gcode_preview_image(
            preview["cache_key"])

        def task():
            if preview["cancel"].is_set():
                raise PreviewCancelled()
            image = cached_image
            if image is None:
                image = _render_gcode_preview(path, preview["cancel"])
            if image is None:
                return None
            blobs = colorize_preview(
                image, layer_state, pending, printed)
            cache_blob = colorize_preview(
                image, None, printed, printed)[0]
            return image, render_key, blobs, printed, cache_blob

        worker = self.file_worker
        if worker is None or not worker.submit(
                task, lambda value, error:
                self._gcode_preview_ready(preview, value, error)):
            # The worker stopped after the loading frame was published; report
            # the failure through the regular completion path.
            self._gcode_preview_ready(
                preview, None, RuntimeError("File worker stopped"))

    def _cancel_gcode_preview(self):
        preview = getattr(self, "_gcode_preview", None)
        if preview is not None:
            preview["cancel"].set()
        self._gcode_preview = None

    def _gcode_preview_cache_key(self, path):
        try:
            file_stat = os.stat(path)
        except OSError:
            return None
        _x, _y, width, height = _gcode_preview_image_rect()
        return path, file_stat.st_size, file_stat.st_mtime, width, height

    def _cached_gcode_preview_image(self, cache_key):
        if cache_key is None:
            return None
        found, value = self.preview_cache.lookup(cache_key)
        if not found or value is None:
            return None
        image = dict(value["image"])
        image["full_mask_blobs"] = {}
        return image

    def _cache_gcode_preview_image(self, cache_key, image, color, blob):
        if cache_key is None:
            return
        cached_image = dict(image)
        cached_image["full_mask_blobs"] = {color: blob}
        self.preview_cache.store(
            cache_key,
            {"image": cached_image, "color": color, "blob": blob})

    def _gcode_preview_key_for(self, path):
        if not path:
            return None
        return path, _gcode_preview_image_rect()

    def _gcode_preview_ready(self, preview, value, error):
        if getattr(self, "_gcode_preview", None) is not preview:
            return

        key = preview["key"]
        self._stop_gcode_preview_loader()
        if error is not None or value is None:
            logging.info(
                "[feather_screen] gcode preview unavailable for %s", key[0])
            preview["status"] = "failed"
            preview["image"] = None
        else:
            preview["status"] = "ready"
            image, render_key, render_blobs, color, cache_blob = value
            preview["image"] = image
            preview["render_key"] = render_key
            preview["render_blobs"] = render_blobs
            # The next transaction shows these rasters; recolor them later.
            preview["redraw_after"] = self.reactor.monotonic() + GCODE_PREVIEW_REDRAW_PERIOD
            self._cache_gcode_preview_image(
                preview["cache_key"], image, color, cache_blob)

        if self._gcode_preview_key_for(self.virtual_sdcard.file_path()) == key:
            self._request_print_values()

    def _request_gcode_preview_recolor(self, preview, stats, eventtime):
        render_spec = self._gcode_preview_render_spec(stats)
        render_key, layer_state, pending, printed = render_spec
        if render_key == preview.get("render_key"):
            return False
        if preview.get("recolor_pending"):
            return False
        worker = self.file_worker
        if worker is None:
            return False

        image = preview["image"]
        preview["recolor_pending"] = True
        preview["redraw_after"] = eventtime + GCODE_PREVIEW_REDRAW_PERIOD

        def task():
            return colorize_preview(
                image, layer_state, pending, printed)

        def ready(value, error):
            if getattr(self, "_gcode_preview", None) is not preview:
                return
            preview["recolor_pending"] = False
            if error is not None or value is None:
                logging.info("[feather_screen] gcode preview recolor failed")
                return
            preview["render_key"] = render_key
            preview["render_blobs"] = value
            preview["redraw_after"] = (
                self.reactor.monotonic() + GCODE_PREVIEW_REDRAW_PERIOD)
            if self._gcode_preview_key_for(
                    self.virtual_sdcard.file_path()) == preview["key"]:
                self._request_print_values()

        if worker.submit(task, ready):
            return True
        preview["recolor_pending"] = False
        return False

    def _gcode_preview_loading_active(self):
        preview = getattr(self, "_gcode_preview", None)
        return bool(
            preview is not None
            and preview.get("status") == "loading"
            and self.page in (ScreenPage.PRINTING, ScreenPage.PAUSED)
            and self._current_dialog() is None
            and self.print_state in (
                PrintState.PREPARING, PrintState.PRINTING,
                PrintState.PAUSED)
            and self._gcode_preview_key_for(self.virtual_sdcard.file_path())
            == preview.get("key"))

    def _start_gcode_preview_loader(self):
        if not self._gcode_preview_loading_active():
            return
        timer = getattr(self, "_gcode_preview_loader_timer", None)
        if timer is not None:
            return
        self._gcode_preview_loader_phase = 0
        self._gcode_preview_loader_timer = self.reactor.register_timer(
            self._gcode_preview_loader_tick,
            self.reactor.monotonic() + GCODE_PREVIEW_LOADER_PERIOD)

    def _stop_gcode_preview_loader(self):
        timer = getattr(self, "_gcode_preview_loader_timer", None)
        if timer is None:
            return
        self.reactor.update_timer(timer, self.reactor.NEVER)
        self._gcode_preview_loader_timer = None

    def _gcode_preview_loader_tick(self, eventtime):
        if not self._gcode_preview_loading_active():
            self._gcode_preview_loader_timer = None
            return self.reactor.NEVER
        preview = getattr(self, "_gcode_preview", None)
        phase = (getattr(self, "_gcode_preview_loader_phase", 0) + 1) % len(
            GCODE_PREVIEW_LOADER_RADII)
        self._gcode_preview_loader_phase = phase
        if preview is not None:
            preview["loading_phase"] = phase
        # Every transaction on the page writes the loader's disc.
        self._ensure_screen_root().paint()
        return eventtime + GCODE_PREVIEW_LOADER_PERIOD

    def _print_controls_ready(self):
        phase = self._timelapse_phase()
        if phase.locks_print_controls:
            return False
        if phase == TimelapsePhase.USER_PAUSE:
            return True
        if getattr(self, "print_state", None) == PrintState.PREPARING:
            return False
        start = getattr(self, "start_print_macro", None)
        if start is None:
            return True
        return bool(getattr(start, "variables", {}).get(
            "print_started", False))

    def _update_print_progress(self, eventtime):
        if self.page not in (ScreenPage.PRINTING, ScreenPage.PAUSED):
            return
        stats = self.print_stats.get_status(eventtime)
        preview = getattr(self, "_gcode_preview", None)
        if (preview is not None and preview.get("status") == "ready"
                and eventtime >= preview.get("redraw_after", 0.0)):
            self._request_gcode_preview_recolor(preview, stats, eventtime)
        progress, values = self._current_print_progress_values(eventtime, stats)
        changed = self._print_control_values(eventtime)
        changed.update(_progress_state(progress, values))
        self._request_print_values(changed)

    def _current_print_progress_values(self, eventtime, stats=None):
        stats = stats or self.print_stats.get_status(eventtime)
        progress_value = self._print_progress(eventtime, stats)
        progress = int(progress_value * 100)
        elapsed, remaining = self._print_time_values(
            eventtime, stats, progress_value)
        info = stats.get("info", {})
        current, total = info.get("current_layer"), info.get("total_layer")
        layer = "%s / %s" % (current if current is not None else "?",
                              total if total is not None else "?")
        toolhead = self.toolhead.get_status(eventtime)
        position = toolhead.get("position", (0.0, 0.0, 0.0, 0.0))
        motion_report = getattr(self, "motion_report", None)
        if motion_report is not None:
            position = motion_report.get_status(eventtime).get(
                "live_position", position)
        height = float(position[2])
        values = (self._clock_duration(elapsed),
                  self._clock_duration(remaining), layer, round(height, 2))
        return progress, values

    def _print_progress(self, eventtime, stats=None):
        stats = stats or self.print_stats.get_status(eventtime)
        status = self.virtual_sdcard.get_status(eventtime)
        duration = stats.get("print_duration") or 0.0
        sd_progress = status.get("progress") or 0.0

        if not self._print_controls_ready():
            self._progress_start = (duration, sd_progress)
            self._progress_floor = 0.0
            self._progress_source = None
            return 0.0

        progress_start = getattr(self, "_progress_start", (0.0, 0.0))
        if progress_start is None:
            progress_start = self._progress_start = (duration, sd_progress)
        duration_start, sd_start = progress_start
        print_duration = max(0.0, duration - duration_start)
        sd_progress = ((sd_progress - sd_start) / (1.0 - sd_start)
                       if sd_start < 1.0 else 0.0)

        display_status = getattr(self, "display_status", None)
        m73_expiry = float(
            getattr(display_status, "expire_progress", 0.0) or 0.0)
        if m73_expiry > getattr(self, "_m73_start_expiry", 0.0):
            self._m73_active = True
        m73_progress = getattr(display_status, "progress", None)
        estimate = (getattr(self.virtual_sdcard, "estimate_print_time", None)
                    or status.get("estimate_print_time"))

        if getattr(self, "_m73_active", False) and m73_progress is not None:
            progress, source = m73_progress, "M73"
        elif self._restored_print_active(eventtime):
            progress, source = sd_progress, "SD"
        elif estimate:
            progress, source = min(0.99, print_duration / estimate), "TIME"
        else:
            progress, source = sd_progress, "SD"

        progress = max(0.0, min(1.0, progress))
        progress = max(getattr(self, "_progress_floor", 0.0), progress)
        self._progress_floor = progress
        if source != getattr(self, "_progress_source", None):
            logging.info("[feather_screen] print progress source=%s", source)
        self._progress_source = source
        return progress

    def _print_time_values(self, eventtime, stats=None, progress=None):
        stats = stats or self.print_stats.get_status(eventtime)
        duration = float(stats.get("print_duration", 0.0) or 0.0)
        restored = self._restored_print_active(eventtime)
        estimate = getattr(self.virtual_sdcard, "estimate_print_time", None)
        if not estimate:
            if restored:
                return duration, None
            info = stats.get("info", {})
            current = info.get("current_layer")
            total = info.get("total_layer")
            if current and total:
                estimate = duration / max(current, 1) * total
            else:
                if progress is None:
                    progress = self._print_progress(eventtime)
                estimate = duration / progress if progress > 0 else None
        if restored and estimate is not None and progress is not None:
            remaining = float(estimate) * max(
                0.0, 1.0 - max(0.0, min(1.0, progress)))
            return duration, remaining
        if estimate is not None:
            estimate = max(duration, float(estimate))
        remaining = None if estimate is None else max(0.0, estimate - duration)
        return duration, remaining

    def _update_operation_context(self, eventtime):
        operation = self._operation_context_status(eventtime)
        revision = operation["revision"]
        if revision == getattr(self, "_last_operation_revision", -1):
            return
        self._last_operation_revision = revision
        self._reconcile_external_operation(operation)
        self._request_print_values({
            printing_ui.PrintingState.STATUS: self._display_status_parts(status=operation)})

    def _request_operation_cancel(self):
        manager = getattr(self, "operation_context", None)
        if manager is None:
            result = {"status": "not_cancelable", "accepted": False,
                      "request_id": None, "target_name": None,
                      "blocker_name": None}
        else:
            result = manager.request_cancel()
        return result

    def _clear_operation_cancel_request(self):
        manager = getattr(self, "operation_context", None)
        if manager is None:
            return {"status": "not_pending", "cleared": False,
                    "request_id": None}
        return manager.clear_cancel(
            getattr(self, "operation_cancel_request_id", None))
