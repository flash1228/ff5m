## Bounded main-thread CPU profiling for on-printer UI diagnostics.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import json
import logging
import math
import os
import sys
import tempfile
import threading
import time

from .artifacts import ARTIFACT_ROOT


MAX_PROFILE_SECONDS = 15.0


class CPUProfile:
    """Own one CPU-clock profiler and its deadline, independent of rendering."""

    def __init__(self, reactor, safe, artifact_root=ARTIFACT_ROOT):
        self.reactor = reactor
        self.safe = safe
        self.artifact_root = artifact_root
        self.profiler = None
        self.timer = None
        self.directory = None
        self.started_at = None
        self.started_cpu = None
        self.result = None

    @property
    def running(self):
        return self.profiler is not None

    def start(self, duration):
        duration = float(duration)
        if not math.isfinite(duration) or not 1.0 <= duration <= MAX_PROFILE_SECONDS:
            raise ValueError("CPU profile duration must be between 1 and %g seconds" % MAX_PROFILE_SECONDS)
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("CPU profiling must run on the main thread")
        if self.running or sys.getprofile() is not None:
            raise RuntimeError("A profiler is already running")
        if not self.safe():
            raise RuntimeError("CPU profiling requires an idle printer with heaters off")

        import cProfile
        profiler = cProfile.Profile(timer=time.thread_time)
        os.makedirs(self.artifact_root, exist_ok=True)
        self.directory = tempfile.mkdtemp(prefix="cpu-profile-", dir=self.artifact_root)
        self.started_at = time.monotonic()
        self.started_cpu = time.thread_time()
        self.result = None
        try:
            self.timer = self.reactor.register_timer(self._deadline, self.reactor.NEVER)
            self.reactor.update_timer(self.timer, self.reactor.monotonic() + duration)
            profiler.enable()
            self.profiler = profiler
        except BaseException:
            profiler.disable()
            self._remove_timer()
            raise

    def _remove_timer(self):
        timer, self.timer = self.timer, None
        if timer is not None:
            self.reactor.unregister_timer(timer)

    def _deadline(self, eventtime):
        try:
            allowed = self.safe()
        except Exception:
            self.stop("safety_check_failed", save=False)
            return self.reactor.NEVER
        self.stop("duration" if allowed else "printer_not_idle", save=allowed)
        return self.reactor.NEVER

    def stop(self, reason="requested", save=True):
        profiler = self.profiler
        if profiler is None:
            return self.result
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("CPU profiling must stop on the main thread")

        ended_cpu = time.thread_time()
        ended_at = time.monotonic()
        profiler.disable()
        self.profiler = None
        self._remove_timer()
        result = {
            "reason": reason, "saved": False, "pid": os.getpid(),
            "clock": "thread_cpu", "directory": self.directory,
            "wall_seconds": ended_at - self.started_at,
            "thread_cpu_seconds": ended_cpu - self.started_cpu,
            "context_note": "Inclusive caller edges can cross greenlet scheduling; use exclusive CPU for attribution.",
        }
        self.result = result
        if not save:
            return result

        try:
            import pstats
            profiler.dump_stats(os.path.join(self.directory, "profile.pstats"))
            stats = pstats.Stats(profiler)
            functions = []
            for (filename, line, name), (primitive, calls, own, total, callers) in stats.stats.items():
                functions.append({
                    "file": filename, "line": line, "function": name,
                    "primitive_calls": primitive, "calls": calls,
                    "self_cpu_seconds": own, "inclusive_cpu_seconds": total,
                })
            functions.sort(key=lambda row: row["self_cpu_seconds"], reverse=True)
            result.update(saved=True, functions=len(functions), total_self_cpu_seconds=stats.total_tt)
            with open(os.path.join(self.directory, "functions.json"), "w", encoding="utf-8") as stream:
                json.dump(functions, stream, indent=2)
            with open(os.path.join(self.directory, "metadata.json"), "w", encoding="utf-8") as stream:
                json.dump(result, stream, indent=2)
        except Exception as error:
            result["saved"] = False
            result["error"] = type(error).__name__
            logging.error("[feather_ui_test] CPU profile could not be saved (%s)", result["error"])
        return result

    def get_status(self):
        status = {"running": self.running, "directory": self.directory}
        if self.result is not None:
            status.update((key, self.result[key]) for key in ("reason", "saved", "error") if key in self.result)
        return status
