## The Feather UI fingerprint: which deployed sources make up the UI, hashed once.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

"""One fingerprint for the printer's UI test and the host's hybrid tool.

The host loads this file by path, without its package, so it imports nothing but the standard library.
"""

import hashlib
import os


UI_FINGERPRINT_FILES = (
    "feather_screen.py",
    "object_exclusion.py",
    "feather_ui_test/fixtures/exclude_models.json",
    "feather/gcode_info.py",
    "feather/model_preview.py",
    "feather/screen/model_preview.py",
    "feather/screen/pages/model_preview.py",
    "feather/calibration/extruder.py",
    "feather/calibration/shaper.py",
    "feather/calibration/z.py",
    "feather/control/joystick.py",
    "feather/control/motion.py",
    "feather/features/benchmark.py",
    "feather/features/calibration.py",
    "feather/features/extruder.py",
    "feather/features/filament.py",
    "feather/features/manager.py",
    "feather/features/settings.py",
    "feather/features/ui_test.py",
    "feather/features/z.py",
    "feather/network/pages.py",
    "feather/screen/controls.py",
    "feather/screen/keyboard.py",
    "feather/screen/pagination.py",
    "feather/screen/pages/__init__.py",
    "feather/screen/pages/exclude_objects.py",
    "feather/screen/pages/files.py",
    "feather/screen/pages/home.py",
    "feather/screen/pages/mod_editor.py",
    "feather/screen/pages/mod_settings.py",
    "feather/screen/pages/print_cancel.py",
    "feather/screen/pages/printing.py",
    "feather/screen/pages/recovery.py",
    "feather/screen/pages/settings.py",
    "feather/settings/mod.py",
    "feather/testing/operation_context_fixtures.py",
    "feather/update_notification.py",
)
UI_FINGERPRINT_PACKAGES = ("ui", "ff5m_ui", "feather_ui_test")


def ui_fingerprint(plugins_root):
    """SHA-256 over the relative path and content of every UI source and indexed asset under plugins_root."""
    files = []
    for relative in UI_FINGERPRINT_FILES:
        path = os.path.join(plugins_root, relative)
        if os.path.isfile(path):
            files.append((relative, path))
    for package in UI_FINGERPRINT_PACKAGES:
        for current, directories, names in os.walk(os.path.join(plugins_root, package)):
            directories[:] = [name for name in directories if name != "__pycache__"]
            for name in names:
                if name.endswith((".py", ".fxi1")):
                    path = os.path.join(current, name)
                    files.append((os.path.relpath(path, plugins_root).replace(os.sep, "/"), path))
    digest = hashlib.sha256()
    for relative, path in sorted(files):
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(128 * 1024), b""):
                digest.update(chunk)
        digest.update(b"\0")
    return digest.hexdigest()
