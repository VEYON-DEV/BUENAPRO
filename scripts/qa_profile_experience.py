"""Local profile QA: production reads only, all browser writes intercepted."""
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
    page.on("pageerror", lambda error: errors.append(str(error)))
    def intercept(route):
        request = route.request
        if request.method == "GET":
            route.continue_()
        elif request.url.endswith("/api/profile") and request.method == "PUT":
            payload = request.post_data_json
            saves.append(payload)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"data": {**original, **payload}}))
        else:
            route.fulfill(status=403, content_type="application/json", body='{"error":"QA: escrituras reales bloqueadas"}')
    page.route("**/api/**", intercept)
    for name, width, height in [("desktop", 1600, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]:
        page.goto("about:blank")
        page.set_viewport_size({"width": width, "height": height})
        page.goto("http://127.0.0.1:3000/perfil#experience", wait_until="networkidle")
        expect(page.get_by_role("heading", name="Experiencia acreditable", exact=True)).to_be_visible()
        page.get_by_role("button", name="Agregar contrato", exact=True).click()
        page.get_by_label("Objeto del contrato", exact=False).fill("Suministro, instalación y configuración de servidores empresariales")
        page.get_by_label("Cliente o entidad", exact=True).fill("Entidad de prueba — datos de QA")
        page.get_by_label("Rubro", exact=True).fill("Tecnología")
        page.get_by_label("Especialidad", exact=True).fill("Infraestructura y servidores")
        page.get_by_label("Tipo de contratación", exact=True).select_option("bienes")
        page.get_by_label("Actividades realizadas", exact=True).fill("Suministro de servidores, instalación y configuración. No incluye desarrollo de software.")
        page.get_by_label("Monto del contrato", exact=True).fill("300000")
        page.get_by_label("Moneda", exact=True).select_option("PEN")
        page.get_by_label("Participación de la empresa", exact=True).select_option("consorcio")
        page.get_by_label("Participación (%)", exact=True).fill("60")
        page.get_by_label("Fecha de inicio", exact=True).fill("2025-01-10")
        page.get_by_label("Fecha de finalización", exact=True).fill("2025-06-10")
        page.get_by_label("Fecha de conformidad", exact=True).fill("2025-06-20")
        page.get_by_label("Estado del respaldo", exact=True).select_option("pendiente")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        page.get_by_role("heading", name="Experiencia acreditable", exact=True).scroll_into_view_if_needed()
        page.screenshot(path=str(out / f"profile-experience-{name}-20261005.png"), full_page=True)
        page.get_by_role("button", name="Guardar cambios", exact=True).last.click()
        expect(page.get_by_text("Perfil guardado.", exact=False)).to_be_visible()
        saved = saves[-1]
        assert saved["experience_json"][:-1] == original["experience_json"], (name, len(saved["experience_json"]), len(original["experience_json"]), [(i, list(a) if isinstance(a, dict) else type(a).__name__, list(b) if isinstance(b, dict) else type(b).__name__) for i, (a, b) in enumerate(zip(saved["experience_json"], original["experience_json"])) if a != b])
        assert saved["econ_experience_json"] == original["econ_experience_json"]
        assert saved["experience_json"][-1]["porcentaje_participacion"] == 60
        assert saved["experience_json"][-1]["monto"] == 300000
        assert saved["team_json"] == original["team_json"]
    assert not errors, errors
    print(json.dumps({"viewports": 3, "preservation": "passed", "save_payload": "passed", "production_writes": 0}))
    browser.close()
