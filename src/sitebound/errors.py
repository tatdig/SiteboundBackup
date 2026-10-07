"""Exception hierarchy shared by the engine, the job manager and the API.

Every error that is allowed to reach a client carries a stable ``code`` string
and an ``http_status``. The API translates them into the error envelope that
``docs/API.md`` specifies; anything that is *not* a :class:`VmbackupError` is a
bug and is reported as a generic internal error without leaking a traceback.
"""

from __future__ import annotations

from typing import Any, Optional


class VmbackupError(Exception):
    """Base class for all expected, client-reportable failures."""

    code: str = "internal_error"
    http_status: int = 500

    def __init__(self, message: str, *, detail: Optional[Any] = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "detail": self.detail,
            }
        }


class ConfigError(VmbackupError):
    """The config file is missing, unreadable or fails validation."""

    code = "config_error"
    http_status = 503


class UnavailableError(VmbackupError):
    """A required subsystem is not ready (worker missing, storage absent)."""

    code = "unavailable"
    http_status = 503


class UnauthorizedError(VmbackupError):
    """Missing or incorrect API token."""

    code = "unauthorized"
    http_status = 401


class BadRequestError(VmbackupError):
    """The caller sent something structurally valid but semantically wrong."""

    code = "bad_request"
    http_status = 400


class NotFoundError(VmbackupError):
    """A referenced VM, job or backup does not exist."""

    code = "not_found"
    http_status = 404


class ConflictError(VmbackupError):
    """The request contradicts current state, e.g. cancelling a finished job."""

    code = "conflict"
    http_status = 409


class EsxiError(VmbackupError):
    """vSphere could not be reached or refused an operation."""

    code = "esxi_error"
    http_status = 502


class SnapshotCleanupError(VmbackupError):
    """The temporary snapshot could not be removed and needs manual attention.

    This is deliberately distinct from a backup failure: the backup itself may
    have succeeded, but leaving an orphan snapshot on the VM is serious enough
    that it must be surfaced loudly rather than swallowed.
    """

    code = "snapshot_cleanup_failed"
    http_status = 500


class JobCancelled(Exception):
    """Internal control-flow signal: the operator cancelled the running job.

    Not a :class:`VmbackupError` because it is never rendered directly; the job
    manager catches it and records a ``cancelled`` terminal status instead.
    """
