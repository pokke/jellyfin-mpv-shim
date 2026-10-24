"""A launch keeps the previous run's log instead of truncating it.

`configure_log_file` opens ``log.txt`` with mode="w", so without rotation
every launch destroys the one log that explains why the last one died. The
restart path already moves it aside (restart._preserve_log); this is the
same thing for an ordinary launch.

The second half is about *where* in `main` that happens: a second launch
that loses the single-instance race used to configure the log file first,
truncating the live instance's log on its way to exiting.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import inspect
import logging
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import log_utils  # noqa: E402
from jellyfin_mpv_shim.restart import PREVIOUS_LOG  # noqa: E402


class LogRotationTest(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="jms-logrot-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.log = os.path.join(self.dir, "log.txt")
        self.prev = os.path.join(self.dir, PREVIOUS_LOG)

    def _read(self, path):
        with open(path, encoding="utf-8") as fh:
            return fh.read()

    def _run(self, text):
        """One launch: configure, write a line, then end the run (close the
        handler -- Windows will not rename an open file)."""
        before = set(log_utils.root_logger.handlers)
        log_utils.configure_log_file(self.log)
        new = [h for h in log_utils.root_logger.handlers if h not in before]
        record = logging.LogRecord("t", logging.INFO, "f.py", 1, text, (),
                                   None)
        for h in new:
            h.handle(record)
            log_utils.root_logger.removeHandler(h)
            h.close()

    def test_the_previous_run_is_kept(self):
        self._run("first run")
        self._run("second run")
        self.assertIn("first run", self._read(self.prev))
        self.assertIn("second run", self._read(self.log))
        self.assertNotIn("first run", self._read(self.log))

    def test_an_empty_log_does_not_clobber_the_previous_one(self):
        with open(self.prev, "w", encoding="utf-8") as fh:
            fh.write("run one\n")
        open(self.log, "w").close()
        self._run("run three")
        self.assertIn("run one", self._read(self.prev))

    def test_it_keeps_only_one_generation(self):
        for n in range(3):
            self._run("run %d" % n)
        self.assertIn("run 1", self._read(self.prev))
        self.assertNotIn("run 0", self._read(self.prev))

    def test_a_failed_rename_does_not_stop_logging(self):
        self._run("first run")
        with mock.patch("os.replace", side_effect=OSError("locked")):
            self._run("second run")
        self.assertIn("second run", self._read(self.log))


class StartupOrderTest(unittest.TestCase):
    """The log file is configured only once we know we are the instance."""

    def _main_calls(self):
        from jellyfin_mpv_shim import mpv_shim

        tree = ast.parse(inspect.getsource(mpv_shim))
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        calls = {}
        for node in ast.walk(main):
            if isinstance(node, ast.Call):
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else getattr(
                    f, "id", None)
                calls.setdefault(name, []).append(node.lineno)
        return calls

    def test_the_log_file_is_opened_after_the_instance_lock(self):
        calls = self._main_calls()
        self.assertIn("acquire", calls)
        self.assertIn("configure_log_file", calls)
        self.assertGreater(
            min(calls["configure_log_file"]), max(calls["acquire"]),
            "a second launch truncates the running instance's log.txt "
            "before it finds out it is the second launch")

    def test_the_version_is_logged_after_the_file_is_configured(self):
        calls = self._main_calls()
        # `log.info(...)` calls are all named "info"; the version line is
        # the one whose arguments name CLIENT_VERSION.
        from jellyfin_mpv_shim import mpv_shim

        tree = ast.parse(inspect.getsource(mpv_shim))
        main = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef) and n.name == "main")
        lines = [n.lineno for n in ast.walk(main)
                 if isinstance(n, ast.Call)
                 and any(isinstance(a, ast.Name) and a.id == "CLIENT_VERSION"
                         for a in n.args)]
        self.assertTrue(lines, "main never logs the version")
        self.assertGreater(min(lines), min(calls["configure_log_file"]))


if __name__ == "__main__":
    unittest.main()
