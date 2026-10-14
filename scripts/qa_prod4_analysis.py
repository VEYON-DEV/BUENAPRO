"""Local real-data render; all analysis POST/GET mocked to prevent writes/LLM costs."""
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
    mode = {"status": "claimed", "post_error": False, "posts": 0}
    def analysis(route):
        if route.request.method == "POST":
            mode["posts"] += 1
            assert json.loads(route.request.post_data)["force"] is True
            if mode["post_error"]:
                return route.fulfill(status=409, json={"error": "Completa un perfil activo de empresa antes de analizar."})
        route.fulfill(json={"data": {"job_id": "123", "status": mode["status"]}})
    page.route("**/api/prod4/opportunities/*/analyze*", analysis)
    for name, width, height in [("desktop",1440,1000),("laptop",1024,900),("mobile",390,844)]:
        page.set_viewport_size({"width": width,"height": height})
        page.goto("http://127.0.0.1:3000/oportunidad/seace/1242359", wait_until="networkidle")
        page.wait_for_timeout(700)
        button = page.get_by_role("button",name="Volver a analizar con IA",exact=True)
        assert button.is_enabled()
        official = page.get_by_role("link",name="Revisar en SEACE",exact=True)
        assert official.get_attribute("href").endswith("/ficha/idProceso/1242359")
        assert official.get_attribute("target") == "_blank"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), name
        page.screenshot(path=str(out/f"prod4-analyze-{name}-20261005.png"),full_page=name != "mobile")
    page.set_viewport_size({"width":1440,"height":1000})
    page.goto("http://127.0.0.1:3000/oportunidad/seace/1242359",wait_until="networkidle")
    page.wait_for_timeout(700)
    page.get_by_role("button",name="Volver a analizar con IA",exact=True).press("Enter")
    assert page.get_by_role("button",name="Analizando con IA…",exact=True).is_disabled()
    page.get_by_text("La IA está evaluando tu perfil.",exact=False).wait_for()
    page.screenshot(path=str(out/"prod4-analyze-loading-20261005.png"),full_page=True)
    assert mode["posts"] == 1
    mode["status"] = "done"
    page.get_by_text("Análisis completado.",exact=False).wait_for()
    mode["post_error"] = True
    page.get_by_role("button",name="Volver a analizar con IA",exact=True).click()
    page.get_by_text("Completa un perfil activo de empresa",exact=False).wait_for()
    assert page.get_by_role("button",name="Volver a analizar con IA",exact=True).is_enabled()
    page.screenshot(path=str(out/"prod4-analyze-error-20261005.png"),full_page=True)
    page.goto("http://127.0.0.1:3000/oportunidad/seace/1246394",wait_until="networkidle")
    page.wait_for_timeout(700)
    assert page.get_by_role("button",name="Analizar con IA",exact=True).is_disabled()
    assert page.get_by_text("Primero deben procesarse y validarse las bases oficiales.",exact=True).is_visible()
    page.screenshot(path=str(out/"prod4-analyze-missing-bases-20261005.png"),full_page=True)
    assert not errors,errors
    print(json.dumps({"screenshots":6,"viewports":3,"mocked_writes":mode["posts"],"errors":errors}))
    browser.close()
