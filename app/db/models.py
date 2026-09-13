from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


class SearchRequestRecord(Base):
    """One row per /search/hotels call — 'search requests' from the spec."""
    __tablename__ = "search_requests"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    destination: Mapped[str] = mapped_column(String)
    check_in: Mapped[str] = mapped_column(String)
    check_out: Mapped[str] = mapped_column(String)
    guests: Mapped[int] = mapped_column(Integer)
    rooms: Mapped[int] = mapped_column(Integer)
    suppliers_queried: Mapped[list] = mapped_column(JSON, default=list)
    suppliers_failed: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    offers: Mapped[list["OfferRecord"]] = relationship(back_populates="search_request")


class OfferRecord(Base):
    """A normalized offer as returned to the client for a given search —
    the 'normalized offers' + implicitly 'supplier references' from the
    spec (supplier_id + supplier_offer_ref ARE the supplier reference)."""
    __tablename__ = "offers"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    search_request_id: Mapped[str] = mapped_column(ForeignKey("search_requests.id"))
    supplier_id: Mapped[str] = mapped_column(String)
    supplier_offer_ref: Mapped[str] = mapped_column(String)
    property_id: Mapped[str] = mapped_column(String)
    property_name: Mapped[str] = mapped_column(String)
    room_type: Mapped[str] = mapped_column(String)
    currency: Mapped[str] = mapped_column(String)
    total_price: Mapped[float] = mapped_column(Numeric(10, 2))
    raw_offer_json: Mapped[dict] = mapped_column(JSON)  # full Offer, so a booking can trace back to exactly what was quoted
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    search_request: Mapped["SearchRequestRecord"] = relationship(back_populates="offers")


class BookingRecord(Base):
    __tablename__ = "bookings"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    workflow_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    offer_id: Mapped[str] = mapped_column(String)
    client_reference: Mapped[str] = mapped_column(String, unique=True, index=True)
    supplier_id: Mapped[str] = mapped_column(String)
    supplier_booking_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    status_history: Mapped[list["BookingStatusHistory"]] = relationship(back_populates="booking")


class BookingStatusHistory(Base):
    """Every status transition a booking goes through — 'booking status
    history' from the spec. `reason` also doubles as the 'failure or retry
    information' field (e.g. 'price_drift_exceeded', 'compensation_failed',
    'supplier_confirmation_timeout') — kept in the same table rather than
    a separate one since a failure/retry IS a status transition."""
    __tablename__ = "booking_status_history"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    booking_id: Mapped[str] = mapped_column(ForeignKey("bookings.id"))
    status: Mapped[str] = mapped_column(String)
    reason: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    booking: Mapped["BookingRecord"] = relationship(back_populates="status_history")