"""Interprocess, crash-released per-channel OAuth locks (POSIX and Windows)."""

from __future__ import annotations

import hashlib
import os
import time
import unicodedata
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import YouTubeCredentialsError
from .store import validate_channel_name


@contextmanager
def channel_lock(directory: Path, channel: str, *, timeout: float = 10.0) -> Iterator[None]:
    """Serialize refresh/re-authorization without leaving stale exclusive locks.

    The lock file contains one placeholder byte, never OAuth information.
    OS advisory locks are released even if the holder crashes; the file stays
    outside the checkout and a hash avoids Windows path/casing/length problems.
    """
    name = validate_channel_name(channel)
    key = hashlib.sha256(unicodedata.normalize("NFC", name).casefold().encode("utf-8")).hexdigest()[:32]
    path = directory / (".oauth-" + key + ".lock")
    descriptor: int | None = None
    locked = False
    try:
        flags = os.O_CREAT | os.O_RDWR
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        if not os.path.isfile(path) or path.is_symlink() or path.is_junction():
            raise YouTubeCredentialsError("Unsafe YouTube OAuth lock file")
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        # Windows locks a byte range; make sure byte zero exists.
        if os.name == "nt" and os.fstat(descriptor).st_size == 0:
            os.write(descriptor, b"0")
        deadline = time.monotonic() + timeout
        while not locked:
            try:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError as exc:
                if os.name != "nt":
                    import errno

                    if exc.errno not in (errno.EACCES, errno.EAGAIN):
                        raise
                if time.monotonic() >= deadline:
                    raise YouTubeCredentialsError(
                        "Another process is using this YouTube channel; retry shortly"
                    ) from None
                time.sleep(0.1)
        yield
    except OSError:
        raise YouTubeCredentialsError("Could not lock YouTube channel credentials") from None
    finally:
        if descriptor is not None:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
