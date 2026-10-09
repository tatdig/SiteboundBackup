"""SHA-256 digests for backup images.

Backup images are **sparse**: the engine converts with ``qemu-img convert -S``,
so a 120 GiB disk whose guest is nearly empty occupies a couple of megabytes and
every unwritten byte reads back as zero. Hashing the file naively means reading
and digesting the whole apparent size, and the cost is dominated by the holes
rather than by the data:

===========================  ==========
Read + hash a 120 GiB image  ~51 s
Read only its real data      0.001 s
===========================  ==========

So there are two digest kinds, and which one produced a given value is recorded
alongside it:

``file``
    Sequential SHA-256 over every byte of the file, holes included. What every
    backup written before this module existed recorded. Correct, and slow in
    proportion to the *virtual* disk size.

``sparse-v1``
    A canonical digest over the regions that actually carry non-zero data. Reads
    in proportion to the *data*, not the capacity.

The sparse digest is deliberately **invariant to sparseness itself**. It walks
the file on a fixed block grid and digests a block only when that block contains
a non-zero byte; whether the other blocks are stored as holes or as allocated
zeros makes no difference. Copying a repository without preserving holes — which
materialises every hole — therefore still verifies, it just reads more. A digest
tied to the allocation map would have broken on exactly that, and the failure
would have looked like corruption.

It is also unambiguous: the virtual size and each block's offset and length are
fed into the hash, so a file truncated, shifted, or reassembled from the same
bytes at different offsets cannot collide with the original.
"""

from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
from typing import List, Optional, Tuple

from .errors import JobCancelled

#: Sequential SHA-256 over the whole file, holes included.
DIGEST_KIND_FILE = "file"

#: SHA-256 over the non-zero regions only.
DIGEST_KIND_SPARSE = "sparse-v1"

#: BLAKE2b over the non-zero regions only. Same coverage as ``sparse-v1`` and
#: roughly twice the throughput, which measured at 594 MiB/s against 312 MiB/s on
#: a real 5 GiB image. BLAKE2b is also a stronger hash than SHA-256, and it is in
#: the standard library, so this costs nothing to adopt.
#:
#: Full coverage is deliberate. Sampling a few hundred megabytes would be faster
#: still, but it turns an integrity check into a spot check: on a 5 GiB image a
#: 300 MB sample covers about 6% of the data, so corruption in the other 94%
#: would go unreported. Hashing is only a few percent of a backup anyway, because
#: the VDDK read dominates.
DIGEST_KIND_BLAKE2B = "blake2b-sparse"

#: The kind written for new backups.
DIGEST_KIND_CURRENT = DIGEST_KIND_BLAKE2B

#: Domain separation, so a sparse digest can never be confused with any other
#: SHA-256 over similar bytes.
_PREFIX = b"vmbackup-sparse-digest-v1\0"

#: Blocks are aligned to offset 0 and never to an extent boundary, because extent
#: boundaries move when a file is copied and that must not change the digest.
BLOCK_SIZE = 64 * 1024


class DigestError(RuntimeError):
    """The file could not be read for hashing."""


def _data_regions(fd: int, size: int) -> List[Tuple[int, int]]:
    """The allocated regions of an open file, as ``(start, end)`` pairs.

    Returns ``[(0, size)]`` when the filesystem cannot report sparseness, which
    degrades to reading everything rather than guessing. That is slower but the
    digest is unchanged, because blocks that turn out to be zero are skipped
    anyway.
    """
    regions: List[Tuple[int, int]] = []
    offset = 0

    while offset < size:
        try:
            start = os.lseek(fd, offset, os.SEEK_DATA)
        except OSError as exc:
            if exc.errno == errno.ENXIO:
                break  # no further data: everything left is a hole
            return [(0, size)]
        except ValueError:
            return [(0, size)]

        try:
            end = os.lseek(fd, start, os.SEEK_HOLE)
        except OSError:
            end = size

        if end <= start:
            end = size
        regions.append((start, min(end, size)))
        offset = end

    return regions


def _sha256():
    return hashlib.sha256()


def _blake2b():
    # 32-byte output, so the digest is the same width as the SHA-256 it replaces
    # and the stored column needs no widening.
    return hashlib.blake2b(digest_size=32)


def sparse_digest(
    path: Path,
    *,
    block_size: int = BLOCK_SIZE,
    cancel_event=None,
    factory=_sha256,
    regions: Optional[List[Tuple[int, int]]] = None,
    on_block=None,
) -> str:
    """Canonical digest over the non-zero data of a possibly sparse file.

    Reads in proportion to the data, and gives the same answer whether or not
    the holes are materialised. ``factory`` selects the hash; the walk is
    identical either way, so ``sparse-v1`` records stay verifiable.

    ``regions`` is a hint, never a definition: it says where data *may* be, and
    the digest hashes a block only when that block actually holds a non-zero
    byte. So any superset of the non-zero blocks produces the same value — which
    is what lets a recorded map stand in for the kernel's hole reporting
    without changing a single stored digest. When it is omitted the kernel is
    asked, and a filesystem that cannot answer means reading the whole file.

    ``on_block``, if given, is called with ``(start, end)`` for every block that
    is hashed — so a pass that has to read a file anyway can record its map on
    the way past. Runs are ``(start, end)`` there for the same reason they are in
    :mod:`vmbackup.holes`: a length and an end are the same number for a single
    block, and mixing them is invisible until it is a real corruption scare.
    """
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError as exc:
        raise DigestError(f"cannot open {path}: {exc}") from exc

    try:
        size = os.fstat(fd).st_size
        if regions is None:
            regions = _data_regions(fd, size)
        else:
            regions = list(regions)

        digest = factory()
        digest.update(_PREFIX)
        digest.update(str(size).encode("ascii"))
        digest.update(b"\0")

        region_index = 0
        offset = 0

        while offset < size:
            if cancel_event is not None and cancel_event.is_set():
                # JobCancelled, NOT DigestError. A cancellation is control flow;
                # DigestError means "this image cannot be hashed". Reporting the
                # first as the second is how a cancel becomes a false corruption
                # alarm: verify.digest_status maps DigestError to
                # STATUS_UNREADABLE, so remaining images get reported as
                # unreadable because the operator pressed cancel.
                raise JobCancelled("Cancelled while hashing")

            length = min(block_size, size - offset)
            block_end = offset + length

            # Walk the region list forward with the block, so this stays linear.
            while region_index < len(regions) and regions[region_index][1] <= offset:
                region_index += 1

            overlaps = (
                region_index < len(regions)
                and regions[region_index][0] < block_end
            )

            if overlaps:
                os.lseek(fd, offset, os.SEEK_SET)
                data = _read_exactly(fd, length)
                if any(data):
                    digest.update(offset.to_bytes(8, "big"))
                    digest.update(length.to_bytes(8, "big"))
                    digest.update(data)
                    if on_block is not None:
                        on_block(offset, offset + length)

            offset = block_end

        return digest.hexdigest()
    finally:
        os.close(fd)


def _read_exactly(fd: int, length: int) -> bytes:
    """Read ``length`` bytes, treating a short read at EOF as trailing zeros."""
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = os.read(fd, remaining)
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)

    data = b"".join(chunks)
    if len(data) < length:
        # A file shorter than the last block reads as zeros beyond its end.
        data += b"\0" * (length - len(data))
    return data


def file_digest(
    path: Path,
    *,
    block_size: int = 4 * 1024 * 1024,
    cancel_event=None,
) -> str:
    """Sequential SHA-256 over the whole file, holes included.

    The legacy kind, kept so that images hashed before the sparse digest existed
    can still be verified.
    """
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            while True:
                if cancel_event is not None and cancel_event.is_set():
                    raise DigestError("hashing cancelled")
                chunk = handle.read(block_size)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise DigestError(f"cannot read {path}: {exc}") from exc

    return digest.hexdigest()


def factory_for_kind(kind: Optional[str]):
    """The hash factory a kind names.

    Used when *writing* a digest, so the value and the recorded ``hash_kind``
    cannot disagree — they are chosen from the same place.
    """
    if kind == DIGEST_KIND_BLAKE2B:
        return _blake2b
    if kind == DIGEST_KIND_SPARSE:
        return _sha256
    return None  # sequential whole-file, handled by file_digest


def digest_for_kind(
    path: Path,
    kind: Optional[str],
    *,
    cancel_event=None,
    regions: Optional[List[Tuple[int, int]]] = None,
    on_block=None,
) -> str:
    """Compute the digest using the algorithm ``kind`` names.

    An unknown or absent kind means legacy: those values were produced by the
    sequential digest, and assuming otherwise would report every pre-existing
    backup as corrupt. A legacy value covers every byte of the file including its
    holes, so ``regions`` cannot shorten it — there is nothing to skip.
    """
    if kind == DIGEST_KIND_BLAKE2B:
        return sparse_digest(
            path,
            cancel_event=cancel_event,
            factory=_blake2b,
            regions=regions,
            on_block=on_block,
        )
    if kind == DIGEST_KIND_SPARSE:
        return sparse_digest(
            path, cancel_event=cancel_event, regions=regions, on_block=on_block
        )
    return file_digest(path, cancel_event=cancel_event)


def read_block_digest(
    path: Path,
    *,
    size: Optional[int] = None,
    block_size: int = BLOCK_SIZE,
) -> Tuple[List[Tuple[int, bytes]], int]:
    """Non-zero blocks of a file, for comparing two images block by block.

    Returns ``(blocks, total_size)`` where each entry is ``(offset, bytes)``.
    Only used for diagnostics and tests; the digest is the cheap path.
    """
    fd = os.open(str(path), os.O_RDONLY)
    try:
        total = os.fstat(fd).st_size if size is None else size
        blocks: List[Tuple[int, bytes]] = []
        offset = 0
        while offset < total:
            length = min(block_size, total - offset)
            os.lseek(fd, offset, os.SEEK_SET)
            data = _read_exactly(fd, length)
            if any(data):
                blocks.append((offset, data))
            offset += length
        return blocks, total
    finally:
        os.close(fd)
