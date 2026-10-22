## The optional native accelerator falls back to the Python UI on every failure.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import json
import os
import pathlib
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

PLUGINS = pathlib.Path(__file__).parents[1] / ".py" / "klipper" / "plugins"
sys.path.insert(0, str(PLUGINS))

import feather_native  # noqa: E402


class Installed:
    pass


class LoaderTests(unittest.TestCase):
    def setUp(self):
        feather_native._attempted, feather_native._installation = False, None
        self.addCleanup(setattr, feather_native, "_attempted", False)
        self.addCleanup(setattr, feather_native, "_installation", None)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = pathlib.Path(directory.name)
        (self.root / "ui").mkdir()
        (self.root / "ui" / "layout.py").write_text("LAYOUT = 1\n")
        self.binary = self.root / "feather_native.so"
        self.binary.write_bytes(b"\x7fELF native")
        self.manifest = self.root / "manifest.json"
        self.cache = self.root / "digests.json"
        self.write_manifest()

    def write_manifest(self, **changes):
        values = dict(abi=1, binary_sha256=feather_native.file_digest(str(self.binary)),
                      framework_sha256=feather_native.framework_digest(str(self.root / "ui")))
        values.update(changes)
        self.manifest.write_text(json.dumps(values))

    def enable(self, module=None, flag=None):
        module = module or SimpleNamespace(ABI=1, install=Installed)
        with mock.patch.object(feather_native, "_load", return_value=module) as load:
            result = feather_native.enable(str(self.binary), str(self.manifest), str(self.root / "ui"),
                                           str(flag or self.root / "missing.off"), str(self.cache))
        return result, load

    def enable_again(self):
        feather_native._attempted, feather_native._installation = False, None
        with mock.patch.object(feather_native, "file_digest", wraps=feather_native.file_digest) as hashed:
            result, _load = self.enable()
        return result, hashed

    def test_a_validated_binary_is_installed_once(self):
        with self.assertLogs(level="INFO") as logs:
            result, load = self.enable()
        self.assertIsInstance(result, Installed)
        self.assertIn("enabled", "\n".join(logs.output))
        again, second = self.enable()
        self.assertIs(again, result)
        second.assert_not_called()

    def test_the_disable_flag_keeps_the_python_ui(self):
        flag = self.root / "feather_native.off"
        flag.write_text("")
        result, load = self.enable(flag=flag)
        self.assertIsNone(result)
        load.assert_not_called()

    def test_missing_files_keep_the_python_ui(self):
        self.binary.unlink()
        result, load = self.enable()
        self.assertIsNone(result)
        load.assert_not_called()

    def test_mismatches_and_failures_keep_the_python_ui(self):
        def failing_install():
            raise RuntimeError("native install failed")

        cases = (
            ("binary", lambda: self.binary.write_bytes(b"\x7fELF changed"), None),
            ("framework", lambda: (self.root / "ui" / "layout.py").write_text("LAYOUT = 2\n"), None),
            ("abi", lambda: None, SimpleNamespace(ABI=2, install=Installed)),
            ("install", lambda: None, SimpleNamespace(ABI=1, install=failing_install)),
            ("manifest", lambda: self.manifest.write_text("{"), None),
        )
        for name, change, module in cases:
            with self.subTest(case=name):
                self.setUp()
                change()
                with self.assertLogs(level="WARNING") as logs:
                    result, _load = self.enable(module)
                self.assertIsNone(result)
                self.assertIn("disabled", logs.output[0])

    def test_unchanged_files_are_not_hashed_again(self):
        self.assertIsNotNone(self.enable()[0])
        result, hashed = self.enable_again()
        self.assertIsInstance(result, Installed)
        hashed.assert_not_called()

    def test_a_changed_file_is_hashed_again_and_still_disables(self):
        self.assertIsNotNone(self.enable()[0])
        layout = self.root / "ui" / "layout.py"
        stat = layout.stat()
        layout.write_text("LAYOUT = 2\n")  # Same size; only the modification time tells it apart.
        os.utime(layout, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        with self.assertLogs(level="WARNING") as logs:
            result, hashed = self.enable_again()
        self.assertIsNone(result)
        self.assertIn("framework", logs.output[0])
        hashed.assert_called_once_with(str(layout))

    def test_a_corrupt_or_unwritable_cache_keeps_the_native_ui(self):
        self.cache.write_text("[")
        self.assertIsInstance(self.enable()[0], Installed)
        self.cache = self.root / "missing" / "digests.json"
        self.assertIsInstance(self.enable_again()[0], Installed)

    def test_framework_digest_follows_module_names_and_contents(self):
        before = feather_native.framework_digest(str(self.root / "ui"))
        (self.root / "ui" / "notes.txt").write_text("data")
        self.assertEqual(feather_native.framework_digest(str(self.root / "ui")), before)
        (self.root / "ui" / "extra.py").write_text("")
        self.assertNotEqual(feather_native.framework_digest(str(self.root / "ui")), before)


if __name__ == "__main__":
    unittest.main()
