## File-browser pages for Feather.
##
## Copyright (C) 2025-2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import logging
import os
import time
import threading

from ui import ThemeColor
from ui.lazy import LazyModule
from ff5m_ui.screen import ScreenPage

from feather.files import FileEntry, FileTaskSuperseded, scan_gcode_files
from feather.previews import (
    PREVIEW_MASK_SIZE, PreviewCancelled, colorize_preview, load_preview,
)
from feather.screen.pagination import Pagination


FILE_PRELOAD_LIMIT = 15
FILE_CACHE_TTL = 5.0
# The browser and the pages that return to it with its list. Outside them a list older than FILE_CACHE_TTL is
# released: the next visit would scan again anyway, so keeping it would only hold memory that grows with the files.
FILE_FLOW_PAGES = frozenset((
    ScreenPage.FILE_BROWSER, ScreenPage.FILE_CONFIRM, ScreenPage.PRINT_PREVIEW, ScreenPage.MODEL_PREVIEW))
file_browser_ui = LazyModule("ff5m_ui.file_browser")
file_confirm_ui = LazyModule("ff5m_ui.file_confirm")


class FileBrowserPagesMixin:
    def _configured_file_view(self):
        view = str(self._setting("feather_file_view", "list")).strip().lower()
        return view if view in ("list", "tiles") else "list"

    def _file_page_size(self):
        return len(file_browser_ui.TILES if self.file_view == "tiles" else file_browser_ui.ROWS)

    def _normalize_file_source(self):
        source = self.file_source
        usb_storage = self.usb_storage
        if source == "usb" and (
                usb_storage is None or not usb_storage.available):
            source = "internal"
            self.file_source = source
            self.file_page = 0
        return source

    def _build_file_scan_task(self, source):
        root = self.virtual_sdcard.sdcard_dirname
        history = getattr(self, "print_history", None)
        history_snapshot = dict(getattr(history, "timestamps", {}))
        usb_storage = self.usb_storage
        if source == "usb":
            mount_point = usb_storage.mount_point
            history_prefix = os.path.relpath(mount_point, root)

            def scan_usb():
                try:
                    return scan_gcode_files(
                        mount_point, history_snapshot,
                        history_prefix=history_prefix)
                except RuntimeError as exc:
                    # Removable media can disappear between the monitor tick
                    # and directory traversal. Keep the UI responsive; the
                    # next tick will remove the USB entry if its mount is gone.
                    logging.info(
                        "[feather_screen] USB file scan deferred: %s", exc)
                    return []
            return scan_usb

        excluded = ((usb_storage.mount_point,)
                    if usb_storage is not None else ())
        usb_available = bool(
            usb_storage is not None and usb_storage.available)
        usb_mount_point = (usb_storage.mount_point
                           if usb_storage is not None else None)

        def scan_internal():
            entries = scan_gcode_files(
                root, history_snapshot, excluded_paths=excluded)
            if usb_available:
                entries.insert(0, FileEntry(
                    "USB", usb_mount_point, directory=True))
            return entries
        return scan_internal

    def _load_file_entries(self):
        """Load the current file source synchronously."""
        source = self._normalize_file_source()
        self.file_entries = self._build_file_scan_task(source)()

    def _invalidate_file_entries(self, source=None):
        cache = self.file_entry_cache
        loaded_at = self.file_entry_loaded_at
        if source is None:
            cache.clear()
            loaded_at.clear()
        else:
            cache.pop(source, None)
            loaded_at.pop(source, None)

    def _drop_file_entries(self):
        """Forget every scanned list and the scan in flight: the next browser shows a fresh scan."""
        self._invalidate_file_entries()
        self.file_entries = []
        self.file_scan_token = getattr(self, "file_scan_token", 0) + 1
        self.file_scan_loading = False
        self.file_scan_source = None

    def _release_stale_file_entries(self):
        """Outside the file pages, forget the lists a visit would scan again (see FILE_FLOW_PAGES)."""
        cache = getattr(self, "file_entry_cache", None)
        if not cache or self.page in FILE_FLOW_PAGES:
            return
        for source in tuple(cache):
            self._expire_file_entries_if_stale(source)
        if not cache:
            self.file_entries = []

    def _expire_file_entries_if_stale(self, source):
        cache = self.file_entry_cache
        if source not in cache:
            return True
        loaded_at = self.file_entry_loaded_at
        timestamp = loaded_at.get(source)
        now = self.reactor.monotonic()
        if timestamp is None or now - timestamp >= FILE_CACHE_TTL:
            self._invalidate_file_entries(source)
            return True
        return False

    def _file_scan_shown(self):
        """Whether the browser shows its source's scan: the loader covers the page."""
        return (getattr(self, "file_scan_loading", False) and self.page == ScreenPage.FILE_BROWSER
                and self.file_scan_source == self.file_source)

    def _file_loading_label(self):
        return "LOADING USB FILES..." if self.file_scan_source == "usb" else "LOADING PRINT FILES..."

    def _ensure_file_entries(self):
        """The screen is about to show the browser: without entries for its source, their scan starts."""
        source = self._normalize_file_source()
        if source not in self.file_entry_cache:
            self._start_file_scan(source)

    def _start_file_scan(self, source):
        self._cancel_file_preview()
        self._cancel_gcode_preview()
        if (self.file_scan_loading and self.file_scan_source == source
                and self.file_scan_token > 0):
            return
        self.file_scan_token += 1
        token = self.file_scan_token
        self.file_scan_loading = True
        self.file_scan_source = source
        self.file_scan_phase = 0
        task = self._build_file_scan_task(source)
        submitted = self.file_worker.submit(
            task, lambda entries, error:
            self._finish_file_scan(token, source, entries, error))
        if not submitted:
            self._finish_file_scan(
                token, source, None, RuntimeError("File worker stopped"))

    def _finish_file_scan(self, token, source, entries, error):
        if token != self.file_scan_token:
            return
        self.file_scan_loading = False
        self.file_scan_source = None
        if error is not None:
            logging.error(
                "[feather_screen] unable to scan %s files: %s",
                source, error)
            entries = []
            message = "Unable to load USB files" if source == "usb" \
                else "Unable to load print files"
        else:
            message = None
        valid_preview_keys = {
            self._file_preview_key(entry) for entry in entries
            if not entry.directory
        }
        for cached_source, cached_entries in self.file_entry_cache.items():
            if cached_source == source:
                continue
            valid_preview_keys.update(
                self._file_preview_key(entry) for entry in cached_entries
                if not entry.directory)
        self.file_preview_failures.intersection_update(valid_preview_keys)
        self.file_entry_cache[source] = entries
        self.file_entry_loaded_at[source] = self.reactor.monotonic()
        if source == self.file_source:
            self.file_entries = entries
        if (self._page_paint_allowed(ScreenPage.FILE_BROWSER)
                and source == self.file_source):
            self._render_file_browser()
            if message is not None:
                self._toast(message)

    def _queue_print_history_write(self, task):
        worker = getattr(self, "file_worker", None)
        return worker is not None and worker.submit_write(task)

    def _record_current_print(self):
        history = getattr(self, "print_history", None)
        virtual_sdcard = getattr(self, "virtual_sdcard", None)
        if history is None or virtual_sdcard is None:
            return
        path = (virtual_sdcard.file_path()
                if hasattr(virtual_sdcard, "file_path") else None)
        if not path:
            return
        root = virtual_sdcard.sdcard_dirname
        if history.record(root, path, time.time()):
            self.last_job_path = os.path.relpath(path, root).replace(
                os.sep, "/")
            self.last_job_name = os.path.basename(path)
            self._invalidate_file_entries()

    def _render_file_browser(self):
        source = self._normalize_file_source()
        if self._file_scan_shown() or source not in self.file_entry_cache:
            # The screen starts the scan and shows the loader.
            self._render_screen()
            return
        self.file_entries = self.file_entry_cache[source]
        self._render_file_entries()

    def _render_file_entries(self):
        self._cancel_file_preview()
        self._cancel_gcode_preview()
        self.file_preview_visible_attempted.clear()
        pagination = Pagination(
            self.file_entries, self.file_page, self._file_page_size())
        self.file_page = pagination.page
        self._render_screen()
        if self.file_view == "tiles":
            self._start_next_file_preview(pagination.visible)

    def _file_browser_page(self):
        pages = self.__dict__.setdefault("_file_pages", {})
        if self.file_view not in pages:
            pages[self.file_view] = (file_browser_ui.create_tile_page(PREVIEW_MASK_SIZE) if self.file_view == "tiles"
                                     else file_browser_ui.create_list_page())
        return pages[self.file_view]

    def _file_browser_content(self):
        title = ("Preview files" if getattr(self, "model_preview_file_select", False)
                 else "USB files" if self.file_source == "usb" else "Print files")
        return self._file_browser_page(), title, True

    def _write_file_browser_values(self):
        pagination = Pagination(self.file_entries, self.file_page, self._file_page_size())
        if self.file_view == "tiles":
            values = file_browser_ui.tile_values(
                pagination, tuple(self._file_tile_view(entry) for entry in pagination.visible))
        else:
            values = file_browser_ui.list_values(pagination)
        self._file_browser_page().state.update(values)

    def _file_preview_key(self, entry):
        width, height = PREVIEW_MASK_SIZE
        return (
            entry.path, entry.size, entry.mtime, width, height,
        )

    def _file_preview_candidates(self, visible):
        preload = []
        for entry in self.file_entries:
            if entry.directory:
                continue
            preload.append(entry)
            if len(preload) == FILE_PRELOAD_LIMIT:
                break
        signature = tuple(self._file_preview_key(entry) for entry in preload)
        if signature != self.file_preview_preload_signature:
            self.file_preview_preload_signature = signature
            self.file_preview_preload_attempted = set()
        return list(visible), preload

    def _cached_file_preview(self, entry):
        key = self._file_preview_key(entry)
        found, value = self.preview_cache.lookup(key)
        if found:
            if value is None:
                return True, None
            color = self.renderer.color(ThemeColor.PRIMARY)
            if value["color"] == color:
                return True, value["blob"]
        if key in self.file_preview_failures:
            return True, None
        if not found:
            return found, None

        if self.file_preview_request is None:
            self._submit_file_preview(
                entry, key, cached_image=value["image"])
        return False, None

    def _start_next_file_preview(self, visible):
        if self.file_preview_request is not None:
            return
        visible_entries, preload_entries = self._file_preview_candidates(visible)
        cache = self.preview_cache
        color = self.renderer.color(ThemeColor.PRIMARY)

        for entries, attempted in (
                (visible_entries, self.file_preview_visible_attempted),
                (preload_entries, self.file_preview_preload_attempted)):
            for entry in entries:
                if entry.directory:
                    continue
                key = self._file_preview_key(entry)
                if key in self.file_preview_failures or key in attempted:
                    continue
                found, value = cache.lookup(key)
                if found and (value is None or value["color"] == color):
                    continue
                attempted.add(key)
                if key in self.file_preview_preload_signature:
                    self.file_preview_preload_attempted.add(key)
                cached_image = value["image"] if found else None
                self._submit_file_preview(entry, key, cached_image=cached_image)
                return

    def _submit_file_preview(self, entry, key,
                             cached_image=None):
        width, height = PREVIEW_MASK_SIZE
        color = self.renderer.color(ThemeColor.PRIMARY)
        request = {"key": key, "cancel": threading.Event()}
        self.file_preview_request = request
        worker = self.file_worker

        def task():
            if request["cancel"].is_set():
                raise PreviewCancelled()
            if cached_image is None:
                image = load_preview(entry.path, width, height,
                                     cancel=request["cancel"])
            else:
                image = dict(cached_image)
                image["full_mask_blobs"] = {}
            if image is None:
                return None
            blob = colorize_preview(image, None, color, color)[0]
            return {"image": image, "color": color, "blob": blob}

        if worker is None or not worker.submit(
                task, lambda value, error:
                self._finish_file_preview(request, value, error)):
            self._finish_file_preview(
                request, None, RuntimeError("File preview worker stopped"))

    def _cancel_file_preview(self):
        request = getattr(self, "file_preview_request", None)
        if request is None:
            return
        request["cancel"].set()
        self.file_preview_request = None
        self.file_preview_preload_attempted.discard(request["key"])
        self.file_preview_visible_attempted.discard(request["key"])

    def _finish_file_preview(self, request, value, error):
        if self.file_preview_request is not request:
            return
        self.file_preview_request = None

        if isinstance(error, (FileTaskSuperseded, PreviewCancelled)):
            self.file_preview_preload_attempted.discard(request["key"])
            self.file_preview_visible_attempted.discard(request["key"])
        elif error is not None:
            logging.info(
                "[feather_screen] file preview unavailable for %s: %s",
                request["key"][0], error)
            self.file_preview_failures.add(request["key"])
        else:
            self.file_preview_failures.discard(request["key"])
            self.preview_cache.store(request["key"], value)
        if (self._page_paint_allowed(ScreenPage.FILE_BROWSER)
                and self.file_view == "tiles"
                and not self.file_scan_loading):
            pagination = Pagination(
                self.file_entries, self.file_page, self._file_page_size())
            if any(self._file_preview_key(entry) == request["key"] for entry in pagination.visible):
                self._paint_page_values()
            self._start_next_file_preview(pagination.visible)

    def _file_tile_view(self, entry):
        label = entry.name if entry.directory else os.path.splitext(entry.name)[0]
        detail = "OPEN" if entry.directory else self._format_size(entry.size)
        if entry.directory:
            return file_browser_ui.TileView("folder", None, label, detail)
        found, blob = self._cached_file_preview(entry)
        content = "loading" if not found else "image" if blob is not None else "none"
        return file_browser_ui.TileView(content, blob, label, detail)

    def _handle_file_action(self, action):
        self._require_idle()
        if action == "file.prev":
            self.file_page = max(0, self.file_page - 1)
            self._render_file_browser()
        elif action == "file.next":
            self.file_page += 1
            self._render_file_browser()
        elif action == "file.refresh":
            self.file_page = 0
            self._cancel_file_preview()
            self.file_preview_failures.clear()
            self.file_preview_preload_signature = ()
            self.file_preview_preload_attempted.clear()
            self.file_preview_visible_attempted.clear()
            self._invalidate_file_entries(self.file_source)
            self._render_file_browser()
        elif action in ("file.view.list", "file.view.tiles"):
            view = action.rsplit(".", 1)[-1]
            if view == self.file_view:
                return
            self.params.set_value("feather_file_view", view)
            self.file_view = view
            self.file_page = 0
            self._render_file_browser()
        elif action == "file.mesh.rebuild":
            self.file_confirm_rebuild_mesh = not bool(getattr(
                self, "file_confirm_rebuild_mesh", False))
            if not self.file_confirm_rebuild_mesh:
                self.file_confirm_auto_mesh = False
            self._render_file_confirm()
        elif action == "file.mesh.auto":
            if not getattr(self, "file_confirm_rebuild_mesh", False):
                return
            self.file_confirm_auto_mesh = not bool(getattr(
                self, "file_confirm_auto_mesh", False))
            self._render_file_confirm()
        elif action == "file.start":
            self._start_selected_file()
        elif action == "file.preview":
            self._open_selected_model_preview()
        elif action.startswith("file.item"):
            index = int(action[len("file.item"):])
            pagination = Pagination(
                self.file_entries, self.file_page, self._file_page_size())
            offset = pagination.absolute_index(index)
            if offset is None:
                return
            entry = self.file_entries[offset]
            if entry.directory:
                self.file_source = "usb"
                self.file_page = 0
                self.selected_file = None
                self._cancel_file_preview()
                self._expire_file_entries_if_stale("usb")
                self._render_file_browser()
                return
            if getattr(self, "model_preview_file_select", False):
                self._open_model_preview(entry)
                return
            self.selected_file = entry
            self.file_confirm_return_page = ScreenPage.FILE_BROWSER
            self.file_confirm_repeat = False
            self.file_confirm_rebuild_mesh = False
            self.file_confirm_auto_mesh = False
            self._show_page(ScreenPage.FILE_CONFIRM)

    def _open_last_job(self):
        self._require_idle()
        relative = getattr(self, "last_job_path", None)
        if not relative:
            history = getattr(self, "print_history", None)
            relative = (history.latest_path()
                        if history is not None else None)
        if not relative:
            raise RuntimeError("No previous print is available")
        root = os.path.realpath(self.virtual_sdcard.sdcard_dirname)
        path = os.path.realpath(os.path.join(root, relative))
        if not os.path.isfile(path) or not path.startswith(root + os.sep):
            raise RuntimeError("The last print file is no longer available")
        stat = os.stat(path)
        self.last_job_path = relative
        self.last_job_name = os.path.basename(relative)
        self.selected_file = FileEntry(
            self.last_job_name, path, size=stat.st_size, mtime=stat.st_mtime)
        self.file_confirm_return_page = ScreenPage.IDLE_HOME
        self.file_confirm_repeat = True
        self.file_confirm_rebuild_mesh = False
        self.file_confirm_auto_mesh = False
        self._show_page(ScreenPage.FILE_CONFIRM)

    def _render_file_confirm(self):
        self._render_screen()

    def _file_confirm_page(self):
        repeat = bool(getattr(self, "file_confirm_repeat", False))
        pages = self.__dict__.setdefault("_file_confirm_pages", {})
        if repeat not in pages:
            pages[repeat] = file_confirm_ui.create_page(self.renderer, repeat)
        return pages[repeat]

    def _file_confirm_content(self):
        title = "Print again?" if getattr(self, "file_confirm_repeat", False) else "Start print?"
        return self._file_confirm_page(), title, True

    def _write_file_confirm_values(self):
        entry = self.selected_file
        keys = file_confirm_ui.ConfirmState
        self._file_confirm_page().state.update({
            keys.NAME: entry.name,
            keys.SIZE: self._format_size(entry.size),
            keys.HINT: ("KAMP ENABLED - FULL MESH WILL RUN INSTEAD" if bool(self._setting("use_kamp", False))
                        else "FOR THIS PRINT ONLY"),
            keys.REBUILD: bool(getattr(self, "file_confirm_rebuild_mesh", False)),
            keys.AUTO: bool(getattr(self, "file_confirm_auto_mesh", False)),
        })

    def _start_selected_file(self):
        self._require_idle()
        root = os.path.realpath(self.virtual_sdcard.sdcard_dirname)
        path = os.path.realpath(self.selected_file.path)
        if not os.path.isfile(path) or not path.startswith(root + os.sep):
            raise RuntimeError("Selected file is no longer available")
        file_stat = os.stat(path)
        self.selected_file.size = file_stat.st_size
        self.selected_file.mtime = file_stat.st_mtime
        relpath = os.path.relpath(path, root)
        if any(ord(ch) < 32 for ch in relpath):
            raise RuntimeError("Unsupported filename")
        escaped = relpath.replace("\\", "\\\\").replace('"', '\\"')
        self.last_job_path = relpath.replace(os.sep, "/")
        self.last_job_name = os.path.basename(relpath)
        rebuild_mesh = bool(getattr(
            self, "file_confirm_rebuild_mesh", False))
        auto_mesh = bool(getattr(
            self, "file_confirm_auto_mesh", False) and rebuild_mesh)
        # The virtual-SD timer cannot consume the file until this script
        # yields. Staging after file acceptance avoids a stale one-print choice
        # when SDCARD_PRINT_FILE rejects the path or an already-active job.
        force_leveling = "True" if rebuild_mesh else "None"
        # G-code parsing consumes the outer quotes; literal_eval() in
        # SET_GCODE_VARIABLE must still receive the inner quoted string.
        mesh_name = "'\"auto\"'" if auto_mesh else "None"
        self._run_script("\n".join((
            'SDCARD_PRINT_FILE FILENAME="%s"' % escaped,
            "SET_GCODE_VARIABLE MACRO=START_PRINT "
            "VARIABLE=feather_force_leveling VALUE=%s" % force_leveling,
            "SET_GCODE_VARIABLE MACRO=START_PRINT "
            "VARIABLE=feather_mesh_name VALUE=%s" % mesh_name,
        )))
        self.file_confirm_rebuild_mesh = False
        self.file_confirm_auto_mesh = False
        # Render before the virtual-SD timer can begin START_PRINT and homing.
        self._reconcile_print_state(self.reactor.monotonic())
