"""
CommPlexAPI/server/main.py — Arc Badlands CommPlex FastAPI Gateway
Domain: The Mouth — receives webhooks, routes to Core, persists leads.

Refactored from legacy Flask app.py + arc_server.py.

Endpoints:
    GET  /health                    — Health check + domain status
    POST /webhook/bland             — Receive Bland.ai call transcript
    POST /webhook/email             — Receive Purelymail forwarded email
    GET  /leads                     — List all leads (filterable by status)
    GET  /leads/{lead_id}           — Lead detail
    PATCH /leads/{lead_id}/status   — Manually update lead status
    GET  /campaigns                 — List campaigns and contact summaries
    POST /campaigns/{slug}/run      — Trigger campaign module (dry-run safe)

GoF Patterns:
    - Facade:    Single entry point for all inbound webhook traffic
    - Proxy:     dry_run flag gates all destructive actions
    - Observer:  Notifier fires on Qualified status transition

Domain Rules:
    - No business logic here — route to CommPlexCore for classification
    - All DB persistence via SQLAlchemy models (models.py)
    - CommPlexEdge/notifier fires on QUALIFIED status change
"""

from __future__ import annotations
import os
import sys
import logging
from typing import Optional, List
from datetime import datetime

from fastapi import FastAPI, HTTPException, Query, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy.orm import Session

# ── Local imports (domain-correct) ───────────────────────────────────────────
# NOTE: These resolve when run from the CommPlexAPI/ root directory.
# Adjust sys.path if running from a different working directory.
try:
    from models import Lead, LeadStatus, init_db, get_db
except ImportError:
    # Fallback for direct script execution / testing
    sys.path.insert(0, os.path.dirname(__file__) + "/..")
    from models import Lead, LeadStatus, init_db, get_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("CommPlexAPI")

# ── App Configuration ─────────────────────────────────────────────────────────

app = FastAPI(
    title="Arc Badlands CommPlex API",
    description="The Mouth — FastAPI gateway for telephony, webhooks, and lead management.",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Startup ───────────────────────────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    init_db()
    logger.info("CommPlexAPI — Gateway Online. DB initialized.")


# ── Pydantic Schemas ──────────────────────────────────────────────────────────

class BlandWebhookPayload(BaseModel):
    """Payload from Bland.ai call completion webhook."""
    call_id:      str
    status:       str                 # "completed" | "voicemail" | "failed"
    transcript:   Optional[str] = ""
    dealer_name:  Optional[str] = ""
    dealer_phone: Optional[str] = ""
    campaign_id:  Optional[str] = "mkz"
    metadata:     Optional[dict] = {}


class EmailWebhookPayload(BaseModel):
    """Payload from Purelymail forwarding webhook."""
    from_email:   str
    subject:      str
    body:         str
    dealer_name:  Optional[str] = ""
    dealer_phone: Optional[str] = ""
    campaign_id:  Optional[str] = "mkz"


class LeadStatusUpdate(BaseModel):
    status: LeadStatus
    notes:  Optional[str] = ""


class CampaignRunRequest(BaseModel):
    module:  str   = "email"          # email | phone | sms | formfill
    dry_run: bool  = True
    wave:    int   = 1


class LeadResponse(BaseModel):
    id:             int
    dealer_name:    str
    dealer_phone:   Optional[str]
    price:          Optional[float]
    vehicle_year:   Optional[int]
    status:         str
    campaign_id:    str
    raw_transcript: Optional[str]
    notes:          Optional[str]
    created_at:     datetime
    updated_at:     datetime

    class Config:
        from_attributes = True


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
def health():
    """Gateway health check."""
    return {
        "status":  "ok",
        "domain":  "CommPlexAPI",
        "role":    "The Mouth",
        "version": "1.0.0",
        "db":      "connected",
    }


# ── Webhook: Bland.ai ─────────────────────────────────────────────────────────

@app.post("/webhook/bland", tags=["Webhooks"])
def webhook_bland(payload: BlandWebhookPayload, db: Session = Depends(get_db)):
    """
    Receive Bland.ai call completion webhook.
    Classifies transcript via CommPlexCore SluiceEngine → persists lead.
    Fires notifier if QUALIFIED.
    """
    logger.info(f"[Bland Webhook] call_id={payload.call_id} status={payload.status}")

    if payload.status not in ("completed",):
        return {"received": True, "action": "skipped", "reason": f"Non-completed status: {payload.status}"}

    if not payload.transcript:
        return {"received": True, "action": "skipped", "reason": "No transcript"}

    # ── Classify via CommPlexCore SluiceEngine ────────────────────────────────
    lead_status, price, vehicle_year, notes = _classify_transcript(payload.transcript)

    # ── Persist lead ──────────────────────────────────────────────────────────
    lead = Lead(
        dealer_name=payload.dealer_name or f"Bland-{payload.call_id[:8]}",
        dealer_phone=payload.dealer_phone or "",
        price=price,
        vehicle_year=vehicle_year,
        status=lead_status,
        campaign_id=payload.campaign_id or "mkz",
        raw_transcript=payload.transcript[:5000],
        notes=notes,
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    logger.info(f"[Bland Webhook] Lead #{lead.id} saved → {lead_status}")

    # ── Fire notifier if QUALIFIED ────────────────────────────────────────────
    if lead_status == LeadStatus.QUALIFIED:
        _fire_qualified_alert(lead)

    return {
        "received":    True,
        "lead_id":     lead.id,
        "status":      lead_status,
        "price":       price,
        "vehicle_year": vehicle_year,
    }


# ── Webhook: Email ────────────────────────────────────────────────────────────

@app.post("/webhook/email", tags=["Webhooks"])
def webhook_email(payload: EmailWebhookPayload, db: Session = Depends(get_db)):
    """
    Receive Purelymail forwarded email webhook.
    Classifies reply body → persists as lead.
    """
    logger.info(f"[Email Webhook] from={payload.from_email} subject={payload.subject[:50]}")

    lead_status, price, vehicle_year, notes = _classify_transcript(payload.body)

    lead = Lead(
        dealer_name=payload.dealer_name or payload.from_email,
        dealer_phone=payload.dealer_phone or "",
        price=price,
        vehicle_year=vehicle_year,
        status=lead_status,
        campaign_id=payload.campaign_id or "mkz",
        raw_transcript=payload.body[:5000],
        notes=f"[Email] Subject: {payload.subject[:200]} | {notes}",
    )
    db.add(lead)
    db.commit()
    db.refresh(lead)
    logger.info(f"[Email Webhook] Lead #{lead.id} saved → {lead_status}")

    if lead_status == LeadStatus.QUALIFIED:
        _fire_qualified_alert(lead)

    return {"received": True, "lead_id": lead.id, "status": lead_status}


# ── Leads ─────────────────────────────────────────────────────────────────────

@app.get("/leads", response_model=List[LeadResponse], tags=["Leads"])
def list_leads(
    status:      Optional[str] = Query(None, description="Filter by status: PENDING|QUALIFIED|REJECTED|MANUAL_REVIEW"),
    campaign_id: Optional[str] = Query(None),
    limit:       int           = Query(50, le=200),
    db: Session = Depends(get_db),
):
    """Return all leads, optionally filtered by status or campaign."""
    q = db.query(Lead)
    if status:
        try:
            q = q.filter(Lead.status == LeadStatus(status))
        except ValueError:
            raise HTTPException(400, f"Invalid status '{status}'. Valid: {[e.value for e in LeadStatus]}")
    if campaign_id:
        q = q.filter(Lead.campaign_id == campaign_id)
    return q.order_by(Lead.created_at.desc()).limit(limit).all()


@app.get("/leads/{lead_id}", response_model=LeadResponse, tags=["Leads"])
def get_lead(lead_id: int, db: Session = Depends(get_db)):
    """Return a single lead by ID."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, f"Lead #{lead_id} not found")
    return lead


@app.patch("/leads/{lead_id}/status", tags=["Leads"])
def update_lead_status(lead_id: int, update: LeadStatusUpdate, db: Session = Depends(get_db)):
    """Manually update a lead's status (for operator overrides)."""
    lead = db.query(Lead).filter(Lead.id == lead_id).first()
    if not lead:
        raise HTTPException(404, f"Lead #{lead_id} not found")
    lead.status = update.status
    if update.notes:
        lead.notes = (lead.notes or "") + f"\n[Manual] {update.notes}"
    lead.updated_at = datetime.utcnow()
    db.commit()
    logger.info(f"[Leads] #{lead_id} status updated to {update.status}")
    return {"ok": True, "lead_id": lead_id, "new_status": update.status}


# ── Campaigns ─────────────────────────────────────────────────────────────────

@app.get("/campaigns", tags=["Campaigns"])
def list_campaigns():
    """List available campaigns (delegating to CommPlexCore)."""
    try:
        from CommPlexCore.campaigns.mkz import MKZCampaign
        campaigns = [MKZCampaign().summary()]
    except ImportError:
        # Fallback if CommPlexCore not on path
        campaigns = [{"slug": "mkz", "campaign_id": "MKZ_2016_HYBRID", "status": "CommPlexCore not linked"}]
    return {"campaigns": campaigns}


@app.post("/campaigns/{slug}/run", tags=["Campaigns"])
def run_campaign(slug: str, req: CampaignRunRequest, db: Session = Depends(get_db)):
    """
    Trigger a campaign module. dry_run=true (default) never hits real APIs.
    CommPlexAPI routes the request; CommPlexCore executes the logic.
    """
    logger.info(f"[Campaign Run] slug={slug} module={req.module} dry_run={req.dry_run}")

    if req.dry_run:
        return {
            "slug":    slug,
            "module":  req.module,
            "dry_run": True,
            "result":  f"[DRY RUN] Would execute {req.module} module for '{slug}' campaign. Set dry_run=false to run.",
        }

    # Production run — delegate to CommPlexCore
    return {
        "slug":   slug,
        "module": req.module,
        "result": "Production run requires CommPlexCore to be linked. See README.",
    }


# ── Internal Helpers ──────────────────────────────────────────────────────────

def _classify_transcript(transcript: str):
    """
    Route transcript to CommPlexCore SluiceEngine.
    Returns (LeadStatus, price, vehicle_year, notes).
    """
    try:
        from CommPlexCore.gcp.vertex import get_classifier
        clf = get_classifier()
        result = clf.classify_lead(transcript)
        status = (
            LeadStatus.QUALIFIED    if result.qualified else
            LeadStatus.MANUAL_REVIEW if result.manual_review else
            LeadStatus.REJECTED
        )
        return status, result.price_detected, result.vehicle_year, result.reasoning
    except ImportError:
        # CommPlexCore not on path — use inline sluice stub
        logger.warning("[Gateway] CommPlexCore not found — using inline STUB classifier")
        return LeadStatus.PENDING, None, None, "CommPlexCore not linked — manual review required"


def _fire_qualified_alert(lead: Lead):
    """Fire ntfy/Pushover notification via CommPlexEdge notifier."""
    try:
        from CommPlexEdge.modules.notifier import NotifierModule
        n = NotifierModule()
        n.qualified_lead_alert(
            dealer_name=lead.dealer_name,
            price=lead.price,
            lead_id=lead.id,
        )
    except ImportError:
        logger.warning(f"[Gateway] CommPlexEdge not on path — alert not sent for lead #{lead.id}")
    except Exception as e:
        logger.error(f"[Gateway] Notifier failed for lead #{lead.id}: {e}")


# ── Dev Entrypoint ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port, reload=True)
