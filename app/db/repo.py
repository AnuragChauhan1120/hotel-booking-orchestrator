from __future__ import annotations

import logging
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, joinedload, sessionmaker

from app.db.models import Base, BookingRecord, BookingStatusHistory, OfferRecord, SearchRequestRecord

logger = logging.getLogger("repo")

_engine = create_engine("sqlite:///./travel_platform.db", echo=False)
# expire_on_commit=False: callers use returned rows AFTER the session that
# fetched them has closed (see session_scope below), so attributes must
# not be invalidated on commit.
_SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(_engine)


@contextmanager
def session_scope():
    session: Session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ---- search / offers -------------------------------------------------

def save_search(request, suppliers_queried: list[str], suppliers_failed: list[str], offers: list,
                 correlation_id: str | None = None) -> str:
    """Persists the search request and every normalized offer returned for
    it. Returns the search_request id."""
    with session_scope() as db:
        rec = SearchRequestRecord(
            destination=request.destination,
            check_in=request.check_in.isoformat(),
            check_out=request.check_out.isoformat(),
            guests=request.guests,
            rooms=request.rooms,
            suppliers_queried=suppliers_queried,
            suppliers_failed=suppliers_failed,
        )
        db.add(rec)
        db.flush()  # get rec.id before commit

        for offer in offers:
            db.add(
                OfferRecord(
                    search_request_id=rec.id,
                    supplier_id=offer.supplier_id,
                    supplier_offer_ref=offer.supplier_offer_ref,
                    property_id=offer.property_id,
                    property_name=offer.property_name,
                    room_type=offer.room_type,
                    currency=offer.currency,
                    total_price=offer.total_price,
                    raw_offer_json=offer.model_dump(mode="json"),
                )
            )
        logger.info(
            "search persisted",
            extra={"correlation_id": correlation_id, "search_request_id": rec.id, "offer_count": len(offers)},
        )
        return rec.id


def get_offers_for_search(search_request_id: str) -> list[OfferRecord]:
    with session_scope() as db:
        return db.query(OfferRecord).filter_by(search_request_id=search_request_id).all()


def get_offer(offer_id: str) -> OfferRecord | None:
    with session_scope() as db:
        return db.get(OfferRecord, offer_id)


# ---- bookings ----------------------------------------------------------

def create_booking(*, correlation_id: str, offer_id: str, client_reference: str, supplier_id: str) -> str:
    """Idempotent: same client_reference always returns the same booking id.
    `correlation_id` is the Temporal workflow_id — stored so every DB write
    for one booking can be traced back to the job that made it."""
    with session_scope() as db:
        existing = db.query(BookingRecord).filter_by(client_reference=client_reference).first()
        if existing:
            logger.info(
                "booking already exists for client_reference, reusing",
                extra={"correlation_id": correlation_id, "booking_id": existing.id},
            )
            return existing.id

        booking = BookingRecord(
            workflow_id=correlation_id,
            offer_id=offer_id,
            client_reference=client_reference,
            supplier_id=supplier_id,
            status="pending",
        )
        db.add(booking)
        db.flush()
        db.add(BookingStatusHistory(booking_id=booking.id, status="pending", reason="workflow started"))
        logger.info(
            "new booking row created",
            extra={"correlation_id": correlation_id, "booking_id": booking.id, "supplier": supplier_id},
        )
        return booking.id


def update_booking_status(*, correlation_id: str, booking_id: str, status: str, reason: str | None = None,
                           supplier_booking_ref: str | None = None) -> None:
    with session_scope() as db:
        booking = db.get(BookingRecord, booking_id)
        if booking is None:
            logger.error(
                "update_booking_status: booking not found",
                extra={"correlation_id": correlation_id, "booking_id": booking_id},
            )
            raise ValueError(f"booking {booking_id} not found")
        booking.status = status
        if supplier_booking_ref:
            booking.supplier_booking_ref = supplier_booking_ref
        db.add(BookingStatusHistory(booking_id=booking_id, status=status, reason=reason))


def get_booking(booking_id: str) -> BookingRecord | None:
    with session_scope() as db:
        return (
            db.query(BookingRecord)
            .options(joinedload(BookingRecord.status_history))
            .filter_by(id=booking_id)
            .first()
        )


def find_booking_by_client_reference(client_reference: str) -> BookingRecord | None:
    with session_scope() as db:
        return db.query(BookingRecord).filter_by(client_reference=client_reference).first()