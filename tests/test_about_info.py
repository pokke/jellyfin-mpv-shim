"""Settings -> Logs says which build, and where its logs are.

A bug report arrives as a log and nothing else; the version and the two log
paths are what make it answerable.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import constants  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway  # noqa: E402
from jellyfin_mpv_shim.restart import PREVIOUS_LOG  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeConfig, FakeController, FakeSource, _SyncPool, build_scene)


def _texts(nodes):
    return [n.get("text") for n in nodes if isinstance(n, dict)
            and n.get("text") is not None]


class AboutInfoTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jms-about-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.gw = PlayerGateway.__new__(PlayerGateway)

    def _info(self):
        with mock.patch("jellyfin_mpv_shim.conffile.get",
                        side_effect=lambda app, name: os.path.join(
                            self.dir, name)):
            return self.gw.about_info()

    def test_it_reports_version_and_paths(self):
        info = self._info()
        self.assertEqual(info["version"], constants.CLIENT_VERSION)
        self.assertEqual(info["config_dir"], self.dir)
        self.assertEqual(info["log"], os.path.join(self.dir, "log.txt"))
        self.assertEqual(info["previous_log"],
                         os.path.join(self.dir, PREVIOUS_LOG))

    def test_it_says_whether_the_previous_log_exists(self):
        self.assertFalse(self._info()["previous_log_exists"])
        with open(os.path.join(self.dir, PREVIOUS_LOG), "w") as fh:
            fh.write("x")
        self.assertTrue(self._info()["previous_log_exists"])

    def test_it_never_raises(self):
        with mock.patch("jellyfin_mpv_shim.conffile.get",
                        side_effect=RuntimeError("boom")):
            info = self.gw.about_info()
        self.assertEqual(info["version"], constants.CLIENT_VERSION)


class LogsTabAboutTest(unittest.TestCase):

    def _browser(self, lines, info):
        ctl = FakeController()
        ctl.recent_logs = lambda: list(lines)
        ctl.about_info = lambda: info
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=FakeConfig())
        b._pool = _SyncPool()
        b.nav_stack = [{"kind": "settings", "server": "srv1", "_tab": "logs"}]
        return b

    def _all_text(self, b):
        nodes, _h = build_scene(b)
        return "\n".join(str(n.get("text")) for n in nodes
                         if isinstance(n, dict) and n.get("text"))

    INFO = {"version": "9.8.7", "config_dir": "/c", "log": "/c/log.txt",
            "previous_log": "/c/log.prev.txt", "previous_log_exists": True}

    def test_populated_log_shows_version_and_paths(self):
        text = self._all_text(self._browser(["a line"], self.INFO))
        self.assertIn("Version 9.8.7", text)
        self.assertIn("/c/log.txt", text)
        self.assertIn("/c/log.prev.txt", text)

    def test_empty_log_shows_them_too(self):
        text = self._all_text(self._browser([], self.INFO))
        self.assertIn("Version 9.8.7", text)
        self.assertIn("/c/log.txt", text)

    def test_previous_log_line_only_when_it_exists(self):
        info = dict(self.INFO, previous_log_exists=False)
        text = self._all_text(self._browser(["a line"], info))
        self.assertNotIn("log.prev.txt", text)
        self.assertIn("/c/log.txt", text)


if __name__ == "__main__":
    unittest.main()
