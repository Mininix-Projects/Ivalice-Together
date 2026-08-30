from __future__ import annotations

import os
import time
from pathlib import Path


SHUTDOWN_FILE_ENV = "FF12_COOP_SHUTDOWN_FILE"
PRESERVE_RESIDENT_RUNTIME_MARKER = "preserve-resident-runtime-v1"
POLL_INTERVAL_SECONDS = 0.10

_last_check = 0.0
_requested = False


def requested(*, force: bool = False) -> bool:
    global _last_check, _requested
    if _requested:
        return True
    raw_path = os.environ.get(SHUTDOWN_FILE_ENV)
    if not raw_path:
        return False
    now = time.monotonic()
    if not force and now - _last_check < POLL_INTERVAL_SECONDS:
        return False
    _last_check = now
    try:
        _requested = Path(raw_path).is_file()
    except (OSError, ValueError):
        # The shutdown marker is advisory.  A transient filesystem failure or
        # malformed inherited environment must never terminate a healthy
        # helper; retry the observation on the next polling interval.
        _requested = False
    return _requested


def preserve_resident_runtime(*, force: bool = False) -> bool:
    """Return whether cooperative exit must leave every remote byte resident."""

    raw_path = os.environ.get(SHUTDOWN_FILE_ENV)
    if not raw_path or not requested(force=force):
        return False
    try:
        marker = Path(raw_path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError, ValueError):
        # An unreadable requested marker cannot safely authorize destructive
        # remote teardown.  Preserve and let the supervisor authenticate the
        # replacement topology.
        return True
    return marker == PRESERVE_RESIDENT_RUNTIME_MARKER


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "PRESERVE_RESIDENT_RUNTIME_MARKER",
    "SHUTDOWN_FILE_ENV",
    "preserve_resident_runtime",
    "requested",
]
