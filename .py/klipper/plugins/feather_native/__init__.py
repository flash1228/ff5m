## Optional native acceleration of the Feather UI.
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

"""Load the optional native Feather accelerator; Feather keeps working without it.

The binary is validated against the exact framework sources it was tested with.
Any mismatch, a missing binary, the disable flag or a failed install leaves the
pure Python UI in place and logs why. The attempt is made once per process.
"""

import hashlib
import importlib.util
import json
import logging
import os
import time
from importlib.machinery import ExtensionFileLoader

PACKAGE_DIR = os.path.dirname(os.path.realpath(__file__))
BINARY = os.path.join(PACKAGE_DIR, "feather_native.so")
MANIFEST = os.path.join(PACKAGE_DIR, "manifest.json")
UI_ROOT = os.path.join(os.path.dirname(PACKAGE_DIR), "ui")
DISABLE_FLAG = "/opt/config/mod_data/feather_native.off"
DIGEST_CACHE = "/opt/config/mod_data/feather_native.digests.json"

_attempted = False
_installation = None


def file_digest(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def framework_digest(root=UI_ROOT, digest_of=file_digest):
    """SHA-256 over the framework modules, by name and content."""
    digest = hashlib.sha256()
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if name.endswith(".py") and os.path.isfile(path):
            digest.update(name.encode("utf-8") + b"\0" + digest_of(path).encode("ascii") + b"\n")
    return digest.hexdigest()


class DigestCache:
    """File digests remembered by size and modification time.

    Hashing the binary and the framework costs ~0.13 s at every printer start, while the
    files change only on update. A file whose size or mtime differs is hashed again, so a
    changed file still fails the manifest check. An unreadable cache is simply rebuilt.
    """

    def __init__(self, path):
        self.path = path
        try:
            with open(path, "r", encoding="utf-8") as stream:
                self._known = json.load(stream)
        except (OSError, ValueError):
            self._known = {}
        if not isinstance(self._known, dict):
            self._known = {}
        self._seen = {}

    def digest(self, path):
        stat = os.stat(path)
        key = [stat.st_size, stat.st_mtime_ns]
        entry = self._known.get(path)
        value = entry[2] if isinstance(entry, list) and entry[:2] == key else file_digest(path)
        self._seen[path] = key + [value]
        return value

    def save(self):
        """Keep exactly the digests of this check; failing to save only costs the next start."""
        if self._seen == self._known:
            return
        temporary = self.path + ".tmp"
        try:
            with open(temporary, "w", encoding="utf-8") as stream:
                json.dump(self._seen, stream)
            os.replace(temporary, self.path)
        except OSError as error:
            logging.info("[feather_native] digest cache not saved: %s", error)


def _load(binary):
    loader = ExtensionFileLoader("feather_native", binary)
    spec = importlib.util.spec_from_file_location("feather_native", binary, loader=loader)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def enable(binary=BINARY, manifest=MANIFEST, ui_root=UI_ROOT, disable_flag=DISABLE_FLAG, digest_cache=DIGEST_CACHE):
    """Install the accelerator once; return the installation, or None for the Python UI."""
    global _attempted, _installation
    if _attempted:
        return _installation
    _attempted = True
    started = time.monotonic()
    if os.path.exists(disable_flag):
        logging.info("[feather_native] disabled by %s", disable_flag)
        return None
    if not os.path.exists(binary) or not os.path.exists(manifest):
        logging.info("[feather_native] not installed; using the Python UI")
        return None
    try:
        with open(manifest, "r", encoding="utf-8") as stream:
            expected = json.load(stream)
        digests = DigestCache(digest_cache)
        binary_sha256, framework_sha256 = digests.digest(binary), framework_digest(ui_root, digests.digest)
        digests.save()
        if binary_sha256 != expected["binary_sha256"]:
            raise ValueError("binary does not match its manifest")
        if framework_sha256 != expected["framework_sha256"]:
            raise ValueError("framework sources differ from the validated version")
        module = _load(binary)
        if getattr(module, "ABI", None) != expected["abi"]:
            raise ValueError("binary ABI %r differs from manifest %r" % (getattr(module, "ABI", None), expected["abi"]))
        _installation = module.install()
    except Exception as error:  # Any failure keeps the reference UI.
        logging.warning("[feather_native] disabled: %s", error)
        return None
    logging.info("[feather_native] enabled in %.0f ms", (time.monotonic() - started) * 1000)
    return _installation
