"""
modules/phone.py — Arc Fleet Phone Caller Module

GoF Patterns:
  - Proxy:     PhoneModule gates calls until backend is ACTIVE + DRY_RUN=false
  - Strategy:  Backend is swappable (Bland / Twilio / stub)
  - Observer:  Emits to CallEventBus on call complete/fail
  - Iterator:  wave_call() iterates contacts in batches (parallel fan-out)
  - Chain:     QualifierChain runs lead through qualification steps

CommConQualiPsi architecture:
  - Wave (Observer):   parallel_wave() — concurrent outbound calls
  - Sluice (Chain):    QualifierChain — filters interested leads
  - Series (Iterator): serial_queue() — queues qualified leads for human
  - Transfer (Bridge): transfer_to_human() — warm transfer to Kenyon

Setup (.env):
    BLAND_API_KEY=...            # Required for AI calls
    BLAND_PHONE_NUMBER=+1...     # Your Bland.ai number
    BLAND_TRANSFER_NUMBER=7018705235  # Transfer destination
    CALL_CONCURRENCY=5           # Max parallel calls
    DRY_RUN=true                 # Set false to actually dial
"""

from __future__ import annotations
import os
import time
import logging
import asyncio
from typing import Optional, Dict, List, Any
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from modules import ModuleBase, STATUS_STUB, STATUS_ACTIVE

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════
# CALL RESULT
# ═══════════════════════════════════════════════════════

class CallResult:
    def __init__(self, contact_name: str, to: str, call_id: str = None,
                 status: str = "pending", transcript: str = None,
                 transferred: bool = False, interested: bool = None,
                 error: str = None, dry_run: bool = False):
        self.contact_name = contact_name
        self.to           = to
        self.call_id      = call_id
        self.status       = status
        self.transcript   = transcript
        self.transferred  = transferred
        self.interested   = interested
        self.error        = error
        self.dry_run      = dry_run
        self.ts           = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return self.__dict__

    def __repr__(self):
        tag = "DRY" if self.dry_run else self.status.upper()
        return f"<CallResult [{tag}] {self.contact_name} — interested={self.interested}>"


# ═══════════════════════════════════════════════════════
# QUALIFIER CHAIN — GoF Chain of Responsibility
# ═══════════════════════════════════════════════════════

class QualifierHandler:
    """One link in the qualifier chain. GoF: Chain of Responsibility."""

    def __init__(self, next_handler: Optional["QualifierHandler"] = None):
        self._next = next_handler

    def qualify(self, result: CallResult) -> Optional[CallResult]:
        """Return result if qualified, None if rejected."""
        if self._next:
            return self._next.qualify(result)
        return result  # End of chain — passed all filters


class InterestFilter(QualifierHandler):
    """Reject calls with no answer or clear disinterest."""
    REJECT_STATUSES = {"no-answer", "busy", "failed", "voicemail"}

    def qualify(self, result: CallResult) -> Optional[CallResult]:
        if result.status in self.REJECT_STATUSES:
            logger.info(f"[Sluice] REJECTED {result.contact_name} — {result.status}")
            return None
        return super().qualify(result)


class TranscriptFilter(QualifierHandler):
    """Analyze transcript for buying signals."""
    REJECT_PHRASES = ["not interested", "no thank you", "remove me", "do not call", "never"]
    INTEREST_PHRASES = ["interested", "yes", "tell me more", "how much", "when", "condition"]

    def qualify(self, result: CallResult) -> Optional[CallResult]:
        transcript = (result.transcript or "").lower()
        for phrase in self.REJECT_PHRASES:
            if phrase in transcript:
                logger.info(f"[Sluice] REJECTED {result.contact_name} — rejection phrase detected")
                return None
        for phrase in self.INTEREST_PHRASES:
            if phrase in transcript:
                result.interested = True
                logger.info(f"[Sluice] QUALIFIED {result.contact_name} — interest detected")
                break
        return super().qualify(result)


class DuplicateFilter(QualifierHandler):
    """Reject numbers already called today."""
    def __init__(self, called_set: set, **kwargs):
        super().__init__(**kwargs)
        self._called = called_set

    def qualify(self, result: CallResult) -> Optional[CallResult]:
        if result.to in self._called:
            logger.info(f"[Sluice] REJECTED {result.contact_name} — already called")
            return None
        self._called.add(result.to)
        return super().qualify(result)


def build_qualifier_chain(called_set: set = None) -> QualifierHandler:
    """Factory: build the full qualifier chain. GoF: Factory Method."""
    called_set = called_set or set()
    return DuplicateFilter(
        called_set,
        next_handler=InterestFilter(
            next_handler=TranscriptFilter()
        )
    )


# ═══════════════════════════════════════════════════════
# PHONE MODULE — Main interface
# ═══════════════════════════════════════════════════════

class PhoneModule(ModuleBase):
    """
    Arc Fleet Phone Caller. GoF: Facade + Proxy + Observer.

    CommConQualiPsi T-shaped architecture:
        Wave     → parallel_wave(contacts)   [Observer fan-out]
        Sluice   → qualifier_chain.qualify() [Chain of Responsibility]
        Series   → serial_queue(qualified)   [Iterator]
        Transfer → transfer_to_human()       [Bridge/Mediator]
    """

    MODULE_ID = "phone"

    def __init__(self):
        self.bland_key      = os.getenv("BLAND_API_KEY", "")
        self.from_number    = os.getenv("BLAND_PHONE_NUMBER", "")
        self.transfer_to    = os.getenv("BLAND_TRANSFER_NUMBER", "7018705235")
        self.dry_run        = os.getenv("DRY_RUN", "true").lower() == "true"
        self.concurrency    = int(os.getenv("CALL_CONCURRENCY", "5"))
        self._called_set    = set()
        self._qualifier     = build_qualifier_chain(self._called_set)

        self.STATUS = STATUS_ACTIVE if (self.bland_key and self.from_number) else STATUS_STUB

        # Wire notifier as Observer
        try:
            from modules.notifier import NotifierModule
            self._notifier = NotifierModule()
            from modules.voice import CallEventBus
            self._bus = CallEventBus()
            self._bus.subscribe("completed", self._notifier.call_completed)
        except Exception:
            self._notifier = None
            self._bus      = None

    # ── Single call ──────────────────────────────────────────────────────────

    def call_one(self, contact, campaign, script: str = None) -> CallResult:
        """
        Place one outbound call via Bland.ai.
        GoF: Proxy — gates if dry_run or not ACTIVE.
        """
        to = contact.phone
        if not to:
            return CallResult(contact.name, to="(none)", status="skip", error="No phone")

        vehicle_info = campaign.vehicle_info if campaign else {}
        call_script  = script or self._build_script(contact, campaign)

        if self.dry_run:
            logger.info(f"[Phone DRY-RUN] Would call {contact.name} at {to}")
            return CallResult(contact.name, to=to, status="dry-run",
                              dry_run=True, interested=None)

        if self.STATUS != STATUS_ACTIVE:
            err = "Phone module not active. Set BLAND_API_KEY and BLAND_PHONE_NUMBER."
            return CallResult(contact.name, to=to, status="error", error=err)

        try:
            from modules.voice import VoiceModule
            voice = VoiceModule()
            result = voice.call(to, call_script, vehicle_info=vehicle_info,
                                transfer_to=self.transfer_to)
            status = result.get("status", "unknown")
            return CallResult(
                contact.name, to=to,
                call_id=result.get("call_id"),
                status=status,
                transcript=result.get("transcript", ""),
                transferred=result.get("transferred", False),
            )
        except Exception as e:
            logger.error(f"[Phone] Error calling {contact.name}: {e}")
            return CallResult(contact.name, to=to, status="error", error=str(e))

    # ── Wave: parallel fan-out (Observer pattern) ────────────────────────────

    def parallel_wave(self, contacts: List, campaign,
                      script: str = None, max_workers: int = None) -> List[CallResult]:
        """
        Fan-out: call many contacts in parallel.
        GoF: Observer — broadcasts campaign to many contacts simultaneously.
        Returns ALL results (qualified and rejected).
        """
        max_workers = max_workers or self.concurrency
        phone_contacts = [c for c in contacts
                          if c.method in ("phone", "sms") and c.phone
                          and c.status == "PENDING"]

        if not phone_contacts:
            logger.info("[Wave] No PENDING phone contacts to call.")
            return []

        logger.info(f"[Wave] Launching parallel wave: {len(phone_contacts)} contacts "
                    f"(concurrency={max_workers})")

        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {
                executor.submit(self.call_one, contact, campaign, script): contact
                for contact in phone_contacts
            }
            for future in as_completed(future_map):
                contact = future_map[future]
                try:
                    result = future.result()
                    results.append(result)
                    if self._bus:
                        self._bus.emit(result.status, result.to_dict())
                except Exception as e:
                    results.append(CallResult(
                        contact.name, to=contact.phone or "", status="error", error=str(e)
                    ))

        return results

    # ── Sluice: qualify results ──────────────────────────────────────────────

    def qualify(self, results: List[CallResult]) -> List[CallResult]:
        """
        Run all call results through the qualifier chain.
        GoF: Chain of Responsibility — rejects unqualified, passes interested.
        """
        qualified = []
        for r in results:
            passed = self._qualifier.qualify(r)
            if passed:
                qualified.append(passed)
        logger.info(f"[Sluice] {len(qualified)}/{len(results)} calls qualified")
        return qualified

    # ── Series: serialize qualified leads ───────────────────────────────────

    def serial_queue(self, qualified: List[CallResult]) -> List[CallResult]:
        """
        Sort and prepare qualified leads for human review.
        GoF: Iterator — returns ordered list.
        """
        # Prioritize: interested=True > interested=None > transferred
        def sort_key(r: CallResult):
            return (0 if r.interested else 1, r.ts)

        return sorted(qualified, key=sort_key)

    # ── Full CommConQualiPsi pipeline ────────────────────────────────────────

    def run_pipeline(self, contacts: List, campaign) -> Dict[str, Any]:
        """
        Full T-shaped pipeline:
            Wave → Sluice → Series → (notify human)
        """
        logger.info("[Pipeline] Starting CommConQualiPsi call pipeline ...")

        # 1. Wave: parallel outbound
        all_results = self.parallel_wave(contacts, campaign)

        # 2. Sluice: qualify
        qualified = self.qualify(all_results)

        # 3. Series: sort for human
        ordered = self.serial_queue(qualified)

        # 4. Notify team
        if self._notifier and ordered:
            self._notifier.alert_team(
                "Qualified Leads Ready",
                f"{len(ordered)} interested leads from wave of {len(all_results)}. "
                "Check dashboard for call queue.",
            )

        summary = {
            "wave_total":   len(all_results),
            "qualified":    len(qualified),
            "ordered_queue": [r.to_dict() for r in ordered],
            "timestamp":    datetime.now().isoformat(),
        }
        logger.info(f"[Pipeline] Complete: {summary['qualified']}/{summary['wave_total']} qualified")
        return summary

    def _build_script(self, contact, campaign) -> str:
        """Build a personalized call script for this contact/campaign."""
        info = campaign.vehicle_info if campaign else {}
        vehicle = info.get("display", "vehicle")
        asking  = info.get("asking", "")
        location = info.get("location", "")
        return (
            f"You are a professional outreach agent calling {contact.name} "
            f"on behalf of Kenyon Jones (701-870-5235). "
            f"You are calling to inquire if {contact.name} would be interested in "
            f"a {vehicle} for sale{' at ' + location if location else ''}. "
            f"{('Asking price is ' + asking + '. ') if asking else ''}"
            f"If they express interest, offer to transfer them to Kenyon directly. "
            f"Be professional, brief, and respectful. If not interested, thank them and end the call."
        )

    def health_check(self) -> Dict:
        return {
            "module":         self.MODULE_ID,
            "status":         self.STATUS,
            "active":         self.STATUS == STATUS_ACTIVE,
            "dry_run":        self.dry_run,
            "concurrency":    self.concurrency,
            "bland_key_set":  bool(self.bland_key),
            "from_number":    self.from_number or "(not set)",
            "transfer_to":    self.transfer_to,
        }
