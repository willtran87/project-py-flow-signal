"""Read bounded, identity-checked regular files without following final-path links."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def identity(info: os.stat_result) -> tuple:
    # On Windows Python versions, stat and fstat can expose different ctime
    # semantics (creation versus metadata change). Device/inode, size and mtime
    # are comparable on both APIs; ctime would reject unchanged new files.
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def read_snapshot(path: Path, limit: int, root: Path | None = None) -> bytes:
    """Detect common link swaps and source changes; this is not an OS sandbox."""
    before = path.lstat()
    if is_link(path) or not stat.S_ISREG(before.st_mode):
        raise ValueError("Input must be a regular file, not a link or reparse point")
    resolved = path.resolve(strict=True)
    if root is not None and not resolved.is_relative_to(root.resolve(strict=True)):
        raise ValueError("Input resolves outside the scan root")
    if before.st_size > limit:
        raise ValueError(f"Input exceeds the {limit}-byte limit")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        opened = os.fstat(handle.fileno())
        if not stat.S_ISREG(opened.st_mode) or identity(before) != identity(opened):
            raise ValueError("Input identity changed before reading")
        raw = handle.read(limit + 1)
        after = os.fstat(handle.fileno())
    if len(raw) > limit:
        raise ValueError(f"Input exceeds the {limit}-byte limit")
    if (
        identity(opened) != identity(after)
        or identity(after) != identity(path.lstat())
        or is_link(path)
        or path.resolve(strict=True) != resolved
    ):
        raise ValueError("Input changed while being read; scan a stable checkout")
    return raw
