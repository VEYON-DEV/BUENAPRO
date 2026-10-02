"""Read-only local UI QA against real PROD4 data; no external document clicks."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright

out = Path("docs/new-style/qa")
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    for name, width, height in [("desktop", 1440, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        response = page.goto("http://127.0.0.1:3000/oportunidad/seace/1242359", wait_until="networkidle")
        assert response.status == 200
        assert page.get_by_role("heading", name="CP SER-SM-14-2026-BCRPLIM-1", exact=True).is_visible()
        assert page.get_by_role("heading", name="Qué te falta", exact=True).is_visible()
        assert page.get_by_text("La evaluación de requisitos con tu empresa todavía", exact=False).count() == 0
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
        page.screenshot(path=str(out / f"prod4-detail-{name}-20261002.png"), full_page=True)
        page.get_by_role("tab", name="Documentos", exact=False).click()
        assert page.get_by_role("heading", name="Documentos oficiales", exact=True).is_visible()
        assert "prod1.seace.gob.pe" in page.get_by_role("link", name="Abrir en SEACE", exact=True).get_attribute("href")
        assert page.url.endswith("#documents")
        page.screenshot(path=str(out / f"prod4-detail-documents-{name}-20261002.png"), full_page=True)
        page.get_by_role("tab", name="Cronograma", exact=True).press("Enter")
        assert page.get_by_role("heading", name="Cronograma oficial", exact=True).is_visible()
        assert page.url.endswith("#schedule")
        assert page.get_by_text("Preguntas y observaciones", exact=True).is_visible()
        page.screenshot(path=str(out / f"prod4-detail-schedule-{name}-20261002.png"), full_page=True)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
    page.goto("http://127.0.0.1:3000/feed?source=prod4&q=BCRPLIM", wait_until="networkidle")
    page.get_by_role("button").filter(has_text="CP SER-SM-14-2026-BCRPLIM-1").click()
    page.get_by_role("link", name="Ver detalle completo", exact=False).click()
    page.wait_for_url("**/oportunidad/seace/1242359")
    assert "/oportunidad/seace/1242359" in page.url
    assert not errors, errors
    print(json.dumps({"viewports": 3, "screenshots": 9, "feed_navigation": "passed", "errors": errors}))
    browser.close()
