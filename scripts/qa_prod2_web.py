"""Read-only app QA. The local fixture server replaces every database query.

Run with PROD2_QA_ORIGIN (default localhost:3002). Never visits SEACE or calls a model.
This script does not authenticate to production; production session QA is separate.
"""
import asyncio
import json
import os
import re
from pathlib import Path
from playwright.async_api import async_playwright

ORIGIN = os.environ.get("PROD2_QA_ORIGIN", "http://localhost:3002").rstrip("/")
PROFILE = "00000000-0000-0000-0000-000000000001"
OUT = Path("docs/new-style/qa")
FEED = f"{ORIGIN}/feed?source=prod2&profile_id={PROFILE}"
DETAIL = f"{ORIGIN}/oportunidad/prod2/1001?profile_id={PROFILE}"


async def main():
    assert ORIGIN.startswith(("http://127.0.0.1:", "http://localhost:")), "Fixture QA requires a local origin"
    OUT.mkdir(parents=True, exist_ok=True)
    errors, writes = [], []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))

        async def readonly(route):
            if route.request.method not in ("GET", "HEAD"):
                writes.append(route.request.method)
                return await route.abort()
            await route.continue_()

        await page.route("**/api/**", readonly)
        for name, width, height in [("desktop", 1440, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]:
            await page.set_viewport_size({"width": width, "height": height})
            await page.goto(FEED, wait_until="networkidle")
            await page.wait_for_timeout(800)
            assert await page.get_by_text("10 procedimientos consultados en SEACE", exact=False).is_visible()
            assert await page.locator("article button[aria-expanded]").count() == 10
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"feed overflow: {name}"
            await page.screenshot(path=str(OUT / f"prod2-feed-{name}-20261005.png"), full_page=name != "mobile")

            await page.goto(DETAIL, wait_until="networkidle")
            await page.wait_for_timeout(800)
            assert await page.get_by_text("Fuente PROD2", exact=True).is_visible()
            assert await page.get_by_text("VEYON SAC + DINAUT", exact=True).is_visible()
            assert await page.get_by_role("link", name="Revisar en SEACE", exact=True).get_attribute("href") == "https://prod2.seace.gob.pe/seacebus-uiwd-pub/buscadorPublico/buscadorPublico.xhtml?faces-redirect=true"
            assert await page.get_by_role("link", name="Abrir bases en PDF", exact=True).get_attribute("href") == "/api/prod2/opportunities/1001/documents/00000000-0000-0000-0000-000000000010"
            assert await page.get_by_role("button", name="Analizar con IA", exact=True).count() == 0
            assert await page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), f"detail overflow: {name}"
            await page.screenshot(path=str(OUT / f"prod2-detail-{name}-20261005.png"), full_page=name != "mobile")
            await page.get_by_role("tab", name="Cronograma", exact=True).click()
            await page.get_by_text("Buscador público · PROD2", exact=False).wait_for()
            assert await page.get_by_text("Hora no informada", exact=False).count() > 0

        await page.set_viewport_size({"width": 1440, "height": 1000})
        await page.goto(DETAIL, wait_until="networkidle")
        await page.wait_for_timeout(800)
        await page.locator("details summary").filter(has_text="Certificación Scrum").press("Enter")
        assert await page.get_by_text("Deseable / no obligatorio", exact=True).is_visible()
        assert await page.get_by_role("heading", name=re.compile(r"^DINAUT\b")).is_visible()
        await page.get_by_role("tab", name="Documentos", exact=False).press("Enter")
        assert await page.get_by_text("Documento utilizado en la evaluación", exact=True).is_visible()

        await page.goto(FEED + "&q=no-results", wait_until="networkidle")
        assert await page.get_by_role("heading", name="No hay concursos con esos filtros", exact=True).is_visible()
        await page.screenshot(path=str(OUT / "prod2-feed-empty-20261005.png"), full_page=True)

        # The quick inspector remains usable while a read is pending, and reports a failed read.
        pending = []
        await page.route("**/api/prod2/opportunities/*", lambda route: pending.append(route))
        await page.goto(FEED, wait_until="domcontentloaded")
        await page.get_by_text("Cargando cronograma y documentos…", exact=True).wait_for()
        await page.screenshot(path=str(OUT / "prod2-feed-loading-20261005.png"), full_page=True)
        assert pending
        await pending[0].fulfill(status=503, json={"error": "Fixture failure"})
        await page.get_by_role("alert").filter(has_text="No se pudo cargar la ficha").wait_for()
        await page.screenshot(path=str(OUT / "prod2-feed-error-20261005.png"), full_page=True)
        await page.unroute("**/api/prod2/opportunities/*")
        await page.get_by_role("button", name="Cerrar vista rápida", exact=True).press("Enter")
        await page.locator("article button[aria-expanded]").first.press("Enter")
        await page.get_by_role("link", name="Ver detalle completo", exact=False).wait_for()

        response = await page.request.get(f"{ORIGIN}/api/prod2/opportunities/1001?profile_id=00000000-0000-0000-0000-000000000099")
        assert response.status == 404, "Foreign profile must be rejected"
        await page.goto(f"{ORIGIN}/oportunidad/prod2/1002?profile_id={PROFILE}", wait_until="networkidle")
        assert await page.get_by_text("Las bases requieren revisión humana.", exact=False).is_visible()
        assert await page.get_by_text("Evaluación documental pendiente.", exact=True).is_visible()
        await page.get_by_role("tab", name="Cronograma", exact=True).press("Enter")
        assert await page.get_by_text("Buscador público · PROD2", exact=False).is_visible()
        assert not errors, errors
        assert not writes, "QA attempted a write"
        print(json.dumps({"mode": "isolated_fixture", "screenshots": 9, "viewports": 3, "client_errors": len(errors), "attempted_writes": len(writes)}))
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
