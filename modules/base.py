"""
campaigns/base.py — Arc Fleet Abstract Campaign Base

GoF Patterns:
  - Template Method:  run flow defined here; subclasses fill in content
  - Abstract Factory: subclass provides vehicle_info, messages, contacts

SOLID:
  - SRP: campaign is a config object — modules do the sending
  - OCP: add new campaigns without touching modules
  - DIP: modules depend on BaseCampaign ABC, not concrete campaigns

All campaigns:
  - Inherit BaseCampaign
  - Define SLUG (short CLI id), CAMPAIGN_ID (full id), vehicle_info, messages, contacts
  - Register automatically via CampaignRegistry
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional, List, Dict
from abc import ABC, abstractmethod


# ── Shared sender identity ────────────────────────────────────────────────────
# Single source of truth — all campaigns reference this
SENDER: Dict = {
    "name":          "Kenyon Jones",
    "email":         "kjonesmle@gmail.com",
    "phone":         "7018705235",
    "phone_display": "(701) 870-5235",
    "zip":           "58523",
    "alt_name":      "Cynthia Ennis",
    "alt_phone":     "7019465731",
    "alt_phone_display": "(701) 946-5731",
}

# Contact lifecycle states
STATUS_PENDING   = "PENDING"
STATUS_SENT      = "SENT"
STATUS_FILLED    = "FILLED"
STATUS_SUBMITTED = "SUBMITTED"
STATUS_REPLIED   = "REPLIED"
STATUS_FAILED    = "FAILED"
STATUS_SKIP      = "SKIP"


# ── Contact dataclass ─────────────────────────────────────────────────────────

@dataclass
class Contact:
    """
    Single outreach target. Immutable identity, mutable status.

    Fields:
        name:   Display name (required)
        email:  Email address — used for 'email' method
        phone:  10-digit string — used for 'phone' / 'sms' method
        url:    Contact form URL — used for 'form' method
        tier:   Message tier key (maps to campaign.messages)
        method: 'email' | 'form' | 'phone' | 'sms'
        status: Lifecycle state (see STATUS_* constants)
        notes:  Internal operator notes
    """
    name:   str
    email:  Optional[str] = None
    phone:  Optional[str] = None
    url:    Optional[str] = None
    tier:   str           = "DEFAULT"
    method: str           = "email"
    status: str           = STATUS_PENDING
    notes:  str           = ""

    def is_reachable(self) -> bool:
        return bool(self.email or self.phone or self.url)

    def is_pending(self) -> bool:
        return self.status == STATUS_PENDING

    def channels(self) -> List[str]:
        out = []
        if self.email: out.append(f"email:{self.email}")
        if self.phone: out.append(f"phone:{self.phone}")
        if self.url:   out.append(f"form:{self.url[:40]}")
        return out

    def to_dict(self) -> Dict:
        return {k: v for k, v in self.__dataclass_fields__.items()
                if hasattr(self, k)}

    def __repr__(self):
        return (f"Contact({self.name!r} [{self.tier}/{self.method}] "
                f"status={self.status})")


# ── Abstract Campaign ─────────────────────────────────────────────────────────

class BaseCampaign(ABC):
    """
    Abstract base for all vehicle campaigns.
    GoF: Template Method — defines the structure; subclasses provide content.

    Each campaign is a self-contained config object.
    Modules (emailer, formfill, sms, phone) receive a campaign object
    and execute outreach against its contact list.
    """

    # Override in every subclass
    SLUG:        str = "base"          # CLI id: mkz, towncar, f350, jayco
    CAMPAIGN_ID: str = "BASE"          # Full id: MKZ_2016_HYBRID
    VERSION:     str = "1.0"

    SENDER: Dict = SENDER              # Shared across all campaigns

    # ── Abstract interface (must implement) ───────────────────────────────────

    @property
    @abstractmethod
    def vehicle_info(self) -> Dict:
        """
        Vehicle metadata. Recommended keys:
            display, vin, mileage, color, trim, title,
            location, asking, note, alert (optional)
        """
        ...

    @property
    @abstractmethod
    def messages(self) -> Dict[str, str]:
        """
        Tier-keyed message bodies. Must include 'DEFAULT'.
        Keys must match Contact.tier values used in contacts list.
        """
        ...

    @property
    @abstractmethod
    def contacts(self) -> List[Contact]:
        """
        Ordered contact list. Priority contacts first.
        Modules iterate this list in order.
        """
        ...

    # ── Concrete helpers (don't override unless needed) ───────────────────────

    @property
    def priority_contacts(self) -> List[Contact]:
        """High-priority contacts to process first. Override in subclass."""
        return []

    def get_message(self, tier: str) -> str:
        """Return message for tier; falls back to DEFAULT."""
        msgs = self.messages
        return msgs.get(tier, msgs.get("DEFAULT", ""))

    def get_subject(self, contact: Contact, prefix: str = "") -> str:
        """Build email subject line."""
        vehicle = self.vehicle_info.get("display", "Vehicle")
        return f"{prefix}Vehicle for Sale — {vehicle} | {self.SENDER['name']}"

    def pending_contacts(self, method: str = None) -> List[Contact]:
        """Return PENDING contacts, optionally filtered by method."""
        result = [c for c in self.contacts if c.is_pending()]
        if method:
            result = [c for c in result if c.method == method]
        return result

    def contacts_by_method(self) -> Dict[str, List[Contact]]:
        out: Dict[str, List[Contact]] = {}
        for c in self.contacts:
            out.setdefault(c.method, []).append(c)
        return out

    def contacts_by_tier(self) -> Dict[str, List[Contact]]:
        out: Dict[str, List[Contact]] = {}
        for c in self.contacts:
            out.setdefault(c.tier, []).append(c)
        return out

    def reset_pending(self):
        """Reset all contacts to PENDING. Use for test re-runs."""
        for c in self.contacts:
            c.status = STATUS_PENDING

    def summary(self) -> Dict:
        """Return summary dict for dashboards and CLI output."""
        all_c    = self.contacts
        info     = self.vehicle_info
        pending  = sum(1 for c in all_c if c.status == STATUS_PENDING)
        sent     = sum(1 for c in all_c if c.status in (STATUS_SENT, STATUS_FILLED, STATUS_SUBMITTED))
        replied  = sum(1 for c in all_c if c.status == STATUS_REPLIED)
        failed   = sum(1 for c in all_c if c.status == STATUS_FAILED)

        return {
            "slug":           self.SLUG,
            "campaign_id":    self.CAMPAIGN_ID,
            "vehicle":        info.get("display", self.CAMPAIGN_ID),
            "vin":            info.get("vin", ""),
            "location":       info.get("location", ""),
            "asking":         info.get("asking", ""),
            "title":          info.get("title", ""),
            "total_contacts": len(all_c),
            "pending":        pending,
            "sent":           sent,
            "replied":        replied,
            "failed":         failed,
            "alert":          info.get("alert"),
            "note":           info.get("note"),
            "version":        self.VERSION,
        }

    def __repr__(self):
        s = self.summary()
        return (f"<{self.__class__.__name__} id={self.CAMPAIGN_ID} "
                f"contacts={s['total_contacts']} pending={s['pending']}>")
