# Bounded acquisition and native input-shaper calibration.
# Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
# This file may be distributed under the terms of the GNU GPLv3 license.

import collections
import importlib
import logging
import math
import os
import shutil
import struct
import subprocess
import tempfile
import time

from .shaper_storage import METHODS, REFERENCE_METHODS, ResultHistory, disk_directory, load_result

MAX_PENDING_BYTES = 64 * 1024
SAMPLE = struct.Struct("<4d")

class NativeProcess:
    """One finite child, polled cooperatively; no result objects travel in pipes."""
    def __init__(self, reactor, args, log_path, capture=False):
        self.reactor = reactor
        self.log_path = log_path
        with open(log_path, "wb") as log:
            self.process = subprocess.Popen(args, stdin=subprocess.PIPE if capture else subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=log, close_fds=True)

    def wait(self, cancelled, timeout):
        deadline = self.reactor.monotonic() + timeout
        while self.process.poll() is None:
            if cancelled():
                raise RuntimeError("Shaper calibration cancelled")
            now = self.reactor.monotonic()
            if now >= deadline:
                raise RuntimeError("Shaper worker timed out")
            self.reactor.pause(now + .05)
        if self.process.returncode:
            with open(self.log_path, "rb") as log:
                log.seek(0, os.SEEK_END)
                log.seek(max(0, log.tell() - 2048))
                detail = log.read().decode("utf-8", "replace").strip()
            raise RuntimeError("Shaper worker exited %s: %s" % (self.process.returncode, detail))

    def close(self):
        pipe = self.process.stdin
        if pipe is not None and not pipe.closed:
            pipe.close()
        if self.process.poll() is None:
            self.process.terminate()
            deadline = self.reactor.monotonic() + 1.
            while self.process.poll() is None and self.reactor.monotonic() < deadline:
                self.reactor.pause(self.reactor.monotonic() + .02)
            if self.process.poll() is None:
                self.process.kill()
        self.process.wait()


class CaptureClient:
    """An ADXL API client whose retained Python storage is bounded by one queue."""
    def __init__(self, reactor, executable, path, log_path):
        self.reactor = reactor
        self.worker = NativeProcess(reactor, [executable, "capture", path], log_path, capture=True)
        self.pending = bytearray()
        self.done = False
        self.error = None
        self.errors = self.overflows = 0
        self.samples = 0
        self.peak_pending = 0
        self.handler = None
        try:
            fd = self.worker.process.stdin.fileno()
            os.set_blocking(fd, False)
            self.handler = reactor.register_fd(fd, None, self._flush)
            reactor.set_fd_wake(self.handler, False, False)
        except Exception:
            self.worker.close()
            raise

    def get_client_connection(self):
        return self

    def get_dict(self, _name, _default):
        return {}

    def is_closed(self):
        return self.done or self.error is not None

    def send(self, message):
        if self.is_closed():
            return
        try:
            params = message["params"]
            errors, overflows = params.get("errors", 0), params.get("overflows", 0)
            self.errors, self.overflows = errors, overflows
            if self.errors or self.overflows:
                raise RuntimeError("Accelerometer reported errors or overflow")
            data = params["data"]
            if len(self.pending) + len(data) * SAMPLE.size > MAX_PENDING_BYTES:
                raise RuntimeError("Shaper capture queue full; storage cannot keep up")
            for row in data:
                self.pending.extend(SAMPLE.pack(*row))
            self.samples += len(data)
            self.peak_pending = max(self.peak_pending, len(self.pending))
            self._flush(self.reactor.monotonic())
        except Exception as error:
            self.error = str(error)

    def _flush(self, _eventtime):
        if self.error:
            return
        try:
            while self.pending:
                try:
                    count = os.write(self.worker.process.stdin.fileno(), self.pending)
                except BlockingIOError:
                    break
                if count <= 0:
                    raise RuntimeError("Shaper capture pipe closed")
                del self.pending[:count]
            self.reactor.set_fd_wake(self.handler, False, bool(self.pending))
        except Exception as error:
            self.error = str(error)
            self.reactor.set_fd_wake(self.handler, False, False)

    def finish(self, cancelled):
        self.done = True
        deadline = self.reactor.monotonic() + 5.
        while self.pending and not self.error:
            if cancelled():
                raise RuntimeError("Shaper calibration cancelled")
            self._flush(self.reactor.monotonic())
            if self.reactor.monotonic() >= deadline:
                raise RuntimeError("Shaper capture drain timed out")
            if self.pending:
                self.reactor.pause(self.reactor.monotonic() + .02)
        if self.error:
            raise RuntimeError(self.error)
        self.reactor.unregister_fd(self.handler)
        self.handler = None
        self.worker.process.stdin.close()
        self.worker.wait(cancelled, 5.)

    def close(self):
        self.done = True
        if self.handler is not None:
            self.reactor.unregister_fd(self.handler)
            self.handler = None
        self.worker.close()
        self.pending.clear()


class SweepCommand:
    """Check the job between sweep frequency reports without altering its moves."""
    def __init__(self, command, check, progress):
        self.command, self.check, self.progress = command, check, progress

    def __getattr__(self, name):
        return getattr(self.command, name)

    def respond_info(self, *args, **kwargs):
        self.check()
        if args:
            self.progress(args[0])
        return self.command.respond_info(*args, **kwargs)


class Job:
    """Progress of one FORGE_SHAPER_CALIBRATE command, published until the next one starts."""
    def __init__(self, job_id=0, axes=(), started_at=None):
        self.id = job_id
        self.requested_axes, self.completed_axes = tuple(axes), ()
        self.axis = ""
        self.frequency = self.freq_start = self.freq_end = None
        self.samples = 0
        self.started_at, self.finished_at = started_at, None
        self.cancelled = False

    def elapsed(self, eventtime):
        if self.started_at is None:
            return 0.
        end = self.finished_at if self.finished_at is not None else eventtime
        return max(0., end - self.started_at)


# One saved history record, held in memory only while it is open.
Record = collections.namedtuple("Record", "id saved_at results")


class ForgeShaper:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.executable = config.get("executable", "/opt/config/mod/.bin/exec/forge-shaper")
        self.directory = config.get("directory", "/opt/config/mod_data/shaper")
        self.timeout = config.getfloat("timeout", 180., above=0.)
        self.keep_raw = config.getboolean("keep_raw", False)
        # Saved results belong to the history on disk; only the open record
        # and the record it is compared with are kept in memory.
        self.history = ResultHistory(self.directory)
        self.record = self.reference = None
        self.revision = 0
        self.state, self.error, self.busy = "idle", "", False
        self.job = Job()
        self.worker = self.capture = None
        self.gcode.register_command("FORGE_SHAPER_CALIBRATE", self.cmd_CALIBRATE,
                                    desc="Run bounded native shaper calibration")
        self.gcode.register_command("FORGE_SHAPER_APPLY", self.cmd_APPLY,
                                    desc="Stage a selected shaper for SAVE_CONFIG")
        self.gcode.register_command("FORGE_SHAPER_LOAD", self.cmd_LOAD,
                                    desc="Open the last saved shaper result without measuring")
        self.gcode.register_command("FORGE_SHAPER_CANCEL", lambda gcmd: self.cancel(),
                                    desc="Cancel a native shaper job at a sweep boundary")
        immediate = getattr(self.gcode, "register_immediate_command", None)
        if immediate is not None:
            immediate("FORGE_SHAPER_CANCEL")
        self.printer.register_event_handler("klippy:disconnect", self.disconnect)

    def get_history(self):
        return self.history.entries()

    def _open(self, entries, index, results=None):
        """Open entries[index] for inspection and FORGE_SHAPER_APPLY.

        The latest record is compared with its predecessor, an older record
        with the latest one.
        """
        entry = entries[index]
        if results is None:
            results, _ = self.history.load(entry["id"])
        reference = None
        if len(entries) > 1:
            other = entries[1 if index == 0 else 0]
            try:
                reference = Record(other["id"], other["saved_at"], self.history.load(other["id"])[0])
            except (OSError, ValueError, KeyError, struct.error) as error:
                # A damaged comparison record must not hide the selected one.
                logging.warning("[forge_shaper] Unable to load reference result: %s", error)
        self.record, self.reference = Record(entry["id"], entry["saved_at"], results), reference
        self.state, self.error = "complete", ""
        self.revision += 1

    def restore_saved(self, result_id=None):
        if self.busy:
            raise ValueError("Shaper calibration is running")
        entries = self.history.entries()
        if not entries:
            raise ValueError("No saved shaper result")
        index = next((i for i, e in enumerate(entries) if e["id"] == result_id), None) if result_id else 0
        if index is None:
            raise ValueError("Shaper result is no longer available")
        self._open(entries, index)

    def cmd_LOAD(self, gcmd):
        try:
            self.restore_saved(gcmd.get("RESULT", None))
        except ValueError as error:
            raise gcmd.error(str(error))
        gcmd.respond_info("Last shaper result loaded")

    def cancel(self):
        if self.busy:
            self.job.cancelled = True

    def disconnect(self):
        self.cancel()
        if self.capture:
            self.capture.close()
        if self.worker:
            self.worker.close()

    def _check(self):
        if self.job.cancelled:
            raise RuntimeError("Shaper calibration cancelled")
        if self.capture and self.capture.error:
            raise RuntimeError(self.capture.error)

    def _set_phase(self, phase):
        self.state = phase
        context = self.printer.lookup_object("operation_context", None)
        types = context.get_status(self.reactor.monotonic()).get("context_types", ()) if context else ()
        if types and types[-1] == "input_shaper":
            self.gcode.run_script_from_command("_CONTEXT_STATE NAME=" + phase.upper())

    def cmd_CALIBRATE(self, gcmd):
        if self.busy:
            raise gcmd.error("Shaper calibration is already running")
        axes = gcmd.get("AXIS", "xy").lower()
        if axes not in ("x", "y", "xy"):
            raise gcmd.error("AXIS must be x, y or xy")
        now = self.reactor.monotonic()
        stats = self.printer.lookup_object("print_stats", None)
        sd = self.printer.lookup_object("virtual_sdcard", None)
        if (stats is not None and stats.get_status(now).get("state") in ("printing", "paused")) or (
                sd is not None and sd.is_active()):
            raise gcmd.error("Shaper calibration is unavailable during a print")
        tester = self.printer.lookup_object("resonance_tester")
        if len(tester.test.get_start_test_points()) != 1:
            raise gcmd.error("Native shaper calibration supports one probe point; use standard calibration for multiple points")
        axis_type = importlib.import_module(tester.__class__.__module__).TestAxis
        chips = {}
        for axis in axes:
            matching = [chip for chip_axis, chip in tester.accel_chips if axis_type(axis).matches(chip_axis)]
            if len(matching) != 1 or not hasattr(matching[0], "api_dump"):
                raise gcmd.error("Native shaper calibration requires one ADXL API sensor per axis")
            if matching[0].is_measuring():
                raise gcmd.error("Accelerometer is already in use")
            chips[axis] = matching[0]
        if not os.path.isfile(self.executable) or not os.access(self.executable, os.X_OK):
            raise gcmd.error("Native shaper executable is not installed")
        limit = gcmd.get_float("MAX_SMOOTHING", tester.max_smoothing or 0., minval=0.)
        try:
            directory = disk_directory(self.directory)
            job = tempfile.mkdtemp(prefix="job-", dir=directory)
        except (OSError, ValueError) as error:
            raise gcmd.error("Shaper storage unavailable: %s" % error)
        self.busy, self.state, self.error = True, "preparing", ""
        self.job = Job(self.job.id + 1, axes, self.reactor.monotonic())
        results = {}
        try:
            tester.test.prepare_test(gcmd)
            self.job.freq_start = tester.test.freq_start
            self.job.freq_end = tester.test.freq_end
            for axis in axes:
                self._check()
                self.job.axis, self.job.frequency, self.job.samples = axis, None, 0
                source = os.path.join(job, axis + ".samples")
                start, end = self._measure(gcmd, tester, axis_type(axis), chips[axis], source)
                self._check()
                self._set_phase("processing")
                output = os.path.join(job, axis + ".json")
                scv = self.printer.lookup_object("toolhead").square_corner_velocity
                args = [self.executable, "analyze", source, output, "--axis", axis,
                        "--scv", str(scv), "--max-smoothing", str(limit),
                        "--start", str(start), "--end", str(end)]
                self.worker = NativeProcess(self.reactor, args, os.path.join(job, axis + ".analysis.log"))
                try:
                    self.worker.wait(lambda: self.job.cancelled, self.timeout)
                    results[axis] = load_result(output)
                    if results[axis]["axis"] != axis:
                        raise RuntimeError("Shaper result axis mismatch")
                    self.job.completed_axes = tuple(results)
                finally:
                    self.worker.close()
                    self.worker = None
                if not self.keep_raw:
                    os.unlink(source)
            self._check()
            entries = self.history.save(results, time.time())
            # No partial result is published and no printer setting changes automatically.
            self._open(entries, 0, results)
            for axis, result in results.items():
                best = next(s for s in result["shapers"] if s["name"] == result["recommended"])
                gcmd.respond_info("%s: recommended %s at %.1f Hz" % (
                    axis.upper(), best["name"], best["freq"]))
            gcmd.respond_info("Review results, then FORGE_SHAPER_APPLY AXIS=x SHAPER=<method>; SAVE_CONFIG remains explicit")
        except Exception as error:
            self.state = "cancelled" if self.job.cancelled else "error"
            self.error = str(error)
            raise gcmd.error(self.error)
        finally:
            if not self.keep_raw or self.state != "complete":
                shutil.rmtree(job, ignore_errors=True)
            self.job.finished_at = self.reactor.monotonic()
            self.busy = False

    def _sweep_progress(self, message):
        # VibrationPulseTest exposes frequency reports, not a progress hook.
        # Observe its bounded reports without changing the movement generator.
        if message.startswith("Testing frequency ") and message.endswith(" Hz"):
            try:
                frequency = float(message[len("Testing frequency "):-len(" Hz")])
            except ValueError:
                return
            if math.isfinite(frequency):
                self.job.frequency = max(self.job.freq_start, min(self.job.freq_end, frequency))

    def _measure(self, gcmd, tester, axis, chip, path):
        self._set_phase("positioning")
        toolhead = self.printer.lookup_object("toolhead")
        toolhead.manual_move(tester.test.get_start_test_points()[0], tester.move_speed)
        toolhead.wait_moves()
        toolhead.dwell(.5)
        before = toolhead.get_status(self.reactor.monotonic())
        start = toolhead.get_last_move_time()
        self.capture = CaptureClient(self.reactor, self.executable, path, path + ".log")
        try:
            chip.api_dump.add_client(self.capture)
            self._set_phase("measuring")
            try:
                tester.test.run_test(axis, SweepCommand(gcmd, self._check, self._sweep_progress))
            except Exception:
                self.gcode.run_script_from_command("SET_VELOCITY_LIMIT ACCEL=%.9g ACCEL_TO_DECEL=%.9g" % (
                    before["max_accel"], before["max_accel_to_decel"]))
                shaper = self.printer.lookup_object("input_shaper", None)
                if shaper is not None and not gcmd.get_int("INPUT_SHAPING", 0):
                    shaper.enable_shaping()
                raise
            end = toolhead.get_last_move_time()
            toolhead.wait_moves()
            self.capture.finish(lambda: self.job.cancelled)
            return start, end
        finally:
            self.job.samples = self.capture.samples
            self.capture.close()
            self.capture = None

    def cmd_APPLY(self, gcmd):
        if self.busy or self.state != "complete":
            raise gcmd.error("No completed shaper result to apply")
        axis, method = gcmd.get("AXIS").lower(), gcmd.get("SHAPER").lower()
        result = self.record.results.get(axis)
        if method in REFERENCE_METHODS:
            raise gcmd.error("ZVD is reference only; firmware support has not been enabled")
        if result is None or method not in METHODS:
            raise gcmd.error("Unknown measured axis or shaper method")
        shaper = next(s for s in result["shapers"] if s["name"] == method)
        configfile = self.printer.lookup_object("configfile")
        configfile.set("input_shaper", "shaper_type_" + axis, method)
        configfile.set("input_shaper", "shaper_freq_" + axis, "%.1f" % shaper["freq"])
        gcmd.respond_info("Staged %s: %s %.1f Hz. SAVE_CONFIG saves and restarts Klipper." % (
            axis.upper(), method, shaper["freq"]))

    def get_status(self, eventtime):
        job = self.job
        return {"state": self.state, "error": self.error, "busy": self.busy,
                "revision": self.revision, "axes": sorted(self.record.results) if self.record else [],
                "job_id": job.id, "axis": job.axis,
                "requested_axes": job.requested_axes, "completed_axes": job.completed_axes,
                "frequency": job.frequency, "freq_start": job.freq_start, "freq_end": job.freq_end,
                "samples": self.capture.samples if self.capture else job.samples,
                "elapsed": job.elapsed(eventtime), "cancel_requested": job.cancelled}


def load_config(config):
    return ForgeShaper(config)
