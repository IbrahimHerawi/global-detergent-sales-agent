"""Tests for atomic, path-safe local quotation artifact storage."""

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.services.quotation_storage import (
    LocalQuotationStorage,
    QuotationArtifactStorage,
    QuotationReservation,
    QuotationReservationExhaustedError,
    QuotationStorageError,
    StoredQuotationArtifact,
    UnsafeQuotationPathError,
)

VALID_PDF = b"%PDF-1.7\nsynthetic test document\n%%EOF\n"


class SequenceIdFactory:
    """Thread-safe deterministic IDs for collisions and concurrency tests."""

    def __init__(self, identifiers: list[str]) -> None:
        self._identifiers = iter(identifiers)
        self._lock = threading.Lock()

    def __call__(self) -> str:
        with self._lock:
            return next(self._identifiers)


def artifact_for(root: Path, quotation_id: str) -> StoredQuotationArtifact:
    return StoredQuotationArtifact(
        quotation_id=quotation_id,
        path=root.resolve() / f"{quotation_id}.pdf",
    )


def test_local_adapter_satisfies_storage_interface_and_round_trips_pdf(
    tmp_path: Path,
) -> None:
    storage = LocalQuotationStorage(
        lambda: "GDF-Q-20260919-A82F",
        root=tmp_path,
    )

    assert isinstance(storage, QuotationArtifactStorage)
    reservation = storage.reserve()
    artifact = storage.write(reservation, VALID_PDF)

    assert reservation.quotation_id == "GDF-Q-20260919-A82F"
    assert artifact.path == tmp_path.resolve() / "GDF-Q-20260919-A82F.pdf"
    assert storage.exists(artifact) is True
    assert storage.read(artifact) == VALID_PDF
    assert not list(tmp_path.glob("*.reserve"))
    assert not list(tmp_path.glob("*.tmp"))


def test_forced_collision_generates_another_id_without_overwrite(tmp_path: Path) -> None:
    first_id = "GDF-Q-20260919-AAAA"
    second_id = "GDF-Q-20260919-BBBB"
    factory = SequenceIdFactory([first_id, first_id, second_id])
    storage = LocalQuotationStorage(factory, root=tmp_path, max_reservation_attempts=3)
    first_artifact = storage.write(storage.reserve(), VALID_PDF)

    second_reservation = storage.reserve()

    assert first_artifact.quotation_id == first_id
    assert second_reservation.quotation_id == second_id
    assert storage.read(first_artifact) == VALID_PDF


def test_collision_retries_are_bounded(tmp_path: Path) -> None:
    quotation_id = "GDF-Q-20260919-AAAA"
    storage = LocalQuotationStorage(
        lambda: quotation_id,
        root=tmp_path,
        max_reservation_attempts=2,
    )
    storage.write(storage.reserve(), VALID_PDF)

    with pytest.raises(QuotationReservationExhaustedError):
        storage.reserve()


def test_concurrent_reservations_are_unique_and_leave_exclusive_claims(
    tmp_path: Path,
) -> None:
    workers = 8
    shared_id = "GDF-Q-20260919-CAFE"
    identifiers = [shared_id] * workers + [
        f"GDF-Q-20260919-{value:04X}" for value in range(workers - 1)
    ]
    storage = LocalQuotationStorage(
        SequenceIdFactory(identifiers),
        root=tmp_path,
        max_reservation_attempts=workers + 1,
    )

    with ThreadPoolExecutor(max_workers=workers) as executor:
        reservations = list(executor.map(lambda _: storage.reserve(), range(workers)))

    ids = {reservation.quotation_id for reservation in reservations}
    assert len(ids) == workers
    assert shared_id in ids
    assert len(list(tmp_path.glob("*.reserve"))) == workers


@pytest.mark.parametrize(
    "quotation_id",
    [
        "../../outside",
        "GDF-Q-20260919-A82F/../../outside",
        "GDF-Q-20260919-A82F.pdf",
        "GDF-Q-20260919-a82f",
    ],
)
def test_traversal_and_noncanonical_identifiers_are_rejected(
    tmp_path: Path,
    quotation_id: str,
) -> None:
    storage = LocalQuotationStorage(lambda: quotation_id, root=tmp_path)

    with pytest.raises(ValueError, match="backend-generated"):
        storage.reserve()

    forged_artifact = StoredQuotationArtifact(
        quotation_id=quotation_id,
        path=tmp_path / quotation_id,
    )
    with pytest.raises(ValueError, match="backend-generated"):
        storage.exists(forged_artifact)


def test_artifact_path_must_be_derived_from_backend_identifier(tmp_path: Path) -> None:
    quotation_id = "GDF-Q-20260919-A82F"
    storage = LocalQuotationStorage(lambda: quotation_id, root=tmp_path)
    forged_artifact = StoredQuotationArtifact(
        quotation_id=quotation_id,
        path=tmp_path / "customer-name.pdf",
    )

    with pytest.raises(UnsafeQuotationPathError):
        storage.exists(forged_artifact)


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    storage_root = tmp_path / "quotes"
    outside_root = tmp_path / "outside"
    storage_root.mkdir()
    outside_root.mkdir()
    quotation_id = "GDF-Q-20260919-A82F"
    outside_pdf = outside_root / "outside.pdf"
    outside_pdf.write_bytes(VALID_PDF)
    linked_path = storage_root / f"{quotation_id}.pdf"
    linked_path.symlink_to(outside_pdf)
    storage = LocalQuotationStorage(lambda: quotation_id, root=storage_root)

    with pytest.raises(UnsafeQuotationPathError):
        storage.read(artifact_for(storage_root, quotation_id))

    assert outside_pdf.read_bytes() == VALID_PDF


def test_existing_final_file_is_never_overwritten(tmp_path: Path) -> None:
    quotation_id = "GDF-Q-20260919-A82F"
    storage = LocalQuotationStorage(lambda: quotation_id, root=tmp_path)
    reservation = storage.reserve()
    final_path = tmp_path / f"{quotation_id}.pdf"
    original_content = b"%PDF-1.4\nexisting\n%%EOF\n"
    final_path.write_bytes(original_content)

    with pytest.raises(QuotationStorageError) as raised:
        storage.write(reservation, VALID_PDF)

    assert raised.value.diagnostic_detail is not None
    assert "not overwritten" in raised.value.diagnostic_detail
    assert final_path.read_bytes() == original_content


def test_interrupted_write_leaves_no_final_or_partial_pdf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    quotation_id = "GDF-Q-20260919-A82F"
    storage = LocalQuotationStorage(lambda: quotation_id, root=tmp_path)
    reservation = storage.reserve()

    def interrupted_link(
        source: os.PathLike[str] | str,
        destination: os.PathLike[str] | str,
        *,
        follow_symlinks: bool = True,
    ) -> None:
        del source, destination, follow_symlinks
        raise OSError("simulated interruption")

    monkeypatch.setattr(os, "link", interrupted_link)

    with pytest.raises(QuotationStorageError) as raised:
        storage.write(reservation, VALID_PDF)

    assert raised.value.diagnostic_detail is not None
    assert "write failed" in raised.value.diagnostic_detail
    assert not (tmp_path / f"{quotation_id}.pdf").exists()
    assert not list(tmp_path.glob("*.tmp"))
    assert (tmp_path / f"{quotation_id}.reserve").exists()


def test_invalid_or_partial_pdf_is_never_finalized(tmp_path: Path) -> None:
    quotation_id = "GDF-Q-20260919-A82F"
    storage = LocalQuotationStorage(lambda: quotation_id, root=tmp_path)
    reservation = storage.reserve()

    with pytest.raises(QuotationStorageError) as raised:
        storage.write(reservation, b"%PDF-1.7\npartial")

    assert raised.value.diagnostic_detail is not None
    assert "complete PDF" in raised.value.diagnostic_detail
    assert not (tmp_path / f"{quotation_id}.pdf").exists()
    assert not list(tmp_path.glob("*.tmp"))


def test_forged_reservation_token_cannot_write(tmp_path: Path) -> None:
    quotation_id = "GDF-Q-20260919-A82F"
    storage = LocalQuotationStorage(lambda: quotation_id, root=tmp_path)
    storage.reserve()
    forged = QuotationReservation(quotation_id=quotation_id, _token="f" * 32)

    with pytest.raises(QuotationStorageError) as raised:
        storage.write(forged, VALID_PDF)

    assert raised.value.diagnostic_detail is not None
    assert "token does not match" in raised.value.diagnostic_detail
    assert not (tmp_path / f"{quotation_id}.pdf").exists()
