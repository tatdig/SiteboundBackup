"""Which stored runs depend on which, so retention cannot leave a delta orphaned.

A site's runs used to be independent: every capture is a full, so "keep the newest
N" was a complete rule. Transferred appliance runs are **chains** — a full and the
deltas built on it — and a delta without its full is not a restore point, it is a
file with a manifest. Keep two *runs* of a six-run chain and what is left is a
delta and its parent, or a delta alone: either way the repository claims a restore
point it cannot produce.

So the unit of retention is the **chain**:

* a run is a chain head when its first disk has no ``parent_image`` (the appliance
  writes that for a full, and a site's own captures are all fulls);
* every other run belongs to the chain of the head it descends from, so the
  dependency is read from manifests the repository already holds — no database, no
  hypervisor, nothing to keep in step;
* keeping ``N`` keeps the newest ``N`` chains *entirely*, and removes the others
  entirely. Nothing is ever kept whose ancestors were removed.

For a repository of independent fulls this is exactly the old rule, because every
full is its own chain — which is what makes it safe to apply to sites that have
never seen a delta.

A chain whose head is missing (the appliance's retention pruned an old full) is its
own chain and is *kept if it falls within the count*: deleting an orphan is not
retention's job, and a caller that wants to complain about it can see it in
:func:`orphans`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple


@dataclass(frozen=True)
class Stored:
    """One stored run: its directory and the manifest that describes it."""

    run: Path
    manifest: Dict[str, Any]

    @property
    def label(self) -> str:
        return Path(self.run).name

    @property
    def managed(self) -> bool:
        """Whether this run has a manifest this code can reason about.

        An unreadable manifest is not a reason to delete a directory, and it is not
        a reason to keep it either *by this rule*: retention cannot tell how old it
        is, so it leaves it alone and :func:`unmanaged` reports it. Guessing —
        treating it as oldest, which an empty ``created_utc`` would do — is how a
        migration's only copy of something gets pruned for being illegible.
        """
        return bool(self.manifest)

    @property
    def created_utc(self) -> str:
        return str(self.manifest.get("created_utc") or "")

    @property
    def image(self) -> str:
        """The first disk's image, as a bare name — what a parent refers to."""
        disks = self.manifest.get("disks") or [{}]
        name = str((disks[0] or {}).get("image") or "")
        return Path(name).name if name else ""

    @property
    def parent(self) -> str:
        """The image this run must be applied on top of, or ``""`` for a full."""
        disks = self.manifest.get("disks") or [{}]
        parent = str((disks[0] or {}).get("parent_image") or "")
        return Path(parent).name if parent else ""

    @property
    def is_full(self) -> bool:
        return not self.parent

    @property
    def sort_key(self) -> Tuple[str, float, str]:
        """Newest last. The same rule both site prunes already use.

        The directory name carries a timestamp but two runs inside one second tie
        on it; the manifest's ``created_utc`` breaks most ties and the directory's
        mtime breaks the rest, because the manifest is written last.
        """
        try:
            mtime = Path(self.run).stat().st_mtime
        except OSError:
            mtime = 0.0
        return (self.created_utc, mtime, self.label)


def load(vm_dir: Path) -> List[Stored]:
    """Every run of a VM, newest first.

    An unreadable manifest is not a crash and not a deletion: the run is kept as a
    manifest-less entry so the caller can see it, and it sorts by its directory
    (its ``created_utc`` is empty, which puts it oldest among equals rather than
    pretending to a time it does not have).
    """
    stored: List[Stored] = []
    for run in sorted(p for p in Path(vm_dir).iterdir() if p.is_dir()):
        manifest: Dict[str, Any] = {}
        try:
            data = json.loads((run / "manifest.json").read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                manifest = data
        except (OSError, ValueError):
            manifest = {}
        stored.append(Stored(run=run, manifest=manifest))
    return sorted(stored, key=lambda entry: entry.sort_key, reverse=True)


def chains(stored: Iterable[Stored]) -> List[List[Stored]]:
    """The runs grouped into chains, newest chain first, each oldest run first.

    Walks each run back to its head through ``parent_image``. A parent that is not
    in the list — pruned, or named by a manifest that cannot be read — ends the
    walk, which makes that run a head: the chain is then incomplete and
    :func:`orphans` says so.
    """
    entries = list(stored)
    by_image = {entry.image: entry for entry in entries if entry.image}
    groups: List[List[Stored]] = []
    assigned = set()

    for entry in entries:  # newest first
        if entry.label in assigned:
            continue
        chain = [entry]
        assigned.add(entry.label)
        current = entry
        seen = {entry.label}
        while True:
            parent = by_image.get(current.parent) if current.parent else None
            if parent is None or parent.label in seen:
                break
            seen.add(parent.label)
            chain.append(parent)
            assigned.add(parent.label)
            current = parent
        chain.reverse()  # the full first, which is the order a restore applies
        groups.append(chain)
    return groups


def unmanaged(stored: Iterable[Stored]) -> List[Stored]:
    """Runs with no readable manifest: retention's business is elsewhere."""
    return [entry for entry in stored if not entry.managed]


def orphans(stored: Iterable[Stored]) -> List[Stored]:
    """Runs whose chain has no full: a delta nothing can be restored from.

    Reported rather than deleted. Retention's job is to bound growth, not to tidy
    up after a migration; and a delta whose full is missing is a fact about the
    repository that somebody should see.
    """
    entries = list(stored)
    by_image = {entry.image: entry for entry in entries if entry.image}
    out = []
    for entry in entries:
        if entry.is_full:
            continue
        parent = by_image.get(entry.parent)
        if parent is None:
            out.append(entry)
    return out


def prune_plan(stored: Iterable[Stored], keep: int) -> List[Stored]:
    """The runs to delete so that the newest ``keep`` chains survive whole.

    ``keep <= 0`` means "keep every chain", which is what an operator asks for when
    the repository is the only copy and they would rather have the disk full than a
    gap. Every run of every chain outside the newest ``keep`` is returned, oldest
    chain first, so a caller that logs as it deletes reads chronologically.
    """
    entries = [entry for entry in stored if entry.managed]
    if keep <= 0:
        return []
    groups = chains(entries)
    doomed: List[Stored] = []
    for chain in groups[keep:]:
        doomed.extend(chain)
    return sorted(doomed, key=lambda entry: entry.sort_key)


__all__ = [
    "Stored",
    "chains",
    "load",
    "orphans",
    "prune_plan",
    "unmanaged",
]
