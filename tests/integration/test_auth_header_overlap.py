"""Two starts overlapping must not hand one item the other's auth header.

``play()`` decides ``http-header-fields`` before its url is built and
outside ``_lock`` (docs/auth-headers.md section 1), and the option is global.
So a second start -- a cast, a browser click, an auto-advance on another
thread -- could write its own decision while the first was still loading:
installing our token while the first item's third-party subtitle was about
to be fetched, or clearing it from an item whose url relies on it. A start
that re-created mpv in ``_ensure_mpv`` had the same hole: the new handle
holds no header, and nothing put it back.

What is asserted: during a start's ``_play_media``, mpv carries exactly the
decision that start's url was built on, and no other start can change it.
"""

import os
import sys
import threading
import time
import unittest
from types import SimpleNamespace as NS
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402

from test_playback_start import build, make_video, player_module  # noqa: E402

OURS = 'Authorization: MediaBrowser Token="A"'
THEIRS = 'Authorization: MediaBrowser Token="B"'


def _client(token):
    return NS(http=NS(
        _get_authenication_header=lambda: 'MediaBrowser Token="%s"' % token))


def _other_start(token):
    """The second start's video, as far as `_apply_auth_headers` reads it."""
    return NS(client=_client(token), foreign_subtitle_hosts=lambda: set())


class OverlappingStartsTest(unittest.TestCase):
    def setUp(self):
        self.pm = build(test=self)

    def _start(self, video, answer_after=0.05):
        timer = threading.Timer(
            answer_after,
            lambda: self.pm._player.fire_property("duration", 100.0))
        timer.daemon = True
        timer.start()
        self.addCleanup(timer.cancel)
        with mock.patch.object(player_module.settings, "playback_timeout", 5):
            self.pm._play_media(video, "http://example.invalid/s.mkv",
                                is_initial_play=True)

    def test_a_header_another_start_installed_is_cleared_before_the_load(self):
        """A refused the header (a subtitle on a third-party host), then B's
        preamble installed B's before A reached _play_media."""
        video = make_video()
        video.auth_via_header = False
        self.pm._player.http_header_fields = [THEIRS]
        self.pm.journal.mark("other start installed its header")
        self._start(video)
        self.pm.journal.since("test:other start installed its header").order(
            "mpv.set:http_header_fields=[]", "mpv.play")

    def test_this_items_header_is_back_on_the_mpv_that_loads_it(self):
        """A's url carries no token because the header took; by the load the
        option holds nothing (cleared by an overlapping start, or a fresh
        mpv from _ensure_mpv)."""
        video = make_video(client=_client("A"))
        video.auth_via_header = True
        self.pm._player.http_header_fields = []
        self.pm.journal.mark("header gone")
        self._start(video)
        self.pm.journal.since("test:header gone").order(
            "mpv.set:http_header_fields=%r" % ([OURS],), "mpv.play")

    def test_another_start_cannot_write_the_header_during_a_load(self):
        video = make_video()
        video.auth_via_header = False
        loader = threading.Thread(
            target=self._start, args=(video,), kwargs={"answer_after": 0.6},
            daemon=True)
        loader.start()
        deadline = time.monotonic() + 5
        while not self.pm.journal.entries("mpv.play"):
            self.assertLess(time.monotonic(), deadline, "the load never began")
            time.sleep(0.005)

        writer = threading.Thread(
            target=self.pm._apply_auth_headers, args=(_other_start("B"),),
            daemon=True)
        writer.start()
        writer.join(0.3)
        self.assertTrue(writer.is_alive(),
                        "another start wrote the header mid-load")
        self.assertEqual(self.pm._player.http_header_fields, [])

        loader.join(5)
        writer.join(5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(self.pm._player.http_header_fields, [THEIRS])


if __name__ == "__main__":
    unittest.main()
