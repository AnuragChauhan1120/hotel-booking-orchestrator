import uuid

import pytest

from app.db import repo
from app.db.models import Base


@pytest.fixture(autouse=True)
def isolated_db(monkeypatch, tmp_path):
    """Point repo at a fresh throwaway sqlite file per test so tests don't
    share state or depend on run order."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    db_path = tmp_path / f"{uuid.uuid4().hex}.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(repo, "_engine", engine)
    monkeypatch.setattr(repo, "_SessionLocal", sessionmaker(bind=engine, expire_on_commit=False))
    yield


def test_duplicate_booking_requests_create_only_one_row():
    client_reference = "idem-key-123"

    id_1 = repo.create_booking(
        correlation_id="wf-1", offer_id="offer-1", client_reference=client_reference, supplier_id="atlas"
    )
    id_2 = repo.create_booking(
        correlation_id="wf-1", offer_id="offer-1", client_reference=client_reference, supplier_id="atlas"
    )

    assert id_1 == id_2  # same idempotency key -> same booking row, not a second one


def test_status_update_records_history():
    booking_id = repo.create_booking(
        correlation_id="wf-2", offer_id="offer-2", client_reference="key-2", supplier_id="nova"
    )
    repo.update_booking_status(correlation_id="wf-2", booking_id=booking_id, status="confirmed", reason="supplier confirmed")

    booking = repo.get_booking(booking_id)
    assert booking.status == "confirmed"
    assert len(booking.status_history) == 2  # initial "pending" + this update