"""
arc-fleet-campaign/server/app.py
Flask REST API — runs on GCP e2-micro (free tier) or Codespaces.

Endpoints:
    GET  /api/campaigns              — List all campaigns and summary stats
    GET  /api/campaigns/<id>         — Campaign detail + contacts
    POST /api/run/<id>/<module>      — Trigger a module (email|formfill|sms|phone)
    GET  /api/status/<id>            — Per-contact status for a campaign
    POST /api/contact/<id>/update    — Update a contact's status
    GET  /api/health                 — Health check
"""

import os
import asyncio
import logging
from flask import Flask, jsonify, request, abort
from flask_cors import CORS

from campaigns.mkz_campaign import MKZCampaign
from campaigns.all_campaigns import TownCarCampaign, F350Campaign, JaycoCampaign
from modules.emailer import Emailer
from modules.formfill import FormFiller
from tracking.tracker import CampaignTracker

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)   # Allow Claude Artifact / Colab to call this server

# ── Campaign registry ────────────────────────────────────────────────────────
CAMPAIGNS = {
    "mkz":      MKZCampaign(),
    "towncar":  TownCarCampaign(),
    "f350":     F350Campaign(),
    "jayco":    JaycoCampaign(),
}

tracker = CampaignTracker()


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "campaigns": list(CAMPAIGNS.keys())})


@app.route("/api/campaigns")
def list_campaigns():
    return jsonify([c.summary() for c in CAMPAIGNS.values()])


@app.route("/api/campaigns/<campaign_id>")
def campaign_detail(campaign_id):
    c = CAMPAIGNS.get(campaign_id)
    if not c:
        abort(404, f"Campaign '{campaign_id}' not found")
    contacts = [
        {"name": ct.name, "email": ct.email, "phone": ct.phone,
         "url": ct.url, "tier": ct.tier, "method": ct.method,
         "status": ct.status, "notes": ct.notes}
        for ct in c.contacts
    ]
    return jsonify({**c.summary(), "vehicle": c.vehicle_info, "contacts": contacts})


@app.route("/api/run/<campaign_id>/<module>", methods=["POST"])
def run_module(campaign_id, module):
    """
    Trigger a campaign module.
    POST body: {"dry_run": true, "submit": false}
    """
    c = CAMPAIGNS.get(campaign_id)
    if not c:
        abort(404)

    body = request.get_json(silent=True) or {}
    dry_run = body.get("dry_run", True)

    if module == "email":
        emailer = Emailer()
        results = emailer.run_campaign(c, dry_run=dry_run)
        return jsonify({"module": "email", "campaign": campaign_id, "results": results})

    elif module == "formfill":
        submit = body.get("submit", False)
        filler = FormFiller(headless=True, submit=submit,
                            screenshot_dir=f"screenshots/{campaign_id}")
        results = asyncio.run(filler.run_campaign(c))
        return jsonify({"module": "formfill", "campaign": campaign_id, "results": results})

    elif module == "sms":
        from modules.stubs import SMSSender
        sender = SMSSender()
        results = sender.run_campaign(c, dry_run=dry_run)
        return jsonify({"module": "sms", "campaign": campaign_id, "results": results})

    elif module == "phone":
        from modules.stubs import PhoneCaller
        caller = PhoneCaller()
        wave = body.get("wave", 1)
        results = caller.run_wave(c, wave=wave, dry_run=dry_run)
        return jsonify({"module": "phone", "campaign": campaign_id, "results": results})

    else:
        abort(400, f"Unknown module: {module}")


@app.route("/api/status/<campaign_id>")
def campaign_status(campaign_id):
    rows = tracker.get_campaign_status(campaign_id)
    return jsonify(rows)


@app.route("/api/contact/<campaign_id>/update", methods=["POST"])
def update_contact(campaign_id):
    body = request.get_json()
    tracker.update_contact(campaign_id, body["contact_name"], body["status"], body.get("notes", ""))
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    debug = os.getenv("FLASK_DEBUG", "false").lower() == "true"
    app.run(host="0.0.0.0", port=port, debug=debug)
