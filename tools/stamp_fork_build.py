#!/usr/bin/env python3
"""Stamp a fork build: write ``jellyfin_mpv_shim/_fork_build.py``.

Reads the build identity from the environment variable ``JMS_FORK_BUILD``:

- a bare number (a CI run number) gives the suffix ``+pokke.dev<n>``: a
  development build, which is never offered an update;
- a release tag ``v<base>-pokke.<n>`` gives ``+pokke.<n>``, a release build,
  and is refused unless ``<base>`` is the ``CLIENT_VERSION`` in constants.py
  -- a tag that names a version the tree is not at would ship a build under
  the wrong name.

The two are kept apart because CI run numbers and release numbers are two
counters: a dev build must never read as a release newer than itself.

constants.py is read as text, not imported: importing the package here would
read whatever stamp is already in the tree. See jellyfin_mpv_shim/fork.py.
"""

import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONSTANTS = os.path.join(ROOT, "jellyfin_mpv_shim", "constants.py")
OUTPUT = os.path.join(ROOT, "jellyfin_mpv_shim", "_fork_build.py")


def base_version():
    with open(CONSTANTS, encoding="utf-8") as handle:
        match = re.search(r'^CLIENT_VERSION = "([^"]+)"', handle.read(), re.M)
    if match is None:
        raise SystemExit("stamp_fork_build: no CLIENT_VERSION in constants.py")
    return match.group(1)


def suffix_for(value):
    """``+pokke.dev<n>`` for a build number, ``+pokke.<n>`` for a matching
    tag, else None."""
    if re.fullmatch(r"\d+", value):
        return "+pokke.dev%s" % value
    tag = re.fullmatch(r"v(.+)-pokke\.(\d+)", value)
    if tag is None:
        return None
    if tag.group(1) != base_version():
        print("stamp_fork_build: tag %s is for version %s but constants.py "
              "says %s" % (value, tag.group(1), base_version()),
              file=sys.stderr)
        return None
    return "+pokke.%s" % tag.group(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", default=OUTPUT, help="file to write")
    args = parser.parse_args()
    value = os.environ.get("JMS_FORK_BUILD", "").strip()
    if not value:
        print("stamp_fork_build: JMS_FORK_BUILD is not set", file=sys.stderr)
        return 1
    suffix = suffix_for(value)
    if suffix is None:
        print("stamp_fork_build: JMS_FORK_BUILD=%r is neither a build number "
              "nor a v<version>-pokke.<n> tag" % value, file=sys.stderr)
        return 1
    with open(args.out, "w", encoding="utf-8") as handle:
        handle.write("# Written by tools/stamp_fork_build.py. Not in git.\n"
                     'SUFFIX = "%s"\n' % suffix)
    print("Stamped %s: %s" % (args.out, suffix))
    return 0


if __name__ == "__main__":
    sys.exit(main())
