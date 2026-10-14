"""Isolated public-browser diagnostic. No DB, credentials, CAPTCHA or LLM.

Run once to inspect the actual DOM, then pass a visible --download-text.
Only download using the observed official UI; never retry a denied request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


def verify_and_extract(pdf: Path) -> dict:
    """Validate with Poppler, then extract text; no LLM or production writes."""
    info = subprocess.run(["pdfinfo", str(pdf)], check=True, capture_output=True, text=True, timeout=30)
    pages = next((int(line.split(":", 1)[1].strip()) for line in info.stdout.splitlines()
                  if line.startswith("Pages:")), None)
    text_path = pdf.with_suffix(".txt")
    subprocess.run(["pdftotext", "-layout", str(pdf), str(text_path)], check=True,
                   capture_output=True, timeout=60)
    text = text_path.read_text(encoding="utf-8")
    return {"pages": pages, "text_path": str(text_path), "characters": len(text),
            "needs_ocr_review": len(text.strip()) < 100,
            "tdr_heading_found": "TÉRMINOS DE REFERENCIA" in text.upper()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--download-text")
    parser.add_argument("--download-href", help="Exact href of a previously observed visible official link")
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--extract-text", action="store_true", help="Validate downloaded PDF and extract via installed Poppler")
    parser.add_argument("--document-url", help="Observed official Alfresco link, diagnostic navigation only")
    args = parser.parse_args()
    if args.download_text and args.download_href:
        parser.error("Choose one observed download control")
    if args.download_href:
        link = urlsplit(args.download_href)
        if link.scheme != "https" or link.hostname != "prod1.seace.gob.pe" or link.path != "/SeaceWeb-PRO/SdescargarArchivoAlfresco":
            parser.error("Only an observed official Alfresco link is in scope")
    if urlsplit(args.url).hostname != "prod4.seace.gob.pe":
        parser.error("Only the public PROD4 portal is in scope")
    if args.document_url:
        document = urlsplit(args.document_url)
        if document.scheme != "https" or document.hostname != "prod1.seace.gob.pe" or document.path != "/SeaceWeb-PRO/SdescargarArchivoAlfresco":
            parser.error("Only an observed official Alfresco URL is in scope")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"entry_url": args.url, "network": [], "errors": []}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed, chromium_sandbox=True)
        context = browser.new_context(accept_downloads=True, locale="es-PE", viewport={"width": 1440, "height": 1000})
        page = context.new_page()

        def response_received(response):
            host = urlsplit(response.url).hostname or ""
            if host == "seace.gob.pe" or host.endswith(".seace.gob.pe"):
                report["network"].append({"url": response.url, "status": response.status,
                    "type": response.headers.get("content-type", ""), "resource": response.request.resource_type})

        context.on("response", response_received)
        page.on("requestfailed", lambda request: report["errors"].append({"url": request.url, "error": request.failure}))
        try:
            result = page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            report["navigation_status"] = result.status if result else None
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass  # A polling page need not become idle; inspect current DOM.
            report["url"] = page.url
            report["title"] = page.title()
            report["body"] = page.locator("body").inner_text()[:16000]
            report["controls"] = page.locator("a, button, [role=button], [mattooltip]").evaluate_all(
                "els => els.filter(e => e.getBoundingClientRect().width && e.getBoundingClientRect().height).map(e => ({tag:e.tagName,text:e.innerText,title:e.getAttribute('title'),tooltip:e.getAttribute('mattooltip'),href:e.getAttribute('href')})).slice(0,100)")
            page.screenshot(path=str(out / "page.png"), full_page=True)
            if args.document_url:
                # Known public link from the API. No guessed routes or headers.
                try:
                    with page.expect_download(timeout=15000) as event:
                        try:
                            document_response = page.goto(args.document_url, wait_until="domcontentloaded", timeout=30000)
                            report["document_status"] = document_response.status if document_response else None
                        except Exception as error:
                            if "Download is starting" not in str(error):
                                raise
                    download = event.value
                    report["download"] = {"url": download.url, "filename": download.suggested_filename, "failure": download.failure()}
                    if not report["download"]["failure"]:
                        download.save_as(str(out / "official-document.pdf"))
                        content = (out / "official-document.pdf").read_bytes()
                        report["download"].update({"bytes":len(content), "is_pdf":content.startswith(b"%PDF-"), "sha256":hashlib.sha256(content).hexdigest()})
                except Exception as error:
                    report["document_error"] = str(error)[:700]
                    report["document_body"] = page.locator("body").inner_text()[:2000]
                page.screenshot(path=str(out / "document.png"), full_page=True)
            if args.download_text or args.download_href:
                # Exact selector must come from a previously observed control.
                if args.download_href:
                    # JSON quoting prevents selector injection; no invented button.
                    target = page.locator("a[href=" + json.dumps(args.download_href) + "]")
                else:
                    target = page.get_by_text(args.download_text, exact=True)
                if target.count() != 1:
                    raise ValueError("Observed download control is not unique")
                if not target.is_visible():
                    raise ValueError("Observed download control is not visible")
                with page.expect_download(timeout=25000) as event:
                    target.click(timeout=10000)
                download = event.value
                error = download.failure()
                report["download"] = {"url": download.url, "filename": download.suggested_filename, "failure": error}
                if not error:
                    destination = out / "official-document.pdf"
                    download.save_as(str(destination))
                    content = destination.read_bytes()
                    report["download"].update({"bytes": len(content), "is_pdf": content.startswith(b"%PDF-"),
                        "sha256": hashlib.sha256(content).hexdigest()})
                page.screenshot(path=str(out / "after-download.png"), full_page=True)
            if args.extract_text and report.get("download", {}).get("is_pdf"):
                report["extraction"] = verify_and_extract(out / "official-document.pdf")
        except Exception as error:
            report["error"] = str(error)[:1200]
            try:
                page.screenshot(path=str(out / "error.png"), full_page=True)
                report["body_after_error"] = page.locator("body").inner_text()[:6000]
                report["popups"] = [{"url": popup.url, "body": popup.locator("body").inner_text(timeout=3000)[:3000]}
                    for popup in context.pages if popup != page and not popup.is_closed()]
            except Exception:
                pass
        finally:
            (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            context.close()
            browser.close()


if __name__ == "__main__":
    main()
