"""Read-only local Gantt QA using the configured development tenant and real DB."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright, expect

out = Path("docs/new-style/qa")
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    results = []
    for source in ["prod6", "prod4"]:
        for name, width, height in [("desktop", 1600, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]:
            page.set_viewport_size({"width": width, "height": height})
            response = page.goto(f"http://127.0.0.1:3000/cronograma?source={source}", wait_until="networkidle")
            assert response.status == 200
            assert page.get_by_role("heading", name="Cronograma", exact=True).is_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), (source, name)
            data = page.request.get(f"http://127.0.0.1:3000/api/cronograma?source={source}").json()
            assert data["data"]
            assert all(row["verdict"] in ["verde", "ambar"] or (row["verdict"] is None and row["fitLevel"] >= 2) for row in data["data"])
            page.screenshot(path=str(out / f"gantt-{source}-{name}-20261002.png"), full_page=name != "mobile")
            if name != "mobile":
                chart = page.get_by_role("region", name="Gantt de oportunidades y etapas oficiales")
                first_bar = chart.get_by_role("button").first
                first_bar.press("Enter")
                assert page.get_by_role("region", name="Etapa seleccionada").is_visible()
                assert page.get_by_role("link", name="Ver cronograma completo", exact=False).get_attribute("href").endswith("#schedule")
                page.get_by_role("button", name="Cerrar etapa seleccionada").click()
                page.get_by_role("button", name="Periodo siguiente").click()
                assert "start=" in page.url
                page.get_by_role("button", name="Hoy", exact=True).click()
                page.get_by_role("combobox", name="Días visibles").select_option("60")
                assert "days=60" in page.url
            else:
                summary = page.locator("summary").first
                summary.press("Enter")
                assert page.locator("details[open]").count() == 1
            results.append({"source": source, "viewport": name, "count": data["meta"]["count"], "stages": sum(len(row["schedule"]) for row in data["data"])})
    page.goto("http://127.0.0.1:3000/cronograma?source=prod4", wait_until="networkidle")
    page.get_by_role("textbox", name="Buscar oportunidades en el cronograma").fill("QA_no_matching_opportunity_987")
    page.get_by_role("button", name="Buscar", exact=True).click()
    expect(page.get_by_role("heading", name="No hay oportunidades con estos criterios")).to_be_visible()
    assert "q=QA_no_matching" in page.url
    page.get_by_role("textbox", name="Buscar oportunidades en el cronograma").fill("")
    page.get_by_role("button", name="Buscar", exact=True).click()
    page.wait_for_function("document.body.innerText.includes('oportunidades con potencial')")
    page.route("**/api/cronograma?*", lambda route: route.fulfill(status=503, body="{}"))
    page.get_by_role("button", name="Actualizar cronograma desde la base de datos").click()
    expect(page.get_by_text("No se pudo actualizar el cronograma. Intenta nuevamente.", exact=True)).to_be_visible()
    assert not errors, errors
    print(json.dumps({"results": results, "keyboard": "passed", "empty_error": "passed", "errors": errors}))
    browser.close()
