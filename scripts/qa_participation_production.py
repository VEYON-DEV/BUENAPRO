"""Verify the participation release using existing production reads.

Uses the existing ten-minute internal-session helper; never saves credentials,
cookies, users or full API payloads. No analysis, save, document download or other
write action is clicked. PROD6's existing detail page may refresh its official
metadata on GET; the before/after timestamps are recorded separately.
"""
import asyncio
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlencode, urlsplit

import psycopg
from playwright.async_api import async_playwright

import qa_prod2_production as auth

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/new-style/qa"
PHASE = "prepare"
CHECK_ONLY = "--check-only" in sys.argv


def viewports():
    return [("desktop", 1440, 1000)] if CHECK_ONLY else [
        ("desktop", 1440, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]


def detail_metadata(contract_id):
    with auth.production_settings(ssh_host=auth.HOST, ssh_key=auth.KEY) as settings:
        with psycopg.connect(settings.database_url) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            row = conn.execute(
                "SELECT detail_fetched_at::text, schedule_fetched_at::text, hash_detail "
                "FROM seace_contracts WHERE id_contrato=%s", (contract_id,)
            ).fetchone()
            return dict(zip(["detail_fetched_at", "schedule_fetched_at", "hash_detail"], row)) if row else None


async def main():
    global PHASE
    PHASE = "existing_member_session"
    cookie = auth.session()
    OUT.mkdir(parents=True, exist_ok=True)
    errors, writes, blocked_documents, external, screenshots, sources = [], [], [], [], [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()

        async def guard(route):
            request = route.request
            if not request.url.startswith(auth.ORIGIN + "/"):
                external.append(urlsplit(request.url).hostname)
                return await route.abort()
            if request.method not in ("GET", "HEAD"):
                writes.append(request.method)
                return await route.abort()
            if "/documents/" in urlsplit(request.url).path:
                blocked_documents.append(urlsplit(request.url).path)
                return await route.abort()
            # Prevent Link prefetch from opening a stale PROD6 detail implicitly.
            if request.headers.get("next-router-prefetch"):
                return await route.abort()
            await route.continue_()

        await context.route("**/*", guard)
        await context.add_cookies([{
            "name": "__Secure-next-auth.session-token", "value": cookie,
            "domain": urlsplit(auth.ORIGIN).hostname, "path": "/", "httpOnly": True,
            "secure": True, "sameSite": "Lax", "expires": time.time() + 600,
        }])
        PHASE = "authenticated_session"
        response = await context.request.get(auth.ORIGIN + "/api/auth/session")
        session = await response.json()
        assert response.status == 200 and session.get("user", {}).get("tenantId") == auth.TENANT
        PHASE = "tenant_isolation"
        response = await context.request.get(auth.ORIGIN + "/api/prod2/opportunities?profile_id=e11f2610-eb56-457a-97eb-bbd24dd6f1a1")
        isolation_status = response.status
        assert isolation_status == 404, "tenant_isolation_http_" + str(isolation_status)
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)[:500]))

        for source in ["prod2", "prod4", "prod6"]:
            PHASE = source + "_api_list"
            endpoint = "/api/contracts?has_extraction=true&estado=2&limit=20" if source == "prod6" else (
                "/api/" + source + "/opportunities?" + urlencode({"page_size": 20,
                    **({"profile_id": auth.PROFILE} if source == "prod2" else {"state": "all"})}))
            response = await context.request.get(auth.ORIGIN + endpoint)
            assert response.status == 200, source + " list API failed"
            listing = await response.json()
            rows = listing["data"]
            assert rows and all("participation_extraction" in row for row in rows)
            selected = next((row for row in rows if (row.get("participation_extraction") or {}).get("participation")), rows[0])
            identity = str(selected[{"prod2": "id_convocatoria", "prod4": "id_procedimiento", "prod6": "id_contrato"}[source]])
            detail_api = ("/api/contracts/" + identity if source == "prod6" else
                "/api/" + source + "/opportunities/" + identity +
                ("?" + urlencode({"profile_id": auth.PROFILE}) if source == "prod2" else ""))
            response = await context.request.get(auth.ORIGIN + detail_api)
            assert response.status == 200, source + " detail API failed"
            detail = await response.json()
            process = detail["contract" if source == "prod6" else "process"]
            extraction = process.get("participation_extraction")
            assert "participation_extraction" in process
            if extraction and extraction.get("quality"):
                assert extraction.get("id") and extraction.get("page_scope") in ("combined", "document")
            source_result = {"source": source, "rows_checked": len(rows), "detail_id": identity,
                "list_status": 200, "detail_status": 200, "extraction_id": (extraction or {}).get("id"),
                "quality": (extraction or {}).get("quality"), "requires_human_review": (extraction or {}).get("requires_human_review"),
                "page_scope": (extraction or {}).get("page_scope"), "list_detail_same_extraction":
                    (selected.get("participation_extraction") or {}).get("id") == (extraction or {}).get("id")}
            feed_params = {"source": source, "deadline": "all", "state": "all"}
            if source == "prod2": feed_params["profile_id"] = auth.PROFILE
            feed = auth.ORIGIN + "/feed?" + urlencode(feed_params)
            for viewport, width, height in viewports():
                PHASE = source + "_feed_" + viewport
                await page.set_viewport_size({"width": width, "height": height})
                await page.goto(feed, wait_until="networkidle")
                await page.locator("article button[aria-expanded]").first.wait_for()
                assert await page.get_by_text("Subcontratación", exact=True).count() > 0
                assert await page.get_by_text("Consorcio", exact=True).count() > 0
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), PHASE + " overflow"
                if source == "prod6":
                    assert await page.locator('[class*="deadlineCell"] strong').evaluate_all(
                        "els => els.length > 0 && els.every(el => el.scrollWidth <= el.clientWidth + 1)"
                    ), PHASE + " deadline text exceeds its column"
                filename = "participation-production-" + source + "-feed-" + viewport + "-20261013.png"
                if not CHECK_ONLY:
                    await page.screenshot(path=str(OUT / filename))
                screenshots.append(filename)

            PHASE = source + "_detail"
            url = {"prod2": "/oportunidad/prod2/", "prod4": "/oportunidad/seace/", "prod6": "/oportunidad/"}[source] + identity
            if source == "prod2": url += "?" + urlencode({"profile_id": auth.PROFILE})
            if source == "prod6": source_result["official_detail_metadata_before"] = detail_metadata(identity)
            for viewport, width, height in viewports():
                PHASE = source + "_detail_" + viewport
                await page.set_viewport_size({"width": width, "height": height})
                await page.goto(auth.ORIGIN + url, wait_until="networkidle")
                section = page.get_by_role("region", name="Condiciones de participación", exact=True)
                await section.get_by_role("heading", name="Cómo puedes participar").wait_for()
                assert await section.get_by_text("Subcontratación", exact=True).count() == 1
                assert await section.get_by_text("Consorcio", exact=True).count() == 1
                summary = section.locator("summary").first
                await summary.press("Enter")
                assert await section.locator("details").first.get_attribute("open") is not None
                assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth"), PHASE + " overflow"
                filename = "participation-production-" + source + "-detail-" + viewport + "-20261013.png"
                if not CHECK_ONLY:
                    await page.screenshot(path=str(OUT / filename), full_page=True)
                screenshots.append(filename)
            if source == "prod6":
                source_result["official_detail_metadata_after"] = detail_metadata(identity)
                source_result["existing_get_metadata_refresh"] = source_result["official_detail_metadata_before"] != source_result["official_detail_metadata_after"]
            sources.append(source_result)

        PHASE = "final_checks"
        if CHECK_ONLY:
            screenshots = sorted(p.name for p in OUT.glob("participation-production-*-20261013.png"))
        # Cloudflare injects this external script in the production edge response.
        # The guard aborted it before transport, as it does all external requests.
        unexpected_external = [host for host in external if host != "static.cloudflareinsights.com"]
        result = {"policy": "participation_production_qa_v1", "observed_at": datetime.now(timezone.utc).isoformat(),
            "browser_check_mode": "six_desktop_pages_reusing_final_18_captures" if CHECK_ONLY else "18_captures",
            "origin": auth.ORIGIN, "session_verified": True, "session_type": "existing_member_internal_10min",
            "password_login_tested": False, "tenant_isolation_status": isolation_status, "sources": sources,
            "screenshots": [{"path": str((OUT / name).relative_to(ROOT)), "sha256": hashlib.sha256((OUT / name).read_bytes()).hexdigest()} for name in screenshots],
            "client_errors": errors, "write_requests": writes, "external_browser_requests": external,
            "unexpected_external_browser_requests": unexpected_external, "external_requests_aborted_before_transport": True,
            "blocked_document_requests": blocked_documents, "paid_model_calls_by_qa": 0,
            "semantic_approval_of_clauses": False, "qa_passed": not (errors or writes or unexpected_external)}
        (OUT / "participation-production-result-20261013.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"screenshots": len(screenshots), "sources": len(sources), "client_errors": len(errors), "write_requests": len(writes),
            "prod6_existing_get_metadata_refresh": sources[-1].get("existing_get_metadata_refresh")}))
        if errors or writes or unexpected_external:
            print(json.dumps({"client_errors": errors, "write_requests": writes, "unexpected_external_browser_requests": unexpected_external}))
        assert not errors, "Client errors"
        assert not writes, "Unexpected write request"
        assert not unexpected_external, "Unexpected external browser request"
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        status_detail = str(error) if PHASE == "tenant_isolation" and isinstance(error, AssertionError) else None
        print(json.dumps({"status": "production_qa_failed", "phase": PHASE, "error_type": type(error).__name__, "status_detail": status_detail}))
        sys.exit(1)
