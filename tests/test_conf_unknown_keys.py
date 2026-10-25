"""A key this build does not know is kept when the config is saved.

The schema is the class's annotations, and `save` used to write exactly
those -- so any key the running build had no field for was silently dropped
the first time it saved. Running a build without a setting (an older one,
or upstream's next to a fork that added one) therefore wiped that setting
for every build: back on the newer one it had reset to its default.

The exception is a key this project retired on purpose (RETIRED_KEYS): the
migrations rely on those leaving conf.json.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._tmpdirs import tmpdir  # noqa: E402


class UnknownKeysSurviveASaveTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(tmpdir(), "conf.json")

    def _write(self, data):
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def _read(self):
        with open(self.path, encoding="utf-8") as fh:
            return json.load(fh)

    def _load(self):
        from jellyfin_mpv_shim.conf import Settings

        s = Settings()
        self.assertTrue(s.load(self.path))
        return s

    def test_a_key_from_another_build_is_written_back(self):
        self._write({"auto_play": False, "a_newer_builds_key": 15})
        s = self._load()        # schema change -> saves back
        s.save()
        out = self._read()
        self.assertEqual(out.get("a_newer_builds_key"), 15)
        self.assertIs(out["auto_play"], False)

    def test_it_survives_every_save_unchanged(self):
        self._write({"a_newer_builds_key": {"nested": [1, 2]}})
        for _ in range(3):
            self._load().save()
            self.assertEqual(self._read().get("a_newer_builds_key"),
                             {"nested": [1, 2]})

    def test_it_never_becomes_an_attribute(self):
        self._write({"a_newer_builds_key": 15})
        s = self._load()
        self.assertFalse(hasattr(s, "a_newer_builds_key"))

    def test_a_retired_key_still_leaves(self):
        # Migration 4 moved these into input.conf; keeping them would
        # leave dead settings in the file forever.
        from jellyfin_mpv_shim.conf import RETIRED_KEYS

        self.assertIn("seek_up", RETIRED_KEYS)
        self._write({"seek_up": 30, "skip_intro_always": True})
        self._load().save()
        out = self._read()
        self.assertNotIn("seek_up", out)
        self.assertNotIn("skip_intro_always", out)

    def test_a_known_key_is_written_from_the_setting_not_the_file(self):
        self._write({"auto_play": False})
        s = self._load()
        s.auto_play = True
        s.save()
        self.assertIs(self._read()["auto_play"], True)


if __name__ == "__main__":
    unittest.main()
