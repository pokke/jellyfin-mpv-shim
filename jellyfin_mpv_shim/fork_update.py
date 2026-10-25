"""In-app update for this fork's Windows build.

A fork release build (``+pokke.<n>``, see ``fork.py``) asks GitHub whether a
newer release of **this fork** exists, and if so offers a one-click install:
download the installer, close the app, run the installer silently, and let it
start the app again.

**This downloads a program and runs it, probably elevated.** Every rule below
is there for that reason and none is a nicety:

* The *only* source is the pinned repository's release assets. The first URL
  must be under ``REPO_URL`` and every redirect must stay on a short host
  allowlist, over https on 443, with no userinfo -- ``https://github.com@evil``
  has the *host* ``evil``. No credential of any kind is sent anywhere here.
* The API's ``digest`` and ``size`` are required and checked; a missing or
  garbled one means no offer, not an unchecked download.
* **The file is checked and launched through one handle** (``_open_locked``):
  opened without write or delete sharing, hashed through that handle, and kept
  open until ``Popen`` has returned. So it cannot be swapped between the check
  and the launch. The directory is a fresh ``mkdtemp`` under
  ``%LOCALAPPDATA%``, never the config dir (which roams and, with
  ``--config``, can be anywhere), and the file has a constant name.

  **That is the whole guarantee, and it has an end.** Once ``Popen`` returns
  the handle is closed, and Inno's elevated self-relaunch opens the installer
  by path again; a process of the *same user* could swap the file in that
  gap. Code already running as the user is out of scope here (it could do far
  worse), and the real fix is Authenticode-signing the installer, which
  Windows would then verify at the elevation.
* The installer's arguments are constants. Nothing from the network, the
  environment or the config reaches the command line, and there is no
  ``/LOG`` (a path the installer would write to elevated).
* The launch is the process's last act (``run_installer_if_requested``, from
  ``mpv_shim``'s final action), so the single-instance lock, the tray and mpv
  are gone before the installer starts.

Dev builds (``+pokke.dev<n>``) and source runs never check: their number says
nothing about which release they are. Only an x64 machine with AVX2 is offered
the plain installer, since the LEGACY64 and ARM64 installers are other assets
and a frozen build cannot tell which it is.
"""

import hashlib
import hmac
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from collections import namedtuple
from urllib.parse import urljoin, urlsplit

import requests

from . import fork, restart
from .conf import settings

log = logging.getLogger("fork_update")

REPO = "pokke/jellyfin-mpv-shim"
REPO_URL = "https://github.com/%s/" % REPO
RELEASES_URL = REPO_URL + "releases/"
#: By numeric repository id, not by name: a name can be released by its owner
#: and registered by someone else, an id cannot. If the repository is renamed
#: the download and page URLs below stop matching and the offer fails closed.
REPO_ID = 1429589493
API_URL = ("https://api.github.com/repositories/%d/releases?per_page=10"
           % REPO_ID)
DOWNLOAD_PREFIX = RELEASES_URL + "download/"

#: Where a download (and each redirect of it) may be fetched from.
ALLOWED_HOSTS = frozenset((
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
))
MAX_REDIRECTS = 4

#: Every directory this module makes for a download starts with this.
TEMP_PREFIX = "jellyfin-mpv-shim-update-"

#: What the download is saved as: a constant, so nothing the network said
#: becomes part of a path.
INSTALLER_FILENAME = "setup.exe"

#: The whole command line the installer is started with. Constants.
INSTALLER_ARGS = ("/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
                  "/RELAUNCH=1")

_TAG = re.compile(r"^v.+-pokke\.(\d+)$")
_ASSET = re.compile(
    r"jellyfin-mpv-shim_[0-9A-Za-z][0-9A-Za-z._+-]*-pokke\.(\d+)_installer\.exe")
_DIGEST = re.compile(r"^sha256:([0-9a-fA-F]{64})$")
_SUFFIX = re.compile(r"^\+pokke\.(\d+)$")

#: ``(read timeout)`` for the API call and for a download that has stalled.
API_TIMEOUT = (3, 10)
DOWNLOAD_TIMEOUT = (5, 30)

Offer = namedtuple("Offer", "n version name url size sha256 html_url")


class UpdateError(Exception):
    """A refusal, with a short reason that is safe to show: never a URL."""


# -- who we are --------------------------------------------------------------

def running_build():
    """The release number this build is, or None for a dev/unstamped build.

    ``fork.RELEASE_BUILD`` when it exists; otherwise the same parse of
    ``fork.SUFFIX`` done here, so this module does not depend on the order the
    two tracks landed in. ``+pokke.dev<n>`` deliberately does not match.
    """
    if hasattr(fork, "RELEASE_BUILD"):
        return fork.RELEASE_BUILD
    match = _SUFFIX.match(getattr(fork, "SUFFIX", "") or "")
    return int(match.group(1)) if match else None


def _has_avx2():
    try:
        import ctypes

        # PF_AVX2_INSTRUCTIONS_AVAILABLE
        return bool(ctypes.windll.kernel32.IsProcessorFeaturePresent(40))
    except Exception:
        return False


def _platform_ok():
    """Windows, frozen, x64 with AVX2: the machine the plain installer is for."""
    return (os.name == "nt"
            and bool(getattr(sys, "frozen", False))
            and platform.machine().upper() == "AMD64"
            and _has_avx2())


def _command_is_plain():
    """Whether a relaunch would be exactly what the user started.

    The installer relaunches ``run.exe`` with no arguments, so a launch that
    carries ``--config`` (or anything else ``restart.command`` keeps) would
    come back as a different app. That launch is shown the release link only.
    """
    try:
        cmd = restart.command()
    except Exception:
        return False
    return cmd is not None and len(cmd) == 1


# -- looking -----------------------------------------------------------------

_lock = threading.Lock()
_thread = None            # the one check thread of this process
_install_thread = None
_staged = None
_armed = False


def _reset_for_tests():
    global _thread, _install_thread, _staged, _armed
    with _lock:
        _thread = _install_thread = _staged = None
        _armed = False


def fetch_releases():
    """The newest releases of the pinned repository. No credentials, ever."""
    from .constants import USER_AGENT

    response = requests.get(
        API_URL, timeout=API_TIMEOUT, verify=True, allow_redirects=False,
        headers={"User-Agent": USER_AGENT,
                 "Accept": "application/vnd.github+json"})
    try:
        if response.status_code != 200:
            raise UpdateError("release list answered %s"
                              % response.status_code)
        return response.json()
    finally:
        response.close()


def _bad_path(path):
    """Whether ``path`` has a dot segment, plain or percent-encoded, or a
    backslash. Checked on the decoded form too, so ``%2e%2E`` cannot hide."""
    from urllib.parse import unquote

    for text in (path, unquote(path), unquote(unquote(path))):
        if "\\" in text or any(seg in (".", "..")
                                for seg in text.split("/")):
            return True
    return False


def _in_repo(url):
    try:
        parts = urlsplit(url)
        return (parts.scheme == "https" and parts.hostname == "github.com"
                and parts.port in (None, 443)
                and parts.username is None
                and parts.path.startswith("/" + REPO + "/")
                and not _bad_path(parts.path))
    except (ValueError, AttributeError):
        return False


def _offer_for(release):
    if not isinstance(release, dict):
        return None
    if release.get("draft") or release.get("prerelease"):
        return None
    tag = release.get("tag_name")
    if not isinstance(tag, str):
        return None
    match = _TAG.match(tag)
    if match is None:
        return None
    n = int(match.group(1))
    assets = release.get("assets")
    if not isinstance(assets, list):
        return None
    for item in assets:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        found = _ASSET.fullmatch(name) if isinstance(name, str) else None
        if found is None or int(found.group(1)) != n:
            continue
        digest = item.get("digest")
        digest = _DIGEST.match(digest) if isinstance(digest, str) else None
        size = item.get("size")
        url = item.get("browser_download_url")
        if (digest is None or isinstance(size, bool)
                or not isinstance(size, int) or size <= 0
                or not isinstance(url, str)
                or not url.startswith(DOWNLOAD_PREFIX)):
            continue
        html = release.get("html_url")
        if not (isinstance(html, str) and _in_repo(html)):
            html = RELEASES_URL + "latest"
        return Offer(n, tag[1:], name, url, size, digest.group(1).lower(),
                     html)
    return None


def find_update(running, releases):
    """The offer for the highest release numbered above ``running``, or None.

    Ordered by the number in the tag, never by list position or ``/latest``:
    a re-run or a hand-made release can put them out of order. A release that
    is not usable (draft, odd tag, no matching asset, no digest) is skipped
    without hiding a usable one.
    """
    best = None
    for release in releases if isinstance(releases, list) else ():
        offer = _offer_for(release)
        if offer is not None and offer.n > running:
            if best is None or offer.n > best.n:
                best = offer
    return best


def check(checker):
    """Start the (one) background look for a newer fork release.

    Returns at once. It is called from ``PlayerManager._play_media`` under the
    player's lock as well as from the startup check, and a network round trip
    there would freeze pause and seek; so the work is a daemon thread, started
    once per process.
    """
    global _thread
    if not fork.IS_FORK or running_build() is None:
        return
    if not settings.check_updates or not settings.notify_updates_wanted():
        return
    with _lock:
        if _thread is not None:
            return
        _thread = threading.Thread(target=_look, args=(checker,),
                                   name="fork-update-check", daemon=True)
        _thread.start()


def cleanup_stale():
    """Delete this updater's leftover temp directories from earlier updates.

    Only directories whose name starts with our prefix, directly inside the
    temp root, and never through a link: a symlink or junction named like ours
    is left alone rather than followed.
    """
    try:
        entries = list(os.scandir(_temp_base()))
    except OSError:
        return
    for entry in entries:
        try:
            if not entry.name.startswith(TEMP_PREFIX):
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            is_junction = getattr(entry, "is_junction", None)
            if is_junction is not None and is_junction():
                continue
            shutil.rmtree(entry.path, ignore_errors=True)
        except OSError:
            log.debug("could not remove an old update directory",
                      exc_info=True)


def _look(checker):
    try:
        # Here, not in check(): the probe can be slow on its first call, and
        # check() runs under the player lock.
        if not _platform_ok():
            return
        cleanup_stale()
        running = running_build()
        offer = find_update(running, fetch_releases())
        if offer is None:
            log.info("Up to date (build %s).", running)
            return
        _announce(checker, offer)
    except Exception as exc:
        # The class only: a requests error's text can carry a signed URL.
        log.debug("Could not check for fork updates (%s).",
                  type(exc).__name__, exc_info=False)


def _announce(checker, offer):
    # Where the menu entry and the notice point. Upstream's page is not an
    # option: it would offer the user someone else's build.
    checker.release_url = RELEASES_URL
    checker.new_version = offer.version
    if settings.update_skip_version == offer.version:
        log.info("Update %s available; the user asked to skip it.",
                 offer.version)
        return
    if checker.has_notified:
        return
    checker.has_notified = True
    log.info("Update Available: %s", offer.version)
    notify_ui = getattr(checker.playerManager, "notify_update", None)
    if notify_ui is None:
        checker.notify()
        return
    try:
        if _command_is_plain():
            notify_ui(offer.version, offer.html_url,
                      install=lambda progress, failed: start_install(
                          offer, progress, failed))
        else:
            notify_ui(offer.version, offer.html_url)
    except Exception:
        log.error("Could not send update notice to the UI.", exc_info=True)


# -- downloading -------------------------------------------------------------

def _temp_base():
    return os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()


def _url_ok(url, first):
    if first and not url.startswith(DOWNLOAD_PREFIX):
        return False
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return False
    if first and _bad_path(parts.path):
        return False
    return (parts.scheme == "https" and host in ALLOWED_HOSTS
            and port in (None, 443)
            and parts.username is None and parts.password is None
            and "@" not in parts.netloc)


class _LockedFile:
    """A read handle that nothing else may write to, rename or delete."""

    def __init__(self, handle, kernel32):
        self._h = handle
        self._k = kernel32
        self.closed = False

    def read(self, size):
        import ctypes
        from ctypes import wintypes

        buf = ctypes.create_string_buffer(size)
        got = wintypes.DWORD(0)
        if not self._k.ReadFile(self._h, buf, size, ctypes.byref(got), None):
            raise OSError("could not read the installer")
        return buf.raw[:got.value]

    def close(self):
        if not self.closed:
            self.closed = True
            self._k.CloseHandle(self._h)


def _open_locked(path):
    """``path`` opened for reading with **no** write/delete sharing.

    Raises where that cannot be done: this is a security property, so there
    is no quieter fallback.
    """
    if os.name != "nt":
        raise OSError("the locked open is a Windows facility")
    import ctypes
    from ctypes import wintypes

    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                              wintypes.DWORD, ctypes.c_void_p,
                              wintypes.DWORD, wintypes.DWORD,
                              ctypes.c_void_p]
    k.CreateFileW.restype = ctypes.c_void_p
    k.ReadFile.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
    k.ReadFile.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CloseHandle.restype = wintypes.BOOL
    GENERIC_READ, FILE_SHARE_READ, OPEN_EXISTING = 0x80000000, 1, 3
    handle = k.CreateFileW(path, GENERIC_READ, FILE_SHARE_READ, None,
                           OPEN_EXISTING, 0, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError("could not open the installer (%d)"
                      % ctypes.get_last_error())
    return _LockedFile(handle, k)


class Staged:
    """A downloaded, verified installer and the handle that guards it."""

    def __init__(self, offer, directory, path, handle):
        self.offer = offer
        self.dir = directory
        self.path = path
        self.handle = handle

    @property
    def closed(self):
        return self.handle is None or getattr(self.handle, "closed", False)

    def close(self):
        handle, self.handle = self.handle, None
        if handle is not None:
            try:
                handle.close()
            except Exception:
                log.debug("could not close the installer handle",
                          exc_info=True)


def _remove(directory):
    if directory:
        shutil.rmtree(directory, ignore_errors=True)


def _fetch(offer, path, progress):
    url = offer.url
    for hop in range(MAX_REDIRECTS + 1):
        if not _url_ok(url, first=(hop == 0)):
            raise UpdateError("download address refused")
        from .constants import USER_AGENT

        response = requests.get(
            url, stream=True, allow_redirects=False, verify=True,
            timeout=DOWNLOAD_TIMEOUT,
            headers={"User-Agent": USER_AGENT,
                     "Accept": "application/octet-stream",
                     "Accept-Encoding": "identity"})
        try:
            if response.status_code in (301, 302, 303, 307, 308):
                where = response.headers.get("location")
                if not where:
                    raise UpdateError("redirect without a destination")
                url = urljoin(url, where)
                continue
            if response.status_code != 200:
                raise UpdateError("download answered %s"
                                  % response.status_code)
            done = 0
            last = -1
            with open(path, "wb") as out:
                for chunk in response.iter_content(1 << 16):
                    done += len(chunk)
                    if done > offer.size:
                        raise UpdateError("download is larger than expected")
                    out.write(chunk)
                    pct = done * 100 // offer.size
                    if progress is not None and pct != last:
                        last = pct
                        progress(pct)
            if done != offer.size:
                raise UpdateError("download is the wrong size")
            return
        finally:
            response.close()
    raise UpdateError("too many redirects")


def _verify(handle, offer):
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = handle.read(1 << 20)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
    if total != offer.size:
        raise UpdateError("installer is the wrong size")
    if not hmac.compare_digest(digest.hexdigest(), offer.sha256):
        raise UpdateError("installer does not match its checksum")


def download(offer, progress=None):
    """Fetch ``offer`` into a fresh private directory and return it ``Staged``.

    On any refusal the directory, and so the file, is removed.
    """
    try:
        directory = tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=_temp_base())
    except OSError:
        directory = tempfile.mkdtemp(prefix=TEMP_PREFIX)
    path = os.path.join(directory, INSTALLER_FILENAME)
    handle = None
    try:
        _fetch(offer, path, progress)
        handle = _open_locked(path)
        _verify(handle, offer)
    except BaseException:
        if handle is not None:
            handle.close()
        _remove(directory)
        raise
    return Staged(offer, directory, path, handle)


def _reason(exc):
    if isinstance(exc, UpdateError):
        return str(exc)
    # Never the text: a requests error can carry a signed URL, an OSError a
    # path. The class says enough to act on, and the debug log has the rest.
    return type(exc).__name__


def start_install(offer, progress, failed):
    """Download on a thread of its own, then install. False if one is running.

    Not the UI pool: a download is minutes long and would take a worker the
    browser needs for artwork.
    """
    global _install_thread
    with _lock:
        if _install_thread is not None and _install_thread.is_alive():
            return False
        _install_thread = threading.Thread(
            target=_install_worker, args=(offer, progress, failed),
            name="fork-update-download", daemon=True)
        _install_thread.start()
    return True


def _install_worker(offer, progress, failed):
    global _staged
    staged = None
    try:
        staged = download(offer, progress)
        with _lock:
            _staged = staged
        if install_now():
            return
        reason = "could not close the app"
    except Exception as exc:
        log.debug("Update failed (%s).", type(exc).__name__)
        log.debug("details", exc_info=True)
        reason = _reason(exc)
    if staged is not None:
        with _lock:
            if _staged is staged:
                _staged = None
        staged.close()
        _remove(staged.dir)
    failed(reason)


# -- installing --------------------------------------------------------------

def _user_interface():
    from .mpvtk_browser.ui import user_interface

    return user_interface


def armed():
    return _armed


def _set_armed(value):
    global _armed
    with _lock:
        _armed = value


def install_now():
    """Arm the installer launch and start the ordinary shutdown.

    Mirrors ``restart_app`` in gateway/diagnostics.py, including its trap: if
    the shutdown never began the flag must be cleared, or the user's next
    ordinary quit runs an installer.
    """
    if _staged is None:
        return False
    # A restart armed earlier would relaunch the old app beside the installer.
    # Remembered, so a quit that does not start puts it back.
    pending = restart.requested()
    pending_settings = restart._pending_settings
    restart.cancel()
    _set_armed(True)
    started = False
    try:
        started = _user_interface().quit_app()
    except Exception:
        log.exception("could not start the update's shutdown")
    if not started:
        _set_armed(False)
        if pending:
            restart.request(pending_settings)
        return False
    return True


def run_installer_if_requested():
    """Run the staged installer, if armed. The process's last act but one.

    Fast by design: ``exit_watchdog`` gives the final action ten seconds.
    Never raises. If the installer cannot be started the old app is brought
    back instead, since by now it has already shut down.
    """
    global _staged, _armed
    with _lock:
        if not _armed or _staged is None:
            return False
        staged, _staged, _armed = _staged, None, False
    try:
        if staged.closed:
            raise OSError("the installer handle was lost")
        kwargs = {"close_fds": True, "env": restart.child_env(),
                  "creationflags": getattr(
                      subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        log.info("Starting the update installer.")
        subprocess.Popen([staged.path] + list(INSTALLER_ARGS), **kwargs)
        return True
    except Exception:
        log.exception("Could not start the update installer; restarting "
                      "the current version.")
        try:
            restart.request()
            restart.relaunch_if_requested()
        except Exception:
            log.exception("Could not restart either.")
        return False
    finally:
        # After Popen: the new process has the image mapped by now, which is
        # what keeps the file unchangeable from here on.
        staged.close()
