"""The fork's in-app updater: what it offers, what it will download, what it
will run.

This code downloads an installer and runs it elevated, so most of what is
pinned here is a refusal: a dev build, a draft, a redirect to somewhere else,
a file that is not the size and digest the API promised. No test touches the
network; ``requests.get`` is replaced wherever a request could be made.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import hashlib
import os
import re
import shutil
import threading
import unittest
from unittest import mock

from jellyfin_mpv_shim import fork_update as fu
from jellyfin_mpv_shim import restart

REPO = "https://github.com/pokke/jellyfin-mpv-shim"
DL = REPO + "/releases/download/"
PAYLOAD = b"MZ-not-really-an-installer" * 100
DIGEST = hashlib.sha256(PAYLOAD).hexdigest()


def asset(n, name=None, digest="sha256:" + DIGEST, size=len(PAYLOAD),
          url=None, version="3.3.0"):
    name = name or "jellyfin-mpv-shim_%s-pokke.%d_installer.exe" % (version, n)
    out = {"name": name, "size": size,
           "browser_download_url": url or
           "%s/releases/download/v%s-pokke.%d/%s" % (REPO, version, n, name)}
    if digest is not None:
        out["digest"] = digest
    return out


def release(n, assets=None, tag=None, draft=False, prerelease=False,
            html_url=None):
    return {"tag_name": tag or "v3.3.0-pokke.%d" % n,
            "draft": draft, "prerelease": prerelease,
            "html_url": html_url or "%s/releases/tag/v3.3.0-pokke.%d"
                                    % (REPO, n),
            "assets": [asset(n)] if assets is None else assets}


class FindUpdateTest(unittest.TestCase):
    def test_the_highest_newer_number_wins_in_any_order(self):
        offer = fu.find_update(2, [release(3), release(5), release(4)])
        self.assertEqual(offer.n, 5)
        self.assertEqual(offer.version, "3.3.0-pokke.5")

    def test_the_same_or_an_older_number_offers_nothing(self):
        self.assertIsNone(fu.find_update(5, [release(5), release(4)]))
        self.assertIsNone(fu.find_update(9, [release(5)]))

    def test_drafts_and_prereleases_are_skipped(self):
        rels = [release(9, draft=True), release(8, prerelease=True),
                release(3)]
        self.assertEqual(fu.find_update(2, rels).n, 3)

    def test_a_tag_that_is_not_ours_is_skipped(self):
        rels = [release(9, tag="v3.3.0"), release(8, tag="latest"),
                release(7, tag="v3.3.0-pokke.dev7"), release(3)]
        self.assertEqual(fu.find_update(2, rels).n, 3)

    def test_a_prerelease_style_client_version_is_accepted(self):
        rel = release(4, tag="v3.4.0pre1-pokke.4",
                      assets=[asset(4, version="3.4.0pre1")])
        self.assertEqual(fu.find_update(2, [rel]).n, 4)

    def test_only_the_plain_x64_installer_is_chosen(self):
        names = ["jellyfin-mpv-shim_3.3.0-pokke.4_LEGACY64_installer.exe",
                 "jellyfin-mpv-shim_3.3.0-pokke.4_ARM64_installer.exe",
                 "jellyfin-mpv-shim_3.3.0-pokke.4_installer.exe"]
        rel = release(4, assets=[asset(4, name=n) for n in names])
        self.assertEqual(fu.find_update(2, [rel]).name, names[2])

    def test_a_release_with_only_other_installers_offers_nothing(self):
        rel = release(4, assets=[asset(
            4, name="jellyfin-mpv-shim_3.3.0-pokke.4_ARM64_installer.exe")])
        self.assertIsNone(fu.find_update(2, [rel]))

    def test_an_asset_numbered_for_another_release_is_refused(self):
        rel = release(4, assets=[asset(3)])
        self.assertIsNone(fu.find_update(2, [rel]))

    def test_a_missing_or_garbled_digest_is_refused(self):
        for digest in (None, "", "sha256:abc", "md5:" + DIGEST,
                       "sha256:" + "z" * 64, DIGEST):
            rel = release(4, assets=[asset(4, digest=digest)])
            self.assertIsNone(fu.find_update(2, [rel]), digest)

    def test_a_missing_size_is_refused(self):
        for size in (None, 0, -1, "12"):
            rel = release(4, assets=[asset(4, size=size)])
            self.assertIsNone(fu.find_update(2, [rel]), size)

    def test_a_download_url_off_the_repo_is_refused(self):
        for url in ("https://evil.example/x.exe",
                    "http://github.com/pokke/jellyfin-mpv-shim/releases/"
                    "download/a/b.exe",
                    "https://github.com/someone/else/releases/download/a/b"):
            rel = release(4, assets=[asset(4, url=url)])
            self.assertIsNone(fu.find_update(2, [rel]), url)

    def test_a_broken_release_does_not_hide_a_good_one(self):
        rels = [release(9, assets=[asset(9, digest=None)]), release(4)]
        self.assertEqual(fu.find_update(2, rels).n, 4)

    def test_an_html_url_is_only_used_inside_the_repo(self):
        ok = fu.find_update(2, [release(4)])
        self.assertTrue(ok.html_url.startswith(REPO + "/"))
        bad = fu.find_update(
            2, [release(4, html_url="https://evil.example/pokke/")])
        self.assertTrue(bad.html_url.startswith(REPO + "/"))
        self.assertNotIn("evil", bad.html_url)

    def test_garbage_in_the_listing_is_not_an_exception(self):
        self.assertIsNone(fu.find_update(2, [None, 7, {}, {"tag_name": 3},
                                             {"tag_name": "v1-pokke.9",
                                              "assets": "x"}]))
        self.assertIsNone(fu.find_update(2, None))


class BuildNumberTest(unittest.TestCase):
    def _with(self, suffix, **extra):
        return mock.patch.multiple(fu.fork, SUFFIX=suffix,
                                   IS_FORK=bool(suffix), **extra)

    def test_a_release_suffix_parses(self):
        with self._with("+pokke.12", create=True):
            if hasattr(fu.fork, "RELEASE_BUILD"):
                with mock.patch.object(fu.fork, "RELEASE_BUILD", 12):
                    self.assertEqual(fu.running_build(), 12)
            else:
                self.assertEqual(fu.running_build(), 12)

    def test_a_dev_or_missing_suffix_is_not_a_release(self):
        for suffix in ("", "+pokke.dev12", "+pokke.", "+other.3"):
            with self._with(suffix, create=True):
                if hasattr(fu.fork, "RELEASE_BUILD"):
                    with mock.patch.object(fu.fork, "RELEASE_BUILD", None):
                        self.assertIsNone(fu.running_build(), suffix)
                else:
                    self.assertIsNone(fu.running_build(), suffix)


class _Player:
    def __init__(self, ui=True):
        self.shown = []
        self.ui_calls = []
        if ui:
            self.notify_update = (
                lambda *a, **k: self.ui_calls.append((a, k)))

    def show_text(self, *a):
        self.shown.append(a)


class _Checker:
    def __init__(self, ui=True):
        self.playerManager = _Player(ui)
        self.has_notified = False
        self.new_version = None
        self.release_url = "https://github.com/jellyfin/jellyfin-mpv-shim/releases/"

    def notify(self):
        self.playerManager.show_text(self.new_version, 5000, 1)


class _Settings:
    check_updates = True
    update_skip_version = None
    wanted = True

    def notify_updates_wanted(self):
        return self.wanted


class CheckTest(unittest.TestCase):
    """Gates, and that ``check`` never blocks its caller."""

    def setUp(self):
        fu._reset_for_tests()
        self.settings = _Settings()
        self.threads = []
        patches = [
            mock.patch.object(fu, "settings", self.settings),
            mock.patch.object(fu.fork, "IS_FORK", True),
            mock.patch.object(fu, "running_build", lambda: 2),
            mock.patch.object(fu, "_platform_ok", lambda: True),
            mock.patch.object(fu, "_command_is_plain", lambda: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.fetch = mock.Mock(return_value=[release(4)])
        p = mock.patch.object(fu, "fetch_releases", self.fetch)
        p.start()
        self.addCleanup(p.stop)

    def _run(self, checker):
        fu.check(checker)
        t = fu._thread
        if t is not None:
            t.join(5)
            self.assertFalse(t.is_alive())

    def test_a_newer_release_is_announced_with_an_install_callable(self):
        c = _Checker()
        self._run(c)
        self.assertEqual(c.new_version, "3.3.0-pokke.4")
        (args, kwargs), = c.playerManager.ui_calls
        self.assertEqual(args[0], "3.3.0-pokke.4")
        self.assertTrue(args[1].startswith(REPO + "/"))
        self.assertTrue(callable(kwargs["install"]))

    def test_the_menu_and_notice_point_at_the_fork_never_upstream(self):
        c = _Checker()
        self._run(c)
        self.assertTrue(c.release_url.startswith(REPO + "/"))
        self.assertNotIn("jellyfin/jellyfin-mpv-shim", c.release_url)
        (args, _kw), = c.playerManager.ui_calls
        self.assertNotIn("jellyfin/jellyfin-mpv-shim", args[1])

    def test_a_dev_or_unstamped_build_never_looks(self):
        with mock.patch.object(fu, "running_build", lambda: None):
            self._run(_Checker())
        self.fetch.assert_not_called()
        self.assertIsNone(fu._thread)

    def test_not_a_fork_never_looks(self):
        with mock.patch.object(fu.fork, "IS_FORK", False):
            self._run(_Checker())
        self.fetch.assert_not_called()

    def test_the_check_updates_setting_is_honoured(self):
        self.settings.check_updates = False
        self._run(_Checker())
        self.fetch.assert_not_called()

    def test_the_notify_setting_is_honoured(self):
        self.settings.wanted = False
        c = _Checker()
        self._run(c)
        self.fetch.assert_not_called()
        self.assertEqual(c.playerManager.ui_calls, [])

    def test_a_skipped_version_is_not_announced(self):
        self.settings.update_skip_version = "3.3.0-pokke.4"
        c = _Checker()
        self._run(c)
        self.assertEqual(c.playerManager.ui_calls, [])

    def test_up_to_date_says_nothing(self):
        self.fetch.return_value = [release(2)]
        c = _Checker()
        self._run(c)
        self.assertIsNone(c.new_version)
        self.assertEqual(c.playerManager.ui_calls, [])

    def test_a_failed_lookup_is_silent(self):
        self.fetch.side_effect = OSError("offline")
        c = _Checker()
        self._run(c)
        self.assertIsNone(c.new_version)

    def test_check_returns_while_the_fetch_is_still_blocked(self):
        """It is called from ``_play_media`` under the player lock."""
        gate, entered = threading.Event(), threading.Event()

        def slow():
            entered.set()
            gate.wait(10)
            return [release(4)]

        self.fetch.side_effect = slow
        c = _Checker()
        try:
            fu.check(c)
            self.assertTrue(entered.wait(5))
            self.assertIsNone(c.new_version)
            fu.check(c)                      # a second call: still one thread
        finally:
            gate.set()
        fu._thread.join(5)
        self.assertEqual(self.fetch.call_count, 1)
        self.assertEqual(c.new_version, "3.3.0-pokke.4")

    def test_a_second_check_does_not_look_again(self):
        c = _Checker()
        self._run(c)
        self._run(c)
        self.assertEqual(self.fetch.call_count, 1)

    def test_flags_on_the_relaunch_command_offer_only_the_link(self):
        """A relaunch from the installer cannot carry ``--config``."""
        c = _Checker()
        with mock.patch.object(fu, "_command_is_plain", lambda: False):
            self._run(c)
        (args, kwargs), = c.playerManager.ui_calls
        self.assertEqual(kwargs, {})
        self.assertEqual(len(args), 2)

    def test_the_cli_falls_back_to_the_osd(self):
        c = _Checker(ui=False)
        self._run(c)
        self.assertEqual(len(c.playerManager.shown), 1)


class PlatformGateTest(unittest.TestCase):
    def _ok(self, name="nt", frozen=True, machine="AMD64", avx2=True):
        with mock.patch.object(fu.os, "name", name), \
                mock.patch.object(fu.sys, "frozen", frozen, create=True), \
                mock.patch.object(fu.platform, "machine", lambda: machine), \
                mock.patch.object(fu, "_has_avx2", lambda: avx2):
            return fu._platform_ok()

    def test_windows_frozen_x64_with_avx2_is_ok(self):
        self.assertTrue(self._ok())

    def test_each_missing_requirement_refuses(self):
        self.assertFalse(self._ok(name="posix"))
        self.assertFalse(self._ok(frozen=False))
        self.assertFalse(self._ok(machine="ARM64"))
        self.assertFalse(self._ok(machine="x86"))
        self.assertFalse(self._ok(avx2=False))


# -- download ----------------------------------------------------------------

class _Resp:
    def __init__(self, status=200, body=b"", location=None):
        self.status_code = status
        self.headers = {"location": location} if location else {}
        self._body = body
        self.closed = False

    def iter_content(self, n):
        for i in range(0, len(self._body), n):
            yield self._body[i:i + n]

    def close(self):
        self.closed = True


def _plain_open(path):
    return open(path, "rb")


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.join(os.environ.get("TEMP", "/tmp"),
                                "fu-test-%d" % os.getpid())
        os.makedirs(self.tmp, exist_ok=True)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        patches = [
            mock.patch.object(fu, "_open_locked", _plain_open),
            mock.patch.object(fu, "_temp_base", lambda: self.tmp),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.offer = fu.find_update(2, [release(4)])
        self.cleanups = []

    def _get(self, script):
        """``requests.get`` replacement serving ``script`` in order."""
        calls = []

        def get(url, **kw):
            calls.append((url, kw))
            return script.pop(0) if script else _Resp(500)

        self.calls = calls
        return mock.patch.object(fu.requests, "get", get)

    def _download(self, script):
        with self._get(script):
            return fu.download(self.offer)

    def _leftovers(self):
        return os.listdir(self.tmp)

    def test_a_direct_download_verifies_and_stages(self):
        staged = self._download([_Resp(200, PAYLOAD)])
        try:
            self.assertFalse(staged.closed)
            with open(staged.path, "rb") as f:
                self.assertEqual(f.read(), PAYLOAD)
            self.assertTrue(staged.path.startswith(self.tmp))
            self.assertEqual(os.path.basename(staged.path), self.offer.name)
        finally:
            staged.close()

    def test_every_request_is_plain_verified_and_unauthenticated(self):
        script = [_Resp(302, location="https://objects.githubusercontent.com/x"),
                  _Resp(200, PAYLOAD)]
        self._download(script).close()
        for _url, kw in self.calls:
            self.assertIs(kw.get("allow_redirects"), False)
            self.assertTrue(kw.get("verify", True))
            headers = {k.lower() for k in kw.get("headers", {})}
            self.assertFalse(headers & {"authorization", "x-emby-token",
                                        "x-mediabrowser-token", "cookie"})

    def test_the_first_url_is_the_one_the_api_named(self):
        self._download([_Resp(200, PAYLOAD)]).close()
        self.assertEqual(self.calls[0][0], self.offer.url)

    def test_redirects_inside_the_allowlist_are_followed(self):
        for host in ("github.com", "objects.githubusercontent.com",
                     "release-assets.githubusercontent.com"):
            script = [_Resp(302, location="https://%s/a/b?sig=1" % host),
                      _Resp(200, PAYLOAD)]
            self._download(script).close()

    def _refused(self, location, hops=None):
        script = hops or [_Resp(302, location=location),
                          _Resp(200, PAYLOAD)]
        with self.assertRaises(fu.UpdateError):
            self._download(script)
        self.assertEqual(self._leftovers(), [], "left a file behind")
        return self.calls

    def test_a_redirect_to_another_host_is_refused_before_it_is_asked(self):
        calls = self._refused("https://evil.example/x.exe")
        self.assertEqual(len(calls), 1)

    def test_userinfo_hiding_the_real_host_is_refused(self):
        self._refused("https://github.com@evil.example/x.exe")
        self._refused("https://objects.githubusercontent.com:pw@evil.example/")

    def test_plain_http_is_refused(self):
        self._refused("http://objects.githubusercontent.com/x")

    def test_a_non_443_port_is_refused(self):
        self._refused("https://objects.githubusercontent.com:8443/x")
        self._refused("https://objects.githubusercontent.com:bad/x")

    def test_a_lookalike_host_is_refused(self):
        self._refused("https://github.com.evil.example/x")
        self._refused("https://api.github.com/x")
        self._refused("https://evilgithub.com/x")

    def test_more_than_four_hops_is_refused(self):
        hop = "https://objects.githubusercontent.com/next"
        script = [_Resp(302, location=hop) for _ in range(6)]
        self._refused(None, hops=script)

    def test_four_hops_are_allowed(self):
        hop = "https://objects.githubusercontent.com/next"
        script = [_Resp(302, location=hop) for _ in range(4)] + [
            _Resp(200, PAYLOAD)]
        self._download(script).close()

    def test_a_redirect_without_a_location_is_refused(self):
        self._refused(None, hops=[_Resp(302)])

    def test_a_first_url_off_the_release_path_is_refused(self):
        offer = self.offer._replace(url="https://github.com/pokke/other/x.exe")
        with self._get([_Resp(200, PAYLOAD)]):
            with self.assertRaises(fu.UpdateError):
                fu.download(offer)
        self.assertEqual(self.calls, [])

    def test_an_http_error_is_a_failure_not_a_file(self):
        self._refused(None, hops=[_Resp(404, b"nope")])

    def test_a_short_download_is_deleted(self):
        self._refused(None, hops=[_Resp(200, PAYLOAD[:-1])])

    def test_a_long_download_is_deleted_without_reading_it_all(self):
        self._refused(None, hops=[_Resp(200, PAYLOAD + b"x" * 5000)])

    def test_the_wrong_bytes_are_deleted(self):
        wrong = b"X" * len(PAYLOAD)
        self._refused(None, hops=[_Resp(200, wrong)])

    def test_progress_reports_whole_percent_steps(self):
        seen = []
        with self._get([_Resp(200, PAYLOAD)]):
            staged = fu.download(self.offer, progress=seen.append)
        staged.close()
        self.assertEqual(seen[-1], 100)
        self.assertEqual(seen, sorted(seen))
        self.assertEqual(len(seen), len(set(seen)))

    def test_it_never_downloads_into_the_config_dir(self):
        from jellyfin_mpv_shim import conffile
        from jellyfin_mpv_shim.constants import APP_NAME
        conf = os.path.dirname(conffile.get(APP_NAME, "conf.json"))
        staged = self._download([_Resp(200, PAYLOAD)])
        try:
            self.assertFalse(
                os.path.abspath(staged.path).startswith(
                    os.path.abspath(conf) + os.sep))
        finally:
            staged.close()

    def test_each_download_gets_a_fresh_directory(self):
        a = self._download([_Resp(200, PAYLOAD)])
        b = self._download([_Resp(200, PAYLOAD)])
        try:
            self.assertNotEqual(os.path.dirname(a.path),
                                os.path.dirname(b.path))
        finally:
            a.close()
            b.close()


@unittest.skipUnless(os.name == "nt", "the share-mode lock is a Windows API")
class HeldHandleTest(unittest.TestCase):
    def test_the_held_handle_blocks_a_write_and_a_swap(self):
        tmp = os.path.join(os.environ["TEMP"], "fu-handle-%d" % os.getpid())
        os.makedirs(tmp, exist_ok=True)
        self.addCleanup(shutil.rmtree, tmp, True)
        path = os.path.join(tmp, "setup.exe")
        with open(path, "wb") as f:
            f.write(PAYLOAD)
        h = fu._open_locked(path)
        try:
            self.assertEqual(h.read(10**6), PAYLOAD)
            with self.assertRaises(OSError):
                open(path, "r+b")
            with self.assertRaises(OSError):
                open(path, "ab")
            other = os.path.join(tmp, "other.exe")
            with open(other, "wb") as f:
                f.write(b"evil")
            with self.assertRaises(OSError):
                os.replace(other, path)
            with self.assertRaises(OSError):
                os.remove(path)
            # Reading stays possible: the installer must be able to start.
            with open(path, "rb") as f:
                self.assertEqual(f.read(), PAYLOAD)
        finally:
            h.close()
        os.remove(path)                      # released once closed


# -- install -----------------------------------------------------------------

class _Staged:
    def __init__(self, path="C:/x/setup.exe"):
        self.path = path
        self.closed = False
        self.dir = None

    def close(self):
        self.closed = True


class InstallTest(unittest.TestCase):
    def setUp(self):
        fu._reset_for_tests()
        restart.cancel()
        self.addCleanup(restart.cancel)
        self.addCleanup(fu._reset_for_tests)
        self.staged = _Staged()
        fu._staged = self.staged
        self.quit_result = True
        self.quit_calls = 0
        ui = mock.Mock()

        def quit_app():
            self.quit_calls += 1
            return self.quit_result

        ui.quit_app = quit_app
        self.ui = ui
        p2 = mock.patch.object(fu, "_user_interface", lambda: ui)
        p2.start()
        self.addCleanup(p2.stop)

    def test_install_now_cancels_a_pending_restart_and_arms(self):
        restart.request()
        self.assertTrue(fu.install_now())
        self.assertFalse(restart.requested())
        self.assertTrue(fu.armed())
        self.assertEqual(self.quit_calls, 1)

    def test_install_now_disarms_when_the_quit_did_not_start(self):
        self.quit_result = False
        self.assertFalse(fu.install_now())
        self.assertFalse(fu.armed())

    def test_install_now_disarms_when_the_quit_raises(self):
        def boom():
            raise RuntimeError("x")
        self.ui.quit_app = boom
        self.assertFalse(fu.install_now())
        self.assertFalse(fu.armed())

    def test_install_now_with_nothing_staged_does_nothing(self):
        fu._staged = None
        self.assertFalse(fu.install_now())
        self.assertEqual(self.quit_calls, 0)

    def test_the_final_action_does_nothing_unless_armed(self):
        with mock.patch.object(fu.subprocess, "Popen") as popen:
            self.assertFalse(fu.run_installer_if_requested())
        popen.assert_not_called()

    def test_the_final_action_launches_exactly_the_constant_args(self):
        fu.install_now()
        with mock.patch.object(fu.subprocess, "Popen") as popen:
            self.assertTrue(fu.run_installer_if_requested())
        (cmd,), kw = popen.call_args
        self.assertEqual(cmd, ["C:/x/setup.exe", "/SILENT",
                               "/SUPPRESSMSGBOXES", "/NORESTART",
                               "/RELAUNCH=1"])
        self.assertFalse(any(a.upper().startswith("/LOG") for a in cmd))
        self.assertNotIn("shell", kw)
        self.assertTrue(kw["creationflags"] ==
                        getattr(fu.subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self.assertEqual(kw["env"], restart.child_env())

    def test_the_handle_is_held_across_popen_and_closed_after(self):
        fu.install_now()
        states = []
        with mock.patch.object(
                fu.subprocess, "Popen",
                side_effect=lambda *a, **k: states.append(
                    self.staged.closed)):
            fu.run_installer_if_requested()
        self.assertEqual(states, [False])
        self.assertTrue(self.staged.closed)

    def test_it_launches_only_once(self):
        fu.install_now()
        with mock.patch.object(fu.subprocess, "Popen") as popen:
            fu.run_installer_if_requested()
            fu.run_installer_if_requested()
        self.assertEqual(popen.call_count, 1)

    def test_a_failed_launch_brings_the_app_back(self):
        fu.install_now()
        with mock.patch.object(fu.subprocess, "Popen",
                               side_effect=OSError("denied")), \
                mock.patch.object(restart, "relaunch_if_requested") as back:
            self.assertFalse(fu.run_installer_if_requested())
        back.assert_called_once()
        self.assertTrue(self.staged.closed)

    def test_the_final_action_calls_it_after_the_relaunch(self):
        """The wiring in mpv_shim: a source-shape check, since the final
        action is a closure inside ``main``."""
        path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "jellyfin_mpv_shim", "mpv_shim.py")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        a = text.index("restart.relaunch_if_requested()")
        b = text.index("run_installer_if_requested()")
        self.assertLess(a, b)


class StartInstallTest(unittest.TestCase):
    """The worker the banner's button starts."""

    def setUp(self):
        fu._reset_for_tests()
        self.addCleanup(fu._reset_for_tests)
        self.offer = fu.find_update(2, [release(4)])

    def _run(self, download=None, install=True):
        done = threading.Event()
        events = []

        def failed(msg):
            events.append(("failed", msg))
            done.set()

        def progress(pct):
            events.append(("progress", pct))

        def fake_install_now():
            events.append(("install_now",))
            done.set()
            return install

        with mock.patch.object(fu, "download", download), \
                mock.patch.object(fu, "install_now", fake_install_now):
            self.assertTrue(fu.start_install(self.offer, progress, failed))
            self.assertTrue(done.wait(5))
            fu._install_thread.join(5)
        return events

    def test_a_good_download_ends_in_install_now(self):
        events = self._run(download=mock.Mock(return_value=_Staged()))
        self.assertEqual(events[-1], ("install_now",))
        self.assertIsNotNone(fu._staged)

    def test_a_failed_download_reports_a_reason_and_installs_nothing(self):
        events = self._run(download=mock.Mock(
            side_effect=fu.UpdateError("size mismatch")))
        self.assertEqual(events, [("failed", "size mismatch")])

    def test_an_unexpected_error_reports_without_a_url_or_path(self):
        events = self._run(download=mock.Mock(
            side_effect=OSError("https://x/?sig=SECRET")))
        (kind, msg), = events
        self.assertEqual(kind, "failed")
        self.assertNotIn("SECRET", msg)

    def test_a_quit_that_did_not_start_is_a_failure_and_frees_the_file(self):
        staged = _Staged()
        events = self._run(download=mock.Mock(return_value=staged),
                           install=False)
        self.assertEqual(events[-1][0], "failed")
        self.assertTrue(staged.closed)

    def test_a_second_start_while_busy_is_refused(self):
        gate = threading.Event()
        started = threading.Event()

        def slow(offer, progress=None):
            started.set()
            gate.wait(5)
            raise fu.UpdateError("late")

        with mock.patch.object(fu, "download", slow):
            self.assertTrue(fu.start_install(self.offer, lambda p: None,
                                             lambda m: None))
            self.assertTrue(started.wait(5))
            self.assertFalse(fu.start_install(self.offer, lambda p: None,
                                              lambda m: None))
            gate.set()
            fu._install_thread.join(5)


# -- the banner --------------------------------------------------------------

class BannerTest(unittest.TestCase):
    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import FakeController, FakeSource

        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.invalidated = 0
        real = self.b.invalidate

        def counting():
            self.invalidated += 1
            real()

        self.b.invalidate = counting
        self.started = []

    def _install(self, progress, failed):
        self.started.append((progress, failed))

    def _scene(self):
        from tests._shell_harness import build_scene
        return build_scene(self.b)

    def _said(self, nodes):
        return " ".join(str(n.get("text") or "") for n in nodes)

    def test_there_is_no_install_button_without_the_callable(self):
        from tests._shell_harness import ids
        self.b.notify_update("3.3.0-pokke.4", "http://x")
        nodes, _h = self._scene()
        self.assertNotIn("banner-install", ids(nodes))
        self.assertIn("banner-open", ids(nodes))

    def test_the_button_appears_with_the_callable_beside_the_others(self):
        from tests._shell_harness import ids
        self.b.notify_update("3.3.0-pokke.4", "http://x",
                             install=self._install)
        nodes, _h = self._scene()
        for i in ("banner-install", "banner-open", "banner-dismiss",
                  "banner-ignore"):
            self.assertIn(i, ids(nodes))

    def test_clicking_starts_the_install_and_repaints(self):
        self.b.notify_update("3.3.0-pokke.4", "http://x",
                             install=self._install)
        _n, handlers = self._scene()
        before = self.invalidated
        handlers["banner-install"]["click"]()
        self.assertEqual(len(self.started), 1)
        self.assertGreater(self.invalidated, before)

    def test_progress_is_shown_and_repaints_and_hides_the_buttons(self):
        from tests._shell_harness import ids
        self.b.notify_update("3.3.0-pokke.4", "http://x",
                             install=self._install)
        _n, handlers = self._scene()
        handlers["banner-install"]["click"]()
        progress, _failed = self.started[0]
        before = self.invalidated
        progress(42)
        self.assertGreater(self.invalidated, before)
        nodes, _h = self._scene()
        self.assertIn("42%", self._said(nodes))
        self.assertNotIn("banner-install", ids(nodes))

    def test_a_second_click_while_downloading_does_nothing(self):
        self.b.notify_update("3.3.0-pokke.4", "http://x",
                             install=self._install)
        _n, handlers = self._scene()
        handlers["banner-install"]["click"]()
        self.b._install_update()
        self.assertEqual(len(self.started), 1)

    def test_a_failure_says_so_and_gives_the_buttons_back(self):
        from tests._shell_harness import ids
        self.b.notify_update("3.3.0-pokke.4", "http://x",
                             install=self._install)
        _n, handlers = self._scene()
        handlers["banner-install"]["click"]()
        _progress, failed = self.started[0]
        before = self.invalidated
        failed("size mismatch")
        self.assertGreater(self.invalidated, before)
        nodes, _h = self._scene()
        self.assertIn("Update failed: size mismatch", self._said(nodes))
        self.assertIn("banner-install", ids(nodes))
        self.assertIn("banner-dismiss", ids(nodes))

    def test_the_tooltip_mentions_administrator_approval(self):
        self.b.notify_update("3.3.0-pokke.4", "http://x",
                             install=self._install)
        nodes, _h = self._scene()
        btn = [n for n in nodes if n.get("id") == "banner-install"]
        self.assertTrue(btn)
        blob = repr(btn[0]).lower()
        self.assertIn("administrator", blob)


class MenuAndNoticeUrlTest(unittest.TestCase):
    def test_update_check_delegates_a_fork_build_to_fork_update(self):
        import jellyfin_mpv_shim.update_check as uc

        chk = uc.UpdateChecker(mock.Mock())
        with mock.patch.object(uc.fork, "IS_FORK", True), \
                mock.patch.object(uc.fork_update, "check") as delegated, \
                mock.patch.object(uc, "requests") as rq:
            chk.check()
        delegated.assert_called_once_with(chk)
        rq.get.assert_not_called()


if __name__ == "__main__":
    unittest.main()
