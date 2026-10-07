"""Small shared helpers."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Iterable, Optional, Tuple, Union

#: Characters allowed in a filesystem-safe slug derived from a VM name.
_UNSAFE_SLUG = re.compile(r"[^A-Za-z0-9._-]+")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """RFC 3339 UTC with a trailing ``Z``, second precision.

    This is the only timestamp format that crosses the API boundary; see
    docs/API.md.
    """
    return utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def age_seconds(stamp: Optional[str], now_epoch: float) -> Optional[int]:
    """Seconds since an RFC 3339 timestamp, or None when there is not one.

    Only a string is accepted. A `datetime` once reached this function by
    accident, and ``stamp.replace("Z", "+00:00")`` raised TypeError for a string
    argument -- which the `except` below silently turned into "unknown age". The
    guard makes the contract explicit instead of relying on an exception to
    enforce it.
    """
    if not stamp or not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(now_epoch - parsed.timestamp()))


def readable_by(
    path: Union[str, "os.PathLike[str]"],
    *,
    uid: int,
    gid: int,
    groups: Iterable[int] = (),
) -> Tuple[bool, str]:
    """Whether a *specific* user could read a file, and why not when they could not.

    ``os.access`` answers the question for the calling process only, which is
    exactly the wrong answer when the thing being checked is a credential read by
    a different service account. That gap is not hypothetical: the monitoring
    token was made group-readable for the Zabbix agent, but the directory holding
    it was mode 0710 owned by the web tier, so the agent could not traverse it —
    the item went unsupported, its triggers held their last value, and an alarm
    that should have cleared stayed open for thirteen hours. A check that ran as
    root reported the file as perfectly readable throughout.

    Returns ``(ok, reason)``. The reason names the first thing that blocks the
    path, so the fix is obvious from the message.
    """
    target = os.fspath(path)
    wanted = set(groups) | {gid}

    def denied(where: str, mode: int, owner: int, group: int, *, must_exec: bool) -> str:
        bit = 0o1 if must_exec else 0o4
        if uid == 0:
            return ""
        if uid == owner:
            allowed = mode & (bit << 6)
        elif group in wanted:
            allowed = mode & (bit << 3)
        else:
            allowed = mode & bit
        if allowed:
            return ""
        who = "owner" if uid == owner else ("group" if group in wanted else "other")
        return (
            f"{where} is mode {mode:04o} owned by uid {owner}, gid {group}, and "
            f"the reader (uid {uid}, groups {sorted(wanted)}) falls in the {who} "
            f"class, which {'allows' if allowed else 'does not allow'} "
            f"{'traverse' if must_exec else 'read'}"
        )

    parts = []
    current = os.path.abspath(target)
    while True:
        parts.append(current)
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    # Every parent needs traverse, then the file itself needs read.
    for directory in reversed(parts[1:]):
        try:
            info = os.stat(directory)
        except OSError as exc:
            return False, f"cannot inspect {directory}: {exc}"
        problem = denied(
            directory, info.st_mode & 0o7777, info.st_uid, info.st_gid, must_exec=True
        )
        if problem:
            return False, problem

    try:
        info = os.stat(parts[0])
    except OSError as exc:
        return False, f"{parts[0]} cannot be read: {exc}"

    problem = denied(
        parts[0], info.st_mode & 0o7777, info.st_uid, info.st_gid, must_exec=False
    )
    if problem:
        return False, problem

    return True, ""


def to_iso(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def allocated_bytes(path: Optional[str]) -> Optional[int]:
    """Blocks the filesystem actually handed to a file.

    Backup images are sparse: the engine converts with ``-S 4k``, so runs of
    zeros become holes. ``st_size`` therefore reports the whole *virtual* disk
    capacity while the file occupies far less, and only ``st_blocks`` reflects
    real consumption. Anything sizing a volume needs this number, not the
    capacity.

    Returns None when the path is unknown or unreadable (for example an image
    removed by retention), so the caller can show a dash rather than a zero.
    """
    if not path:
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return int(info.st_blocks) * 512


def timestamp_slug() -> str:
    """Compact UTC timestamp used in job snapshot names and directory names."""
    return utcnow().strftime("%Y%m%dT%H%M%SZ")


def safe_slug(name: str, *, fallback: str = "vm") -> str:
    """Make a VM name safe to use as a single path component.

    VM names are operator-controlled but come from vSphere, and a name such as
    ``../../etc`` must never be able to escape the repository directory. Any
    character outside a conservative allow-list is replaced, then parent
    references are neutralised and leading/trailing dots and dashes stripped so
    the result can never be ``.``, ``..`` or an absolute-looking path.
    """
    slug = _UNSAFE_SLUG.sub("-", name)

    # Collapse any surviving parent-directory reference. Do this after the
    # substitution above, because ``..`` survives it untouched: dots are on the
    # allow-list so that names like ``db.example`` keep their dots.
    while ".." in slug:
        slug = slug.replace("..", "-")

    slug = slug.strip("-.").strip()
    return slug or fallback


def human_bytes(value: Optional[int]) -> str:
    """Bytes as a short human string, for operator-facing messages.

    Deliberately blunt about the sparse distinction: a caller that prints this
    next to "apparent" is making a point that the two numbers differ, so rounding
    to one decimal place is enough and full precision would be noise.
    """
    if value is None:
        return "unknown"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if size < 1024 or unit == "PiB":
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"
