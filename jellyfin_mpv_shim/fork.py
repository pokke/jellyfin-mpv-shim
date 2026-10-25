"""Fork build identity.

A build of this fork carries ``+pokke.<n>`` after the upstream version, so a
log, a server's device list or ``--version`` says which build it is.
``tools/stamp_fork_build.py`` writes ``_fork_build.py`` at build time; a source
checkout or an upstream build has no such file and keeps upstream's identity.

**The update check is switched off in a fork build.** Its notice points at
upstream's releases page, and installing what it offers would overwrite the
fork. The reason is recorded here rather than in docs/do-not-fix.md, which
upstream edits constantly.
"""

import re
from typing import Optional

try:
    from ._fork_build import SUFFIX
except ImportError:
    SUFFIX = ""

IS_FORK = bool(SUFFIX)


def release_build(suffix):
    """The release number in ``+pokke.<n>``, else None.

    A development build (``+pokke.dev<n>``) and an unstamped tree are not
    releases, so they have no number to compare against.
    """
    match = re.fullmatch(r"\+pokke\.(\d+)", suffix)
    return int(match.group(1)) if match else None


RELEASE_BUILD: Optional[int] = release_build(SUFFIX)


def display_version(base):
    """``base`` with the fork's build suffix, if this is a fork build."""
    return base + SUFFIX
