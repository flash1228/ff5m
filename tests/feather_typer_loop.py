## The renderer's frame loop over a fake Typer, for screen tests of what the loop draws.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import json
import os
import shutil
import sys
import tempfile
import time

from ui.engine import FrameLoop
from ui.renderer import MAX_ATOMIC_DRAW, MAX_BATCH_BYTES

FAKE_TYPER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_typer.py")


def wait_for(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value or time.monotonic() > deadline:
            return value
        time.sleep(0.01)


class TyperLoop:
    """Runs renderer's own frame loop over the fake Typer (launches: its
    behaviours, see fake_typer.py): start() and stop() run as on the printer,
    with only the Typer command line and FIFOs replaced."""

    def __init__(self, renderer, *launches, restart_handler=None):
        self.renderer = renderer
        self.directory = tempfile.mkdtemp(prefix="feather-loop-")
        self.log_path = os.path.join(self.directory, "log")
        config = os.path.join(self.directory, "config")
        with open(config, "w") as target:
            json.dump({"launches": list(launches) or [{}]}, target)
        draw, events = os.path.join(self.directory, "draw"), os.path.join(self.directory, "events")
        renderer._loop = FrameLoop(draw, events, "feather-test-no-such-process", max_frame=MAX_ATOMIC_DRAW,
                                   max_batch=MAX_BATCH_BYTES, backoff_initial=0.01, backoff_max=0.05)
        renderer._typer_arguments = lambda: [
            sys.executable, FAKE_TYPER, config, self.log_path, "--event-pipe", events, "batch", "--pipe", draw]
        renderer._font_manifest_loaded = True
        renderer.configure_worker(lambda old, new: None, restart_handler)
        self.events = []

    def start(self):
        self.renderer.start()
        return self

    def close(self):
        self.renderer.stop()
        shutil.rmtree(self.directory, ignore_errors=True)

    def history(self, launch=None):
        """The command batches the launch drew, in order."""
        if not os.path.exists(self.log_path):
            return []
        with open(self.log_path) as source:
            entries = [json.loads(line) for line in source]
        return [entry["commands"] for entry in entries
                if "commands" in entry and (launch is None or entry["launch"] == launch)]

    def drew(self, text, launch=None):
        """Whether the launch's last batch drew text, once it did (or the timeout)."""
        return bool(wait_for(lambda: any(text in command for batch in self.history(launch)[-1:]
                                         for command in batch)))

    def take(self, until, timeout=5.0):
        """The renderer's events (process_events) until until(events) holds."""
        def done():
            self.events.extend(self.renderer.process_events(time.monotonic()))
            return until(self.events)
        wait_for(done, timeout)
        return self.events
