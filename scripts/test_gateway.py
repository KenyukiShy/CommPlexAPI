"""
CommPlexAPI/scripts/test_gateway.py — Bland.ai Webhook Simulation
Domain: CommPlexAPI (The Mouth)

Simulates a Bland.ai webhook hitting the running API gateway.
Run this after `uvicorn server.main:app --reload` is active.

Usage:
    python scripts/test_gateway.py
    python scripts/test_gateway.py --url http://my-server:8080
    python scripts/test_gateway.py --scenario all
"""

import argparse
import json
import sys
import requests

DEFAULT_URL = "http://localhost:8080"


# ── Test Scenarios ────────────────────────────────────────────────────────────

SCENARIOS = {
    "qualify_standard": {
        "label": "✅ QUALIFY — Standard sluice ($25k, 2021 MKZ)",
        "endpoint": "/webhook/bland",
        "payload": {
            "call_id":      "bland-test-001",
            "status":       "completed",
            "transcript":   "Hi, I'm calling about your Lincoln. I have a 2021 MKZ and I'm looking to get $25,000 for it. It's in excellent shape, low miles.",
            "dealer_name":  "Fargo North Ford",
            "dealer_phone": "7015551234",
            "campaign_id":  "mkz",
        },
    },
    "reject_price": {
        "label": "❌ REJECT — Price over standard floor ($32k)",
        "endpoint": "/webhook/bland",
        "payload": {
            "call_id":      "bland-test-002",
            "status":       "completed",
            "transcript":   "Yeah I've got a 2022 Lincoln, asking $32,000. That's my bottom line.",
            "dealer_name":  "Bismarck Auto Group",
            "dealer_phone": "7015559999",
            "campaign_id":  "mkz",
        },
    },
    "reject_year": {
        "label": "❌ REJECT — Year too old (2018)",
        "endpoint": "/webhook/bland",
        "payload": {
            "call_id":      "bland-test-003",
            "status":       "completed",
            "transcript":   "I've got a 2018 Lincoln MKZ. I'd take $22,000 for it.",
            "dealer_name":  "Minot Motors",
            "dealer_phone": "7015558888",
            "campaign_id":  "mkz",
        },
    },
    "voicemail": {
        "label": "📵 SKIP — Voicemail (non-completed status)",
        "endpoint": "/webhook/bland",
        "payload": {
            "call_id":      "bland-test-004",
            "status":       "voicemail",
            "transcript":   "",
            "dealer_name":  "Grand Forks Lincoln",
            "dealer_phone": "7015557777",
            "campaign_id":  "mkz",
        },
    },
    "email_qualify": {
        "label": "📧 EMAIL — Qualify via email webhook",
        "endpoint": "/webhook/email",
        "payload": {
            "from_email":   "dealer@fargoford.com",
            "subject":      "Re: Lincoln MKZ — Offer",
            "body":         "Hello Kenyon, I can offer $26,500 for the 2021 Lincoln MKZ. Please call me to discuss.",
            "dealer_name":  "Fargo Ford Email",
            "dealer_phone": "7015556666",
            "campaign_id":  "mkz",
        },
    },
    "get_leads": {
        "label": "📋 GET /leads — List all leads",
        "endpoint": "/leads",
        "method":   "GET",
        "payload":  None,
    },
    "get_qualified": {
        "label": "🏆 GET /leads?status=QUALIFIED — Qualified leads only",
        "endpoint": "/leads?status=QUALIFIED",
        "method":   "GET",
        "payload":  None,
    },
    "health": {
        "label": "💚 GET /health — Gateway health check",
        "endpoint": "/health",
        "method":   "GET",
        "payload":  None,
    },
}


# ── Runner ────────────────────────────────────────────────────────────────────

def run_scenario(base_url: str, name: str, scenario: dict) -> bool:
    """Run a single test scenario. Returns True on success."""
    print(f"\n{'─' * 60}")
    print(f"  {scenario['label']}")
    print(f"  Endpoint: {scenario['endpoint']}")

    url    = f"{base_url}{scenario['endpoint']}"
    method = scenario.get("method", "POST")

    try:
        if method == "GET":
            resp = requests.get(url, timeout=10)
        else:
            resp = requests.post(url, json=scenario["payload"], timeout=10)

        print(f"  Status: {resp.status_code}")
        try:
            data = resp.json()
            print(f"  Response: {json.dumps(data, indent=4)}")
        except Exception:
            print(f"  Response (raw): {resp.text[:200]}")

        return resp.status_code < 400

    except requests.ConnectionError:
        print(f"  ❌ CONNECTION REFUSED — Is the gateway running?")
        print(f"     Start it with: uvicorn server.main:app --reload --port 8080")
        return False
    except Exception as e:
        print(f"  ❌ Error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="CommPlexAPI Gateway Simulation")
    parser.add_argument("--url",      default=DEFAULT_URL, help="Gateway base URL")
    parser.add_argument("--scenario", default="all",
                        choices=list(SCENARIOS.keys()) + ["all"],
                        help="Which scenario to run (default: all)")
    args = parser.parse_args()

    print("=" * 60)
    print("  CommPlexAPI — Bland.ai Webhook Gateway Simulation")
    print(f"  Target: {args.url}")
    print("=" * 60)

    # Always run health check first
    health_ok = run_scenario(args.url, "health", SCENARIOS["health"])
    if not health_ok:
        print("\n❌ Gateway not reachable. Start server first.")
        print("   uvicorn server.main:app --reload")
        sys.exit(1)

    to_run = (
        {k: v for k, v in SCENARIOS.items() if k != "health"}
        if args.scenario == "all"
        else {args.scenario: SCENARIOS[args.scenario]}
    )

    results = {}
    for name, scenario in to_run.items():
        results[name] = run_scenario(args.url, name, scenario)

    # Summary
    print(f"\n{'=' * 60}")
    print("  SIMULATION SUMMARY")
    print(f"{'=' * 60}")
    passed = sum(1 for v in results.values() if v)
    total  = len(results)
    for name, ok in results.items():
        icon = "✅" if ok else "❌"
        print(f"  {icon} {name}")
    print(f"\n  {passed}/{total} scenarios passed")
    print()

    if passed == total:
        print("✅ Checkpoint: Gateway simulation complete — all scenarios passed.")
    else:
        print(f"⚠️  {total - passed} scenario(s) failed. Check server logs.")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
