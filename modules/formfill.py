"""
arc-fleet-campaign/modules/formfill.py
Playwright-based async form filler for dealer contact pages.
Runs in Colab, Codespaces, GCP, or Chromebook (headless or headed).

Usage:
    from modules.formfill import FormFiller
    from campaigns.mkz_campaign import MKZCampaign

    filler = FormFiller(headless=True, screenshot_dir="screenshots/mkz")
    asyncio.run(filler.run_campaign(MKZCampaign()))

In Colab:
    await filler.run_campaign(MKZCampaign())
"""

import asyncio
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Optional

from campaigns.base_campaign import BaseCampaign, Contact

logger = logging.getLogger(__name__)

# ── Field selector priority lists ────────────────────────────────────────────
NAME_SELECTORS = [
    'input[name*="name" i]', 'input[placeholder*="name" i]',
    '#name', 'input[id*="name" i]', 'input[autocomplete="name"]',
    'input[name="full_name"]', 'input[name="fullname"]',
]
EMAIL_SELECTORS = [
    'input[type="email"]', 'input[name*="email" i]',
    'input[placeholder*="email" i]', '#email',
]
PHONE_SELECTORS = [
    'input[type="tel"]', 'input[name*="phone" i]',
    'input[placeholder*="phone" i]', '#phone',
]
MSG_SELECTORS = [
    'textarea[name*="message" i]', 'textarea[placeholder*="message" i]',
    'textarea[id*="message" i]', 'textarea', '#message', '#comments',
    'textarea[name*="comment" i]',
]
SUBMIT_SELECTORS = [
    'button[type="submit"]', 'input[type="submit"]',
    'button:has-text("Submit")', 'button:has-text("Send")',
    'button:has-text("Send Message")', '#submit',
]


class FormFiller:
    """
    Async Playwright form filler. One instance handles an entire campaign batch.

    Args:
        headless:         Run Chromium headless (True for server/Colab, False to watch)
        screenshot_dir:   Where to save before/after screenshots
        submit:           Actually click submit (default False — review screenshots first!)
        delay_between:    Seconds between sites (be polite to servers)
    """

    def __init__(self,
                 headless: bool = True,
                 screenshot_dir: str = "screenshots",
                 submit: bool = False,
                 delay_between: float = 2.0):
        self.headless = headless
        self.screenshot_dir = Path(screenshot_dir)
        self.submit = submit
        self.delay_between = delay_between
        self.results: list = []

    async def _try_fill(self, page, selectors: list, value: str) -> bool:
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if await el.is_visible(timeout=2000):
                    await el.clear()
                    await el.fill(value)
                    return True
            except Exception:
                continue
        return False

    async def fill_single(self, contact: Contact, campaign: BaseCampaign) -> dict:
        """Fill one contact form. Returns result dict."""
        from playwright.async_api import async_playwright

        url = contact.url
        name = contact.name
        safe_name = name.lower().replace(" ", "_").replace("'", "").replace("/", "")
        screenshot_path = self.screenshot_dir / f"{safe_name}_filled.png"
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)

        body = campaign.get_message(contact.tier)
        sender = campaign.SENDER

        result = {
            "contact": name,
            "url": url,
            "timestamp": datetime.utcnow().isoformat(),
            "fields_filled": {},
            "status": "PENDING",
            "screenshot": str(screenshot_path),
            "submitted": False,
        }

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=self.headless)
            page = await browser.new_page()

            try:
                await page.goto(url, timeout=30000)
                await page.wait_for_load_state("networkidle", timeout=15000)
            except Exception as e:
                result["status"] = f"LOAD_ERROR: {e}"
                await browser.close()
                return result

            # Fill fields
            filled = {
                "name":    await self._try_fill(page, NAME_SELECTORS,  sender["name"]),
                "email":   await self._try_fill(page, EMAIL_SELECTORS, sender["email"]),
                "phone":   await self._try_fill(page, PHONE_SELECTORS, sender["phone"]),
                "message": await self._try_fill(page, MSG_SELECTORS,   body),
            }
            result["fields_filled"] = filled
            result["status"] = "FILLED" if any(filled.values()) else "NO_FIELDS_FOUND"

            # Screenshot before submit
            await page.screenshot(path=str(screenshot_path), full_page=True)
            logger.info(f"📸 Screenshot: {screenshot_path}")

            # Submit (only if explicitly enabled AND fields were filled)
            if self.submit and filled.get("message"):
                try:
                    for sel in SUBMIT_SELECTORS:
                        submit_btn = page.locator(sel).first
                        if await submit_btn.is_visible(timeout=2000):
                            await submit_btn.click()
                            await page.wait_for_load_state("networkidle", timeout=10000)
                            result["submitted"] = True
                            # Post-submit screenshot
                            post_path = self.screenshot_dir / f"{safe_name}_submitted.png"
                            await page.screenshot(path=str(post_path), full_page=True)
                            logger.info(f"✅ Submitted: {name}")
                            break
                except Exception as e:
                    logger.warning(f"Submit click failed for {name}: {e}")

            await browser.close()

        contact.status = "SENT" if result["submitted"] else result["status"]
        return result

    async def run_campaign(self, campaign: BaseCampaign,
                           methods: list = None,
                           concurrency: int = 3) -> list:
        """
        Run form fills for all contacts in a campaign.

        Args:
            campaign:    Campaign object
            methods:     Which contact methods to include (default: ['form'])
            concurrency: Parallel Playwright sessions (keep low to avoid detection)
        """
        if methods is None:
            methods = ["form"]

        contacts_to_fill = [
            c for c in campaign.contacts
            if c.method in methods and c.url and c.status == "PENDING"
        ]

        print(f"\n🚗 Campaign: {campaign.CAMPAIGN_ID}")
        print(f"📋 Form contacts: {len(contacts_to_fill)}")
        print(f"{'⚠️ SUBMIT ENABLED' if self.submit else '📷 REVIEW MODE (no submit)'}")
        print(f"📁 Screenshots: {self.screenshot_dir}/\n")

        # Process in batches for concurrency control
        all_results = []
        for i in range(0, len(contacts_to_fill), concurrency):
            batch = contacts_to_fill[i:i + concurrency]
            tasks = [self.fill_single(c, campaign) for c in batch]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)

            for contact, result in zip(batch, batch_results):
                if isinstance(result, Exception):
                    result = {"contact": contact.name, "status": f"ERROR: {result}"}
                    contact.status = "ERROR"
                all_results.append(result)
                status_icon = "✅" if result.get("submitted") else (
                    "📋" if result.get("status") == "FILLED" else "❌"
                )
                print(f"  {status_icon} {contact.name} — {result.get('status')}")

            if i + concurrency < len(contacts_to_fill):
                await asyncio.sleep(self.delay_between)

        self.results = all_results

        # Save results log
        log_path = Path(f"logs/formfill_{campaign.CAMPAIGN_ID}_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.json")
        log_path.parent.mkdir(exist_ok=True)
        with open(log_path, "w") as f:
            json.dump(all_results, f, indent=2)

        # Summary
        filled = sum(1 for r in all_results if r.get("status") == "FILLED")
        submitted = sum(1 for r in all_results if r.get("submitted"))
        errors = sum(1 for r in all_results if "ERROR" in str(r.get("status", "")))
        print(f"\n{'='*60}")
        print(f"  Filled (not submitted): {filled}")
        print(f"  Submitted: {submitted}")
        print(f"  Errors: {errors}")
        print(f"  Log: {log_path}")
        print(f"\n⚠️ REVIEW SCREENSHOTS in {self.screenshot_dir}/ before enabling submit=True")

        return all_results

    def enable_submit(self):
        """
        DANGER: Enables actual form submission. Only call after reviewing screenshots.
        Usage:
            filler = FormFiller(submit=False)
            await filler.run_campaign(campaign)   # Review screenshots
            filler.enable_submit()
            await filler.run_campaign(campaign)   # Now submits
        """
        print("⚠️  SUBMIT ENABLED — forms will be submitted on next run_campaign() call")
        self.submit = True
