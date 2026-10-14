"""Read-only production QA using an internal, ephemeral NextAuth session.

Requires operator access to the production host. Uses an EXISTING real tenant
member, checks the session through /api/auth/session, and never alters auth or
users. This verifies authorized app reads, not password login. Secrets/cookies
remain in memory, expire in ten minutes, and are never printed or saved.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
import subprocess
import sys
import time
from urllib.parse import urlsplit

import psycopg
from psycopg.rows import dict_row
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))
from buenapro_worker.prod2.runtime import production_settings

HOST = "ubuntu@168.129.177.199"
KEY = ROOT / "credentials/ssh-key-2026-07-14.key"
ORIGIN = "https://buenapro.veyon.dev"
PROFILE = "bc4da6f4-41be-4c60-9421-c14e6b5c95d3"
TENANT = "cc04995b-386d-45eb-ae1b-3d4353789fa2"
OUT = ROOT / "docs/new-style/qa"
PHASE = "prepare"


def session():
    result = subprocess.run(["ssh", "-i", str(KEY), "-o", "BatchMode=yes", HOST,
        "docker inspect --format '{{json .Config.Env}}' docker-web-1"], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("Production configuration unavailable")
    env = dict(value.split("=", 1) for value in json.loads(result.stdout) if "=" in value)
    if urlsplit(env.get("AUTH_URL") or env.get("NEXTAUTH_URL") or "").hostname != urlsplit(ORIGIN).hostname:
        raise RuntimeError("Configured origin does not match QA target")
    with production_settings(ssh_host=HOST, ssh_key=KEY) as settings:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            member = conn.execute("""SELECT user_id::text, role FROM tenant_members m
              WHERE tenant_id=%s AND (SELECT tenant_id FROM tenant_members prior WHERE prior.user_id=m.user_id
                ORDER BY created_at LIMIT 1)=%s
              ORDER BY CASE role WHEN 'owner' THEN 0 WHEN 'admin' THEN 1 ELSE 2 END,created_at LIMIT 1""", (TENANT, TENANT)).fetchone()
    if not member:
        raise RuntimeError("No existing member with this active tenant context")
    secret = env.get("NEXTAUTH_SECRET") or env.get("AUTH_SECRET")
    if not secret:
        raise RuntimeError("Production session configuration missing")
    payload = {"secret": secret, "maxAge": 600, "token": {"sub": member["user_id"], "userId": member["user_id"],
               "tenantId": TENANT, "role": member["role"], "name": "Verificación de lectura"}}
    javascript = "let raw='';process.stdin.on('data',x=>raw+=x);process.stdin.on('end',async()=>{const {encode}=require('next-auth/jwt');process.stdout.write(await encode(JSON.parse(raw)));});"
    encoded = subprocess.run(["node", "-e", javascript], input=json.dumps(payload), capture_output=True, text=True, cwd=ROOT, timeout=30)
    if encoded.returncode or not encoded.stdout:
        raise RuntimeError("Temporary session encoding failed")
    return encoded.stdout


async def main():
    global PHASE
    PHASE = "existing_member_session"
    cookie = session()
    OUT.mkdir(parents=True, exist_ok=True)
    errors, writes = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        context = await browser.new_context()
        async def readonly(route):
            if route.request.method not in ("GET", "HEAD"):
                writes.append(route.request.method)
                return await route.abort()
            await route.continue_()
        await context.route("**/api/**", readonly)
        await context.add_cookies([{"name": "__Secure-next-auth.session-token", "value": cookie,
            "domain": urlsplit(ORIGIN).hostname, "path": "/", "httpOnly": True, "secure": True,
            "sameSite": "Lax", "expires": time.time()+600}])
        PHASE = "session_readback"
        response = await context.request.get(ORIGIN + "/api/auth/session")
        data = await response.json()
        assert response.status == 200 and data.get("user", {}).get("tenantId") == TENANT, "Actual production session rejected"
        PHASE = "pilot_list"
        response = await context.request.get(ORIGIN + f"/api/prod2/opportunities?profile_id={PROFILE}")
        assert response.status == 200, "Production API failed"
        listing = await response.json()
        assert listing["total"] == 10 and len(listing["data"]) == 10, "Expected ten production pilot records"
        assert len({row["id_convocatoria"] for row in listing["data"]}) == 10, "Duplicate convocatoria identity"
        assert all(row["source_key"] == "seace_prod2" for row in listing["data"]), "Incorrect source identity"
        assert listing["profile"]["id"] == PROFILE, "Incorrect selected profile"
        PHASE = "tenant_isolation"
        foreign = await context.request.get(ORIGIN + "/api/prod2/opportunities?profile_id=e11f2610-eb56-457a-97eb-bbd24dd6f1a1")
        assert foreign.status == 404, "Foreign tenant profile must be rejected"
        details = []
        pdfs = 0
        for row in listing["data"]:
            PHASE = "canonical_detail"
            response = await context.request.get(ORIGIN + f"/api/prod2/opportunities/{row['id_convocatoria']}?profile_id={PROFILE}")
            assert response.status == 200, "Production detail API failed"
            detail = await response.json()
            assert detail["process"]["source_key"] == "seace_prod2" and detail["official_schedule"], "Missing source/schedule"
            assert detail["analysis"]["extraction"], "Missing Gemini extraction"
            assert detail["analysis"]["match"] or detail["analysis"]["extraction"]["requires_human_review"], "Missing evaluation or explicit review gate"
            match = detail["analysis"]["match"]
            if match:
                metadata = match["breakdown_json"]["meta"]
                assert match["profile_id"] == PROFILE, "Match belongs to a different profile"
                assert metadata["source_key"] == "seace_prod2" and str(metadata["id_convocatoria"]) == str(row["id_convocatoria"]), "Match source binding failed"
                assert str(metadata["extraction_id"]) == str(detail["analysis"]["extraction"]["id"]), "Stale extraction binding"
                assert metadata["document_sha256"] == detail["analysis"]["extraction"]["sha256_original"], "Document binding failed"
                assert {PROFILE, "e11f2610-eb56-457a-97eb-bbd24dd6f1a1"}.issubset(metadata.get("member_names", {})), "Missing member attribution"
            PHASE = "authenticated_pdf"
            document = next(doc for doc in detail["documents"] if doc.get("download_url"))
            assert document["download_url"].startswith(f"/api/prod2/opportunities/{row['id_convocatoria']}/documents/"), "Noncanonical document URL"
            response = await context.request.get(ORIGIN + document["download_url"])
            content = await response.body()
            assert response.status == 200 and content.startswith(b"%PDF-"), "Authenticated auxiliary PDF failed"
            pdfs += 1
            details.append(detail)
        matched = [detail for detail in details if detail["analysis"]["match"]]
        gated = [detail for detail in details if not detail["analysis"]["match"] and detail["analysis"]["extraction"]["requires_human_review"]]
        PHASE = "completed_stage_counts"
        assert len(matched) == 9 and len(gated) == 1, "Expected nine matches and one explicit LLM1 review"
        page = await context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        feed = ORIGIN + f"/feed?source=prod2&profile_id={PROFILE}"
        sample = next((detail for detail in matched if any(requirement.get("member_gaps") for requirement in detail["analysis"]["match"]["breakdown_json"].get("requisitos", []))), matched[0])
        representative = sample["process"]["id_convocatoria"]
        for name,width,height in [("desktop",1440,1000),("laptop",1024,900),("mobile",390,844)]:
            PHASE = f"feed_{name}"
            await page.set_viewport_size({"width":width,"height":height})
            await page.goto(feed,wait_until="networkidle")
            await page.locator("article button[aria-expanded]").first.wait_for()
            assert await page.locator("article button[aria-expanded]").count()==10
            if name == "desktop":
                await page.get_by_role("button", name="Cerrar vista rápida", exact=True).wait_for()
                await page.get_by_role("heading", name="Cronograma oficial", exact=True).wait_for()
            assert await page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), f"Feed overflow {name}"
            await page.mouse.move(0, 0)
            await page.evaluate("window.scrollTo({top:0,left:0,behavior:'instant'})")
            await page.get_by_role("link", name="Abrir alertas", exact=True).wait_for()
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(OUT/f"prod2-production-feed-{name}-20261005.png"),full_page=name!="mobile")
            PHASE = f"detail_{name}"
            await page.goto(ORIGIN+f"/oportunidad/prod2/{representative}?profile_id={PROFILE}",wait_until="networkidle")
            assert await page.get_by_text("Fuente PROD2",exact=True).is_visible()
            assert await page.get_by_role("link",name="Abrir bases en PDF",exact=False).is_visible()
            assert await page.get_by_role("button", name="Analizar con IA", exact=True).count() == 0
            assert await page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), f"Detail overflow {name}"
            await page.mouse.move(0, 0)
            await page.evaluate("window.scrollTo({top:0,left:0,behavior:'instant'})")
            await page.get_by_role("link", name="Abrir alertas", exact=True).wait_for()
            await page.wait_for_timeout(150)
            await page.screenshot(path=str(OUT/f"prod2-production-detail-{name}-20261005.png"),full_page=name!="mobile")
            PHASE = f"member_evidence_{name}"
            evaluated = sample["analysis"]["match"]["breakdown_json"].get("requisitos", [])
            member_requirement = next(((index, item) for index, item in enumerate(evaluated) if item.get("member_gaps")), None)
            if member_requirement:
                index, item = member_requirement
                disclosure = page.locator("#detail-pane-decision section").last.locator("details").nth(index)
                await disclosure.locator("summary").press("Enter")
                assert await disclosure.get_attribute("open") is not None, "Member evidence failed to expand"
                names = sample["analysis"]["match"]["breakdown_json"]["meta"]["member_names"]
                for gap in item["member_gaps"]:
                    await disclosure.get_by_role("heading", name=re.compile("^" + re.escape(names[gap["profile_id"]]))).wait_for()
            # Tab interactions verify hydration and dense content, without opening external portals.
            PHASE = f"schedule_{name}"
            await page.get_by_role("tab", name="Cronograma", exact=True).click()
            await page.get_by_role("region", name="Cronograma oficial", exact=True).wait_for()
            await page.get_by_text("Buscador público · PROD2", exact=False).wait_for()
            assert await page.locator("[aria-label='Cronograma oficial'] li[data-state]").count() == len(sample["official_schedule"])
            assert await page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), f"Schedule overflow {name}"
            PHASE = f"documents_{name}"
            await page.get_by_role("tab", name="Documentos", exact=False).click()
            await page.get_by_role("heading", name="Documentos oficiales", exact=True).wait_for()
            assert await page.get_by_text("Documento utilizado en la evaluación", exact=True).is_visible()
            assert await page.evaluate("document.documentElement.scrollWidth<=window.innerWidth"), f"Documents overflow {name}"
        PHASE = "review_gate_visible"
        await page.goto(ORIGIN+f"/oportunidad/prod2/{gated[0]['process']['id_convocatoria']}?profile_id={PROFILE}",wait_until="networkidle")
        assert await page.get_by_text("Las bases requieren revisión humana.",exact=False).is_visible()
        assert await page.get_by_text("Evaluación documental pendiente.",exact=True).is_visible()
        await page.get_by_role("tab", name="Cronograma", exact=True).click()
        await page.get_by_role("region", name="Cronograma oficial", exact=True).wait_for()
        PHASE = "client_readonly"
        assert not errors and not writes, "Client error or unexpected write"
        print(json.dumps({"origin":ORIGIN,"verified_records":10,"authenticated_pdfs":pdfs,"viewports":3,
                          "screenshots":6,"foreign_profile_status":foreign.status,"writes":len(writes),"client_errors":len(errors),
                          "matched":len(matched),"llm1_review":len(gated),"representative_convocatoria":representative,
                          "session_method":"internal_ephemeral_existing_member","password_login_tested":False}))
        await context.close()
        await browser.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        # Browser/network exceptions may contain signed URLs/cookies; do not log them.
        print(json.dumps({"qa":"failed","phase":PHASE,"error_type":type(error).__name__}))
        sys.exit(1)
