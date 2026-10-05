"""Local browser QA with production reads; every write is intercepted."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright, expect

out = Path("docs/new-style/qa")
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    original = page.request.get("http://127.0.0.1:3000/api/profile").json()["data"]
    saves, errors = [], []
    page.on("pageerror", lambda err: errors.append(str(err)))
    def intercept(route):
        request = route.request
        if request.method == "GET":
            route.continue_()
        elif request.url.endswith("/api/profile") and request.method == "PUT":
            payload = request.post_data_json
            saves.append(payload)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"data": {**original, **payload}}))
        else:
            route.fulfill(status=403, content_type="application/json", body='{"error":"QA: escritura bloqueada"}')
    page.route("**/api/**", intercept)
    for name, width, height in [("desktop", 1600, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]:
        page.goto("about:blank")
        page.set_viewport_size({"width": width, "height": height})
        page.goto("http://127.0.0.1:3000/perfil#experience", wait_until="networkidle")
        expect(page.get_by_role("heading", name="Experiencia económica por área", exact=False)).to_be_visible()
        # Wait until the asynchronous profile refresh has settled before editing.
        page.wait_for_timeout(700)
        for label, speciality, amount, currency in [
            ("Automatización industrial — ejemplo QA", "PLC, SCADA e instrumentación", "300000", "PEN"),
            ("Desarrollo de software — ejemplo QA", "Aplicaciones web y agentes IA", "80000", "USD"),
        ]:
            page.get_by_role("button", name="Agregar área", exact=True).click()
            page.get_by_label("Área o rubro", exact=False).fill(label)
            page.get_by_label("Especialidad del área", exact=True).fill(speciality)
            page.get_by_label("Tipo de experiencia", exact=True).select_option("servicios")
            page.get_by_label("Monto de experiencia del área", exact=False).fill(amount)
            page.get_by_label("Moneda del área", exact=True).select_option(currency)
            page.get_by_label("Alcance y sustento del área", exact=True).fill("Datos de prueba; no acreditan experiencia y no se guardan en producción.")
        second = page.get_by_role("button", name="Desarrollo de software — ejemplo QA", exact=False).filter(has=page.locator("[class*='overview']"))
        second.click()
        expect(page.get_by_text("USD 80,000", exact=True)).to_be_visible()
        expect(page.get_by_text("S/ 300,000", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        page.get_by_role("heading", name="Experiencia económica por área", exact=False).scroll_into_view_if_needed()
        page.evaluate("window.scrollTo(0, 0)")
        page.get_by_role("button", name="Agregar área", exact=True).blur()
        page.screenshot(path=str(out / f"profile-economic-areas-{name}-20261005.png"), full_page=name != "mobile")
        if name == "mobile":
            page.get_by_role("heading", name="Experiencia económica por área", exact=False).evaluate("el => el.scrollIntoView({block: 'start', behavior: 'instant'})")
            page.wait_for_timeout(100)
            page.screenshot(path=str(out / "profile-economic-areas-mobile-list-20261005.png"))
        page.get_by_role("button", name="Guardar cambios", exact=True).last.click()
        expect(page.get_by_text("Perfil guardado.", exact=False)).to_be_visible()
        saved = saves[-1]
        assert saved["experience_json"] == original["experience_json"]
        assert saved["team_json"] == original["team_json"]
        assert {k: v for k, v in saved["econ_experience_json"].items() if k != "areas"} == {k: v for k, v in original["econ_experience_json"].items() if k != "areas"}
        assert saved["econ_experience_json"]["areas"][-2]["monto"] == 300000
        assert saved["econ_experience_json"]["areas"][-1]["moneda"] == "USD"
    assert not errors, errors
    print(json.dumps({"viewports": 3, "areas_per_test": 2, "preservation": "passed", "production_writes": 0}))
    browser.close()
