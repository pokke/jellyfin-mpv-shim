"""The fork build identity: a version suffix stamped at build time.

A fork build carries ``+pokke.<n>`` after the upstream version (see
``jellyfin_mpv_shim/fork.py``). These pin the three ways that could go wrong
quietly: the suffix leaking into a source checkout, the stamp script writing
a version that does not belong to the tree it ran in, and the update check
still pointing a fork build at upstream.
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
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTANTS = os.path.join(ROOT, "jellyfin_mpv_shim", "constants.py")
STAMP = os.path.join(ROOT, "tools", "stamp_fork_build.py")


def _base_version():
    with open(CONSTANTS, encoding="utf-8") as handle:
        return re.search(r'^CLIENT_VERSION = "([^"]+)"', handle.read(),
                         re.M).group(1)


class DisplayVersionTest(unittest.TestCase):
    def test_the_suffix_is_appended(self):
        from jellyfin_mpv_shim import fork

        with mock.patch.object(fork, "SUFFIX", "+pokke.5"):
            self.assertEqual(fork.display_version("3.3.0"), "3.3.0+pokke.5")

    def test_a_source_checkout_is_not_a_fork_build(self):
        """No stamp file in the tree, so the identity is upstream's."""
        from jellyfin_mpv_shim import fork

        self.assertEqual(fork.SUFFIX, "")
        self.assertFalse(fork.IS_FORK)


class ConstantsTest(unittest.TestCase):
    def test_client_version_is_assigned_once_at_the_left_margin(self):
        """gen_pkg.sh and artifacts.sh grep for it; setuptools reads it
        statically. A second line would give both of them two answers."""
        with open(CONSTANTS, encoding="utf-8") as handle:
            lines = [ln for ln in handle if ln.startswith("CLIENT_VERSION")]
        self.assertEqual(len(lines), 1)

    def test_the_first_top_level_assignment_is_the_plain_literal(self):
        with open(CONSTANTS, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        first = [n for n in tree.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "CLIENT_VERSION"
                         for t in n.targets)][0]
        self.assertEqual(ast.literal_eval(first.value), _base_version())

    def _constants_with_suffix(self, suffix):
        code = (
            "import sys, types\n"
            "m = types.ModuleType('jellyfin_mpv_shim._fork_build')\n"
            "m.SUFFIX = %r\n"
            "sys.modules['jellyfin_mpv_shim._fork_build'] = m\n"
            "from jellyfin_mpv_shim import constants\n"
            "print(constants.CLIENT_VERSION)\n"
            "print(constants.USER_AGENT)\n" % suffix)
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                             capture_output=True, text=True, check=True)
        return out.stdout.split()

    def test_a_stamped_build_carries_the_suffix_everywhere(self):
        base = _base_version()
        version, agent = self._constants_with_suffix("+pokke.5")
        self.assertEqual(version, base + "+pokke.5")
        self.assertEqual(agent, "Jellyfin-MPV-Shim/%s+pokke.5" % base)

    def test_an_unstamped_build_is_untouched(self):
        base = _base_version()
        version, agent = self._constants_with_suffix("")
        self.assertEqual(version, base)
        self.assertEqual(agent, "Jellyfin-MPV-Shim/%s" % base)


class SuffixedVersionParsesTest(unittest.TestCase):
    def test_the_update_checker_can_still_order_a_fork_version(self):
        from jellyfin_mpv_shim import version

        self.assertIsNotNone(version.parse("3.3.0+pokke.5"))
        self.assertIsNotNone(version.parse("3.3.0pre1+pokke.5"))


class StampScriptTest(unittest.TestCase):
    def _run(self, value):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "_fork_build.py")
            env = dict(os.environ)
            env.pop("JMS_FORK_BUILD", None)
            if value is not None:
                env["JMS_FORK_BUILD"] = value
            done = subprocess.run(
                [sys.executable, STAMP, "--out", out], cwd=ROOT, env=env,
                capture_output=True, text=True)
            text = None
            if os.path.exists(out):
                with open(out, encoding="utf-8") as handle:
                    text = handle.read()
            return done.returncode, text

    def test_a_bare_number_is_a_build_number(self):
        code, text = self._run("7")
        self.assertEqual(code, 0)
        self.assertIn('SUFFIX = "+pokke.7"', text)

    def test_a_matching_tag_is_mapped(self):
        code, text = self._run("v%s-pokke.12" % _base_version())
        self.assertEqual(code, 0)
        self.assertIn('SUFFIX = "+pokke.12"', text)

    def test_a_tag_for_another_version_is_refused(self):
        code, text = self._run("v9.9.9-pokke.1")
        self.assertEqual(code, 1)
        self.assertIsNone(text, "a refused stamp still wrote a file")

    def test_something_that_is_neither_is_refused(self):
        for value in ("latest", "v%s" % _base_version(), "1.2"):
            code, text = self._run(value)
            self.assertEqual(code, 1, value)
            self.assertIsNone(text, value)

    def test_the_written_file_is_importable_python(self):
        _code, text = self._run("3")
        ns = {}
        exec(compile(text, "_fork_build.py", "exec"), ns)
        self.assertEqual(ns["SUFFIX"], "+pokke.3")


class UpdateCheckDisabledTest(unittest.TestCase):
    def test_a_fork_build_makes_no_network_call(self):
        """The notice points at upstream's releases, and installing what it
        offers would overwrite the fork."""
        import jellyfin_mpv_shim.update_check as uc

        chk = uc.UpdateChecker(mock.Mock())
        with mock.patch.object(uc.fork, "IS_FORK", True), \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc, "requests") as rq:
            chk.check()
        rq.get.assert_not_called()
        self.assertIsNone(chk.last_check)
        self.assertIsNone(chk.new_version)


if __name__ == "__main__":
    unittest.main()
