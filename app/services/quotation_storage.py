"""Replaceable quotation-artifact storage with a safe local adapter."""

from __future__ import annotations

import os
import re
import secrets
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from app.core.config import get_settings
from app.core.exceptions import QuotationGenerationError
from app.services.quotation_id import QuotationIdGenerator

_QUOTATION_ID_PATTERN: Final = re.compile(
    r"^[A-Za-z0-9](?:[A-Za-z0-9_-]{0,62}[A-Za-z0-9])?-[0-9]{8}-[0-9A-F]{4}$"
)
_RESERVATION_TOKEN_PATTERN: Final = re.compile(r"^[0-9a-f]{32}$")
_PDF_SUFFIX: Final = ".pdf"
_RESERVATION_SUFFIX: Final = ".reserve"
_DEFAULT_MAX_RESERVATION_ATTEMPTS: Final = 8
_MINIMUM_PDF: Final = b"%PDF-1.4\n%%EOF\n"

QuotationIdFactory = Callable[[], str]


class QuotationStorageError(QuotationGenerationError):
    """Safe technical failure at the quotation artifact boundary."""


class QuotationReservationExhaustedError(QuotationStorageError):
    """No unique filename could be reserved within the bounded retry limit."""


class UnsafeQuotationPathError(QuotationStorageError):
    """A candidate path escaped or could redirect outside the storage root."""


@dataclass(frozen=True, slots=True)
class QuotationReservation:
    """Opaque proof that this adapter exclusively reserved one quotation ID."""

    quotation_id: str
    _token: str


@dataclass(frozen=True, slots=True)
class StoredQuotationArtifact:
    """Backend-created reference to one finalized quotation PDF."""

    quotation_id: str
    path: Path


@runtime_checkable
class QuotationArtifactStorage(Protocol):
    """Minimal replaceable boundary for quotation PDF persistence."""

    def reserve(self, quotation_id: str | None = None) -> QuotationReservation:
        """Atomically reserve a fresh backend-generated quotation identifier."""
        ...

    def write(
        self,
        reservation: QuotationReservation,
        pdf_content: bytes,
    ) -> StoredQuotationArtifact:
        """Atomically finalize a complete PDF without overwriting an artifact."""
        ...

    def read(self, artifact: StoredQuotationArtifact) -> bytes:
        """Read and validate a finalized artifact."""
        ...

    def exists(self, artifact: StoredQuotationArtifact) -> bool:
        """Return whether a complete, valid artifact exists."""
        ...

    def cancel(self, reservation: QuotationReservation) -> None:
        """Release an unfinished reservation without touching final artifacts."""
        ...


class LocalQuotationStorage:
    """Store quotation PDFs beneath one configured local directory."""

    __slots__ = ("_id_factory", "_max_reservation_attempts", "_root")

    def __init__(
        self,
        id_factory: QuotationIdFactory | None = None,
        *,
        root: Path | None = None,
        max_reservation_attempts: int = _DEFAULT_MAX_RESERVATION_ATTEMPTS,
    ) -> None:
        if (
            not isinstance(max_reservation_attempts, int)
            or isinstance(max_reservation_attempts, bool)
            or max_reservation_attempts <= 0
        ):
            raise ValueError("max_reservation_attempts must be a positive integer")
        configured_root = get_settings().quote_storage_path if root is None else root
        try:
            configured_root.mkdir(parents=True, exist_ok=True)
            resolved_root = configured_root.resolve(strict=True)
        except OSError as error:
            raise _storage_error("initialize", error) from error
        if not resolved_root.is_dir():
            raise QuotationStorageError("Quotation storage root is not a directory")

        self._root = resolved_root
        self._id_factory = id_factory
        self._max_reservation_attempts = max_reservation_attempts

    @classmethod
    def from_generator(
        cls,
        generator: QuotationIdGenerator,
        *,
        root: Path | None = None,
        max_reservation_attempts: int = _DEFAULT_MAX_RESERVATION_ATTEMPTS,
    ) -> LocalQuotationStorage:
        """Construct the local adapter from a configured ID generator."""
        return cls(
            generator,
            root=root,
            max_reservation_attempts=max_reservation_attempts,
        )

    def reserve(self, quotation_id: str | None = None) -> QuotationReservation:
        """Claim an exact ID or generate unique IDs with bounded retries."""
        identifiers: Iterable[str]
        if quotation_id is not None:
            identifiers = (_validate_quotation_id(quotation_id),)
        else:
            if self._id_factory is None:
                raise ValueError("quotation_id is required when no ID factory is configured")
            identifiers = (
                _validate_quotation_id(self._id_factory())
                for _ in range(self._max_reservation_attempts)
            )

        for candidate_id in identifiers:
            quotation_id = candidate_id
            final_path = self._path_for(quotation_id, _PDF_SUFFIX)
            reservation_path = self._path_for(quotation_id, _RESERVATION_SUFFIX)
            token = secrets.token_hex(16)

            try:
                descriptor = _exclusive_open(reservation_path)
            except FileExistsError:
                continue
            except OSError as error:
                raise _storage_error("reserve", error) from error

            try:
                with os.fdopen(descriptor, "wb") as reservation_file:
                    reservation_file.write(token.encode("ascii"))
                    reservation_file.flush()
                    os.fsync(reservation_file.fileno())
                if final_path.exists() or final_path.is_symlink():
                    reservation_path.unlink(missing_ok=True)
                    continue
            except BaseException:
                reservation_path.unlink(missing_ok=True)
                raise
            return QuotationReservation(quotation_id=quotation_id, _token=token)

        raise QuotationReservationExhaustedError(
            "Unable to reserve a unique quotation identifier within retry limit"
        )

    def write(
        self,
        reservation: QuotationReservation,
        pdf_content: bytes,
    ) -> StoredQuotationArtifact:
        """Write to a private temporary file and atomically link the final PDF."""
        if not isinstance(reservation, QuotationReservation):
            raise TypeError("reservation must be created by quotation storage")
        _validate_pdf(pdf_content)
        quotation_id = _validate_quotation_id(reservation.quotation_id)
        reservation_path = self._path_for(quotation_id, _RESERVATION_SUFFIX)
        final_path = self._path_for(quotation_id, _PDF_SUFFIX)
        self._verify_reservation(reservation, reservation_path)

        temporary_path = self._path_for(
            quotation_id,
            f".{reservation._token}.tmp",
        )
        try:
            descriptor = _exclusive_open(temporary_path)
            with os.fdopen(descriptor, "wb") as temporary_file:
                temporary_file.write(pdf_content)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            # A hard link publishes the complete inode atomically and fails if
            # the destination already exists. Unlike replace(), it cannot
            # overwrite a quotation created by another process.
            os.link(temporary_path, final_path, follow_symlinks=False)
            temporary_path.unlink()
            reservation_path.unlink()
        except BaseException as error:
            temporary_path.unlink(missing_ok=True)
            if isinstance(error, QuotationStorageError):
                raise
            if isinstance(error, FileExistsError):
                raise QuotationStorageError(
                    "Final quotation artifact already exists and was not overwritten",
                    cause=error,
                ) from error
            if isinstance(error, OSError):
                raise _storage_error("write", error) from error
            raise

        artifact = StoredQuotationArtifact(quotation_id=quotation_id, path=final_path)
        if not self.exists(artifact):
            raise QuotationStorageError("Finalized quotation artifact is invalid")
        return artifact

    def read(self, artifact: StoredQuotationArtifact) -> bytes:
        """Read an artifact only when its backend reference maps inside the root."""
        path = self._validate_artifact_reference(artifact)
        try:
            content = _read_regular_file(path)
        except OSError as error:
            raise _storage_error("read", error) from error
        _validate_pdf(content)
        return content

    def exists(self, artifact: StoredQuotationArtifact) -> bool:
        """Check existence and PDF completeness without accepting symlinks."""
        path = self._validate_artifact_reference(artifact)
        if not path.exists():
            return False
        try:
            _validate_pdf(_read_regular_file(path))
        except OSError as error:
            raise _storage_error("check", error) from error
        except QuotationStorageError:
            return False
        return True

    def cancel(self, reservation: QuotationReservation) -> None:
        """Remove only a matching unfinished reservation."""
        if not isinstance(reservation, QuotationReservation):
            raise TypeError("reservation must be created by quotation storage")
        quotation_id = _validate_quotation_id(reservation.quotation_id)
        reservation_path = self._path_for(quotation_id, _RESERVATION_SUFFIX)
        if not reservation_path.exists():
            return
        self._verify_reservation(reservation, reservation_path)
        try:
            reservation_path.unlink()
        except OSError as error:
            raise _storage_error("cancel reservation", error) from error

    def _verify_reservation(
        self,
        reservation: QuotationReservation,
        reservation_path: Path,
    ) -> None:
        self._assert_safe_path(reservation_path)
        if not _RESERVATION_TOKEN_PATTERN.fullmatch(reservation._token):
            raise QuotationStorageError("Quotation reservation token is invalid")
        try:
            stored_token = _read_regular_file(reservation_path).decode("ascii")
        except OSError as error:
            raise _storage_error("validate reservation", error) from error
        except UnicodeError as error:
            raise QuotationStorageError(
                "Quotation reservation token is invalid",
                cause=error,
            ) from error
        if not secrets.compare_digest(stored_token, reservation._token):
            raise QuotationStorageError("Quotation reservation token does not match")

    def _validate_artifact_reference(self, artifact: StoredQuotationArtifact) -> Path:
        if not isinstance(artifact, StoredQuotationArtifact):
            raise TypeError("artifact must be a backend-created quotation reference")
        quotation_id = _validate_quotation_id(artifact.quotation_id)
        expected_path = self._path_for(quotation_id, _PDF_SUFFIX)
        if artifact.path != expected_path:
            raise UnsafeQuotationPathError("Artifact path does not match its quotation ID")
        self._assert_safe_path(expected_path)
        return expected_path

    def _path_for(self, quotation_id: str, suffix: str) -> Path:
        candidate = self._root / f"{quotation_id}{suffix}"
        self._assert_safe_path(candidate)
        return candidate

    def _assert_safe_path(self, candidate: Path) -> None:
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self._root)
        except (OSError, ValueError) as error:
            raise UnsafeQuotationPathError(
                "Quotation artifact path resolves outside the configured storage directory",
                cause=error,
            ) from error


def _exclusive_open(path: Path) -> int:
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags, 0o600)


def _read_regular_file(path: Path) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UnsafeQuotationPathError("Quotation artifact is not a regular file")
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            return source.read()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_quotation_id(quotation_id: str) -> str:
    if not isinstance(quotation_id, str) or not _QUOTATION_ID_PATTERN.fullmatch(quotation_id):
        raise ValueError("quotation ID is not a backend-generated safe identifier")
    return quotation_id


def _validate_pdf(content: bytes) -> None:
    if not isinstance(content, bytes):
        raise TypeError("pdf_content must be bytes")
    if len(content) < len(_MINIMUM_PDF) or not content.startswith(b"%PDF-"):
        raise QuotationStorageError("Quotation artifact is not a complete PDF")
    if not content.rstrip().endswith(b"%%EOF"):
        raise QuotationStorageError("Quotation artifact is not a complete PDF")


def _storage_error(operation: str, error: OSError) -> QuotationStorageError:
    return QuotationStorageError(
        f"Quotation artifact {operation} failed: {type(error).__name__}",
        cause=error,
    )


__all__ = [
    "LocalQuotationStorage",
    "QuotationArtifactStorage",
    "QuotationReservation",
    "QuotationReservationExhaustedError",
    "QuotationStorageError",
    "StoredQuotationArtifact",
    "UnsafeQuotationPathError",
]
