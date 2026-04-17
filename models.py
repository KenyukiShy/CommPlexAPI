"""
CommPlexAPI/models.py — SQLAlchemy ORM Models
Domain: CommPlexAPI (The Mouth)

Tables:
    leads   — Inbound dealer offers, qualified/rejected/pending

GoF Patterns:
    - Repository: get_db() session factory (used via FastAPI Depends)
"""

from __future__ import annotations
import os
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    DateTime, Text, Enum
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# ── Database Config ───────────────────────────────────────────────────────────

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./commplex_leads.db"   # Default: local SQLite (zero-config)
)

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


# ── Enums ─────────────────────────────────────────────────────────────────────

class LeadStatus(str, PyEnum):
    PENDING       = "PENDING"
    QUALIFIED     = "QUALIFIED"
    REJECTED      = "REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"


# ── Lead Model ────────────────────────────────────────────────────────────────

class Lead(Base):
    """
    Persisted inbound dealer offer.

    Lifecycle:
        PENDING → [SluiceEngine] → QUALIFIED | REJECTED | MANUAL_REVIEW
        QUALIFIED → [Human] → closed deal or back to PENDING
    """
    __tablename__ = "leads"

    id             = Column(Integer, primary_key=True, index=True)
    dealer_name    = Column(String(200), nullable=False, index=True)
    dealer_phone   = Column(String(20),  nullable=True)
    price          = Column(Float,       nullable=True)
    vehicle_year   = Column(Integer,     nullable=True)
    status         = Column(
        Enum(LeadStatus),
        default=LeadStatus.PENDING,
        nullable=False,
        index=True,
    )
    campaign_id    = Column(String(50),  default="mkz", index=True)
    raw_transcript = Column(Text,        nullable=True)
    notes          = Column(Text,        nullable=True)
    created_at     = Column(DateTime,    default=datetime.utcnow,  nullable=False)
    updated_at     = Column(DateTime,    default=datetime.utcnow,
                            onupdate=datetime.utcnow, nullable=False)

    def __repr__(self):
        return f"<Lead #{self.id} {self.dealer_name} ${self.price} [{self.status}]>"

    def to_dict(self):
        return {
            "id":             self.id,
            "dealer_name":    self.dealer_name,
            "dealer_phone":   self.dealer_phone,
            "price":          self.price,
            "vehicle_year":   self.vehicle_year,
            "status":         self.status.value if self.status else None,
            "campaign_id":    self.campaign_id,
            "notes":          self.notes,
            "created_at":     self.created_at.isoformat() if self.created_at else None,
            "updated_at":     self.updated_at.isoformat() if self.updated_at else None,
        }


# ── DB Lifecycle ──────────────────────────────────────────────────────────────

def init_db():
    """Create all tables. Safe to call multiple times (idempotent)."""
    Base.metadata.create_all(bind=engine)


def get_db() -> Session:
    """FastAPI dependency — yields a DB session, closes after request."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Initializing CommPlexAPI database...")
    init_db()
    db = SessionLocal()

    # Seed a test lead
    test_lead = Lead(
        dealer_name="Fargo North Ford",
        dealer_phone="7015551234",
        price=25000.0,
        vehicle_year=2021,
        status=LeadStatus.QUALIFIED,
        campaign_id="mkz",
        raw_transcript="I have a 2021 Lincoln MKZ for $25,000 — in great shape.",
        notes="[Test seed] Created by models.py __main__",
    )
    db.add(test_lead)
    db.commit()
    db.refresh(test_lead)

    print(f"✅ Lead created: {test_lead}")
    print(f"   ID: {test_lead.id}")
    print(f"   Status: {test_lead.status}")

    leads = db.query(Lead).all()
    print(f"\nTotal leads in DB: {len(leads)}")
    for l in leads:
        print(f"  {l}")

    db.close()
    print("\n✅ Checkpoint: models.py DB schema verified.")
