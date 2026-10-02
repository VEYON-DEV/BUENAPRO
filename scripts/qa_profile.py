"""Profile UI QA: real read-only data, intercepted writes/uploads (no production mutations)."""
from pathlib import Path
import json
from playwright.sync_api import sync_playwright, expect

out = Path("docs/new-style/qa")
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as pw:
    browser = pw.chromium.launch()
    page = browser.new_page()
    errors, saves, uploads = [], [], []
    fail_save = False
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: dialog.accept())
    original = page.request.get("http://127.0.0.1:3000/api/profile").json()["data"]
    def intercept(route):
        request = route.request
        if request.method == "GET":
            route.continue_()
        elif request.url.endswith("/api/profile") and request.method == "PUT":
            if fail_save:
                route.fulfill(status=503, content_type="application/json", body='{"error":"QA: reintenta guardar"}')
                return
            payload = request.post_data_json
            saves.append(payload)
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"data": {**original, **payload}}))
        elif request.url.endswith("/api/profile/library/documents") and request.method == "POST":
            uploads.append(request.post_data)
            route.fulfill(status=201, content_type="application/json", body=json.dumps({"data": {"id": "qa-document", "title": "Certificado QA", "filename": "qa.pdf"}}))
        else:
            route.fulfill(status=500, content_type="application/json", body='{"error":"QA blocks all real writes"}')
    page.route("**/api/**", intercept)
    for name, width, height in [("desktop", 1600, 1000), ("laptop", 1024, 900), ("mobile", 390, 844)]:
        page.set_viewport_size({"width": width, "height": height})
        page.goto("http://127.0.0.1:3000/perfil", wait_until="networkidle")
        expect(page.get_by_role("heading", name="Perfil de empresa", exact=True)).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        page.screenshot(path=str(out / f"profile-radar-{name}-20261002.png"), full_page=name != "mobile")
        page.get_by_role("button", name="Equipo", exact=True).press("Enter")
        expect(page.get_by_role("heading", name="Equipo profesional", exact=True)).to_be_visible()
        page.screenshot(path=str(out / f"profile-team-{name}-20261002.png"), full_page=name != "mobile")
        if name == "mobile":
            page.get_by_role("heading", name="Equipo profesional", exact=True).scroll_into_view_if_needed()
            page.evaluate("window.scrollBy(0, 170)")
            page.screenshot(path=str(out / "profile-team-list-mobile-20261002.png"))
        page.get_by_role("button", name="Agregar profesional", exact=True).click()
        page.get_by_label("Rol o especialidad", exact=False).fill("Arquitecto de soluciones y especialista en sistemas de información")
        page.get_by_label("Nombre del profesional", exact=True).fill("Profesional QA")
        page.get_by_label("Grado o título académico", exact=True).fill("Ingeniero")
        page.get_by_label("Carrera o formación", exact=True).fill("Ingeniería de sistemas")
        page.get_by_label("Años de experiencia", exact=True).fill("8")
        page.get_by_role("button", name="Agregar certificado", exact=True).click()
        page.get_by_label("Certificación o capacitación", exact=False).fill("Certificación QA")
        page.get_by_label("Institución emisora", exact=True).fill("Institución QA")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), name
        page.screenshot(path=str(out / f"profile-editor-{name}-20261002.png"), full_page=name != "mobile")
        if name == "desktop":
            page.get_by_label("Subir respaldo de Certificación QA", exact=True).set_input_files({"name": "qa.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4 QA"})
            expect(page.get_by_role("link", name="Certificado QA", exact=True)).to_be_visible()
            assert "usableForApplications" in uploads[0] and "on" in uploads[0]
            page.get_by_role("button", name="Experiencia", exact=True).click()
            page.get_by_role("button", name="Guardar cambios", exact=True).first.click()
            expect(page.get_by_text("Perfil guardado.", exact=False)).to_be_visible()
            payload = saves[-1]
            assert payload["team_json"][:-1] == original["team_json"]
            assert payload["experience_json"] == original["experience_json"]
            assert payload["certifications_json"] == original["certifications_json"]
            professional = payload["team_json"][-1]
            assert professional["experiencia_anios"] == 8
            assert professional["certifications"][0]["documents"][0]["id"] == "qa-document"
            page.get_by_role("button", name="Equipo", exact=True).click()
            page.get_by_role("button", name="Agregar profesional", exact=True).click()
            page.get_by_role("button", name="Experiencia", exact=True).click()
            before = len(saves)
            page.get_by_role("button", name="Guardar cambios", exact=True).first.click()
            expect(page.get_by_text("Completa el nombre, rol u objeto", exact=False)).to_be_visible()
            assert len(saves) == before
    page.goto("http://127.0.0.1:3000/perfil#team", wait_until="networkidle")
    import re
    while page.get_by_role("button", name=re.compile(r"^Eliminar ")).count():
        page.get_by_role("button", name=re.compile(r"^Eliminar ")).first.click()
    expect(page.get_by_text("Todavía no hay registros.", exact=False).first).to_be_visible()
    page.get_by_role("button", name="Agregar profesional", exact=True).click()
    page.get_by_label("Rol o especialidad", exact=False).fill("Prueba de reintento")
    fail_save = True
    page.get_by_role("button", name="Guardar cambios", exact=True).first.click()
    expect(page.get_by_text("QA: reintenta guardar", exact=True)).to_be_visible()
    fail_save = False
    page.get_by_role("button", name="Guardar cambios", exact=True).first.click()
    expect(page.get_by_text("Perfil guardado.", exact=False)).to_be_visible()
    assert not errors, errors
    print(json.dumps({"viewports": 3, "screenshots": 9, "preservation": "passed", "upload_mock": "passed", "hidden_validation": "passed", "errors": errors}))
    browser.close()
