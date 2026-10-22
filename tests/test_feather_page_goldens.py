## Declarative pages keep sending the recorded commands, input and geometry
##
## Copyright (C) 2026, Alexander K <https://github.com/drA1ex>
##
## This file may be distributed under the terms of the GNU GPLv3 license

import difflib
import os
import pathlib
import unittest

from tests import feather_page_goldens

GOLDENS = pathlib.Path(__file__).resolve().parent / "fixtures" / "page_goldens"
# Set to regenerate after a deliberate output change; review the diff.
UPDATE = os.environ.get("FEATHER_UPDATE_GOLDENS") == "1"


class PageGoldenTest(unittest.TestCase):
    def test_pages_match_recorded_output(self):
        scenarios = feather_page_goldens.scenarios()
        if UPDATE:
            GOLDENS.mkdir(parents=True, exist_ok=True)
            for path in GOLDENS.glob("*.txt"):
                if path.stem not in scenarios:
                    path.unlink()
        for name, record in scenarios.items():
            with self.subTest(scenario=name):
                actual = "\n".join(record()) + "\n"
                path = GOLDENS / (name + ".txt")
                if UPDATE:
                    path.write_text(actual)
                    continue
                expected = path.read_text()
                if actual != expected:
                    diff = difflib.unified_diff(
                        expected.splitlines(), actual.splitlines(),
                        "golden", "actual", lineterm="", n=2)
                    self.fail("%s differs from its golden:\n%s" % (
                        name, "\n".join(list(diff)[:60])))
        self.assertEqual(
            sorted(path.stem for path in GOLDENS.glob("*.txt")), sorted(scenarios))


if __name__ == "__main__":
    unittest.main()
