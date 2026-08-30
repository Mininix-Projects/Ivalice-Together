from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
from pathlib import Path


WINDOWS_ERROR_NOT_SAME_DEVICE = 17
REPLACE_ATTEMPTS = 20
REPLACE_RETRY_SECONDS = 0.01


def _is_cross_device(error: OSError) -> bool:
    return error.errno == errno.EXDEV or getattr(error, "winerror", None) == (
        WINDOWS_ERROR_NOT_SAME_DEVICE
    )


def replace_file(source: Path, destination: Path) -> None:
    """Replace a file, including through virtual filesystems such as MO2's."""

    source = Path(source)
    destination = Path(destination)
    try:
        os.replace(source, destination)
    except OSError as error:
        if not _is_cross_device(error):
            raise
        shutil.copyfile(source, destination)
        source.unlink()


def write_text(path: Path, payload: str, *, encoding: str = "utf-8") -> None:
    """Write text atomically when possible, with a cross-device fallback."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(REPLACE_ATTEMPTS):
            try:
                replace_file(temporary, path)
                break
            except PermissionError:
                if attempt + 1 == REPLACE_ATTEMPTS:
                    raise
                time.sleep(REPLACE_RETRY_SECONDS)
    finally:
        temporary.unlink(missing_ok=True)


__all__ = ["replace_file", "write_text"]
