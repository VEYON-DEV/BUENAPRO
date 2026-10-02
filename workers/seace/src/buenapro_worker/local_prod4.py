"""Laptop-only official document reader; production DB stays behind an SSH tunnel.

No stealth, CAPTCHA solving, invented URLs, or database export. Failed documents
remain local for retry; successful temporaries are removed only after read-back.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import socket
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from buenapro_worker.extraction.gemini import GeminiExtractor
from buenapro_worker.jobs.prod4_documents import (
    _profile_has_relevant_fit,
    extract_prod4_document_job,
    select_official_requirements_pdf,
)
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.prod4.client import DocumentTooLargeError
from buenapro_worker.settings import Settings


class BrowserAccessError(RuntimeError):
    """Normal portal navigation was denied; no automatic bypass or repeated retries."""


def tunnel_url(original: str, port: int) -> str:
    parsed = urlsplit(original)
    if parsed.scheme not in {"postgres", "postgresql"} or "@" not in parsed.netloc:
        raise ValueError("Expected PostgreSQL URL with credentials")
    credentials = parsed.netloc.rsplit("@", 1)[0]
    return urlunsplit((parsed.scheme, f"{credentials}@127.0.0.1:{port}", parsed.path, parsed.query, ""))


@contextmanager
def production_settings(args):
    ssh = ["ssh", "-i", str(Path(args.ssh_key).resolve()), "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    # Keep secret values in memory, over encrypted SSH; never print subprocess output.
    result = subprocess.run(ssh + [args.ssh_host, "docker inspect --format '{{json .Config.Env}}' docker-worker-llm-1"],
                            capture_output=True, text=True, check=True, timeout=30)
    remote = dict(item.split("=", 1) for item in json.loads(result.stdout) if "=" in item)
    with socket.socket() as candidate:
        candidate.bind(("127.0.0.1", 0))
        port = candidate.getsockname()[1]
    tunnel = subprocess.Popen(ssh + ["-N", "-o", "ExitOnForwardFailure=yes", "-L", f"127.0.0.1:{port}:127.0.0.1:5432", args.ssh_host],
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            if tunnel.poll() is not None:
                raise RuntimeError("SSH tunnel failed")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("SSH tunnel did not become ready")
        overrides = {key.lower(): value for key, value in remote.items()
                     if key.startswith("PROD4_") or key in {"GEMINI_MODEL", "GEMINI_FALLBACK_MODEL"}}
        # API key remains the existing local .env.local key, not a printed/exported secret.
        yield Settings(database_url=tunnel_url(remote["DATABASE_URL"], port), **overrides)
    finally:
        tunnel.terminate()
        try:
            tunnel.wait(timeout=10)
        except subprocess.TimeoutExpired:
            tunnel.kill()
            tunnel.wait()


class OfficialBrowserDownload:
    def __init__(self, browser, procedure_id: int, directory: Path):
        self.browser = browser
        self.procedure_id = procedure_id
        self.directory = directory
        self.path: Path | None = None
        self.sha256: str | None = None

    def download_document(self, code: UUID, *, max_bytes: int) -> bytes:
        context = self.browser.new_context(accept_downloads=True, locale="es-PE")
        try:
            page = context.new_page()
            response = page.goto(f"https://prod4.seace.gob.pe/openegocio/#/ficha/idProceso/{self.procedure_id}",
                                 wait_until="domcontentloaded", timeout=45000)
            if response and response.status >= 400:
                raise BrowserAccessError(f"Portal denied navigation: HTTP {response.status}")
            # The href must actually exist in the rendered official ficha. We do
            # not navigate directly to a guessed document URL or forge headers.
            link = page.locator(f'a[href*="fileCode={code}"]')
            link.wait_for(state="visible", timeout=30000)
            if link.count() != 1:
                raise ValueError("Official document link is ambiguous")
            observed = urlsplit(link.get_attribute("href") or "")
            if observed.scheme != "https" or observed.hostname != "prod1.seace.gob.pe" or observed.path != "/SeaceWeb-PRO/SdescargarArchivoAlfresco":
                raise ValueError("Unexpected official document destination")
            with page.expect_download(timeout=45000) as event:
                link.click(timeout=10000)
            download = event.value
            if download.failure():
                raise ValueError("Official browser download failed")
            self.path = self.directory / "official-document.pdf"
            download.save_as(str(self.path))
            if self.path.stat().st_size > max_bytes:
                raise DocumentTooLargeError("PDF exceeds configured analysis size limit")
            content = self.path.read_bytes()
            if not content.startswith(b"%PDF-"):
                raise ValueError("Official document is not a PDF")
            # Parse independently before spending Gemini tokens.
            subprocess.run(["pdfinfo", str(self.path)], capture_output=True, check=True, timeout=30)
            self.sha256 = hashlib.sha256(content).hexdigest()
            return content
        finally:
            context.close()


class BudgetedExtractor:
    def __init__(self, settings: Settings, budget: float):
        self.inner = GeminiExtractor(settings)
        self.budget = budget

    def extract(self, content: bytes, **kwargs):
        tokens = self.inner.count_tokens(content)
        # Conservative reserve for primary/retry/fallback using existing prices;
        # final cost is persisted by the existing worker. Not a billing guarantee.
        reserve = 3 * (tokens * 0.30 + 24576 * 2.50) / 1_000_000
        if reserve > self.budget:
            raise ValueError("Estimated retry reserve exceeds remaining extraction budget")
        return self.inner.extract(content, **kwargs)


def run(settings: Settings, args) -> dict:
    from playwright.sync_api import sync_playwright

    report = {"eligible": 0, "processed": 0, "verified": 0, "failed": 0, "cost_usd": 0.0, "items": []}
    args.work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn, sync_playwright() as pw:
        conn.execute("SET TIME ZONE 'UTC'")
        rows = conn.execute("""SELECT id_procedimiento, analyzed_document_code, document_analysis_reason FROM prod4_processes
            WHERE technology_relevant = true AND missing_since IS NULL
              AND object_type IN ('good', 'service')
            ORDER BY id_procedimiento DESC""").fetchall()
        repo = JobRepository(conn)
        candidates = []
        for row in rows:
            pid = int(row["id_procedimiento"])
            if args.process_id and pid != args.process_id:
                continue
            docs = conn.execute("SELECT * FROM prod4_documents WHERE id_procedimiento = %s", (pid,)).fetchall()
            selected = select_official_requirements_pdf([dict(doc) for doc in docs])
            if selected is None or not _profile_has_relevant_fit(repo, pid):
                continue
            if row["analyzed_document_code"] == selected["codigo_alfresco"] and row["document_analysis_reason"] == "pdf_exceeds_analysis_limit":
                continue  # Known oversized source needs explicit size-policy review, not repeated downloads.
            extracted = conn.execute("""SELECT id FROM prod4_document_extractions
                WHERE id_procedimiento = %s AND codigo_alfresco = %s AND is_current
                  AND quality <> 'failed'""", (pid, selected["codigo_alfresco"])).fetchone()
            if not extracted:
                candidates.append((pid, selected["codigo_alfresco"]))
        report["eligible"] = len(candidates)
        conn.commit()
        if not args.execute:
            report["pending_ids"] = [pid for pid, _ in candidates]
            return report
        browser = pw.chromium.launch(headless=args.headless, chromium_sandbox=True)
        try:
            for pid, code in candidates[:args.limit]:
                directory = Path(tempfile.mkdtemp(prefix=f"prod4-{pid}-", dir=args.work_dir))
                downloader = OfficialBrowserDownload(browser, pid, directory)
                try:
                    conn.execute("SELECT pg_advisory_xact_lock(hashtext('prod4_document_daily_budget'))")
                    today = conn.execute("""SELECT count(*) AS total FROM prod4_document_extractions
                        WHERE created_at >= date_trunc('day', now())""").fetchone()["total"]
                    if today >= settings.prod4_document_daily_limit:
                        conn.rollback()
                        directory.rmdir()
                        report["stop_reason"] = "daily_extraction_limit"
                        break
                    conn.execute("SELECT id_procedimiento FROM prod4_processes WHERE id_procedimiento = %s FOR UPDATE", (pid,))
                    result = extract_prod4_document_job(settings, repo, id_procedimiento=pid,
                        codigo_alfresco=str(code), client=downloader,
                        extractor=BudgetedExtractor(settings, args.max_cost_usd - report["cost_usd"]))
                    conn.commit()
                    report["processed"] += 1
                    if result.get("extraction_id"):
                        saved = conn.execute("""SELECT sha256_original, cost_usd, summary_json
                            FROM prod4_document_extractions WHERE id = %s AND is_current
                              AND id_procedimiento = %s AND codigo_alfresco = %s""",
                            (result["extraction_id"], pid, code)).fetchone()
                        conn.commit()
                        if not saved or (downloader.sha256 and saved["sha256_original"] != downloader.sha256) or not saved["summary_json"]:
                            raise RuntimeError("Durable extraction verification failed; keeping PDF")
                        report["cost_usd"] += float(saved["cost_usd"] or 0)
                        report["verified"] += 1
                        if downloader.path:
                            downloader.path.unlink()
                        directory.rmdir()
                    else:
                        if downloader.path is None:
                            directory.rmdir()
                        else:
                            result["retained_directory"] = str(directory)
                            result["downloaded_bytes"] = downloader.path.stat().st_size
                    report["items"].append({"id_procedimiento": pid, **result})
                    print(json.dumps({"id_procedimiento": pid, **result}), flush=True)
                except Exception as exc:
                    conn.rollback()
                    report["failed"] += 1
                    # Do not stringify exceptions: connection/model errors can contain secrets.
                    report["items"].append({"id_procedimiento": pid, "error_type": type(exc).__name__,
                                            "retained_directory": str(directory)})
                    if args.stop_on_error:
                        break
        finally:
            browser.close()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-key", required=True)
    parser.add_argument("--execute", action="store_true", help="Default is read-only inventory")
    parser.add_argument("--process-id", type=int)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--max-cost-usd", type=float, default=1.0, help="Extraction budget only; production match retains its own daily cap")
    parser.add_argument("--headless", action="store_true", help="Default normal headed Chromium")
    parser.add_argument("--stop-on-error", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--work-dir", type=Path, default=Path("tmp/prod4-local"))
    args = parser.parse_args()
    if args.limit < 1 or args.max_cost_usd <= 0:
        parser.error("Positive limit and budget required")
    with production_settings(args) as settings:
        report = run(settings, args)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
