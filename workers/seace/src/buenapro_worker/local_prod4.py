"""Laptop-only official document reader; production DB stays behind an SSH tunnel.

No stealth, CAPTCHA solving, invented URLs, or database export. Failed documents
remain local for retry; successful temporaries are removed only after read-back.
"""
from __future__ import annotations

import argparse
import hashlib
import io
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
from buenapro_worker.jobs.prod4_match import route_prod4_profiles_job
from google.genai import types


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


class LocalFileExtractor(GeminiExtractor):
    """Large originals use Files API, never resized/cropped PDF derivatives."""

    def __init__(self, settings: Settings):
        super().__init__(settings)
        self.uploaded = None
        self.uploaded_sha = None

    def _contents(self, pdf_bytes: bytes, *, mime: str):
        if len(pdf_bytes) <= 18 * 1024 * 1024:
            return super()._contents(pdf_bytes, mime=mime)
        if mime != "application/pdf" or len(pdf_bytes) > 100_000_000:
            raise ValueError("Large PDF exceeds supported Files API policy")
        digest = hashlib.sha256(pdf_bytes).hexdigest()
        if self.uploaded is None:
            from buenapro_worker.local_pdf_transport import partition_pdf
            chunks = partition_pdf(pdf_bytes) if len(pdf_bytes) > 50_000_000 else None
            self.uploaded_parts = []
            for chunk in chunks or [None]:
                uploaded = self.client.files.upload(file=io.BytesIO(chunk.content if chunk else pdf_bytes), config={"mime_type": mime})
                self.uploaded_parts.append((uploaded, chunk))
                self.uploaded = self.uploaded or uploaded
            self.uploaded_sha = digest
        if digest != self.uploaded_sha:
            raise ValueError("Extractor instance cannot reuse another document")
        parts = []
        for uploaded, chunk in self.uploaded_parts:
            for _ in range(60):
                state = getattr(uploaded.state, "name", uploaded.state)
                if state == "ACTIVE":
                    if chunk:
                        parts.append(types.Part.from_text(text=f"Parte del MISMO documento original: páginas {chunk.start_page}–{chunk.end_page}. Cualquier cita debe usar la numeración original (offset {chunk.page_offset}), no tratar las partes como convocatorias distintas. Lee TODAS las partes para producir una sola extracción."))
                    parts.append(types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime))
                    break
                if state != "PROCESSING":
                    raise RuntimeError("Uploaded PDF is not usable")
                time.sleep(0.5)
                uploaded = self.client.files.get(name=uploaded.name)
            else:
                raise TimeoutError("PDF processing timed out")
        return parts

    def close(self):
        if self.uploaded is not None:
            for uploaded, _ in self.uploaded_parts:
                self.client.files.delete(name=uploaded.name)
            self.uploaded = None


class BudgetedExtractor:
    def __init__(self, settings: Settings, budget: float):
        self.inner = LocalFileExtractor(settings)
        self.budget = budget

    def extract(self, content: bytes, **kwargs):
        try:
            tokens = self.inner.count_tokens(content)
            # Conservative reserve for primary/retry/fallback using existing prices;
            # final cost is persisted by the existing worker. Not a billing guarantee.
            reserve = 3 * (tokens * 0.30 + 24576 * 2.50) / 1_000_000
            if reserve > self.budget:
                raise ValueError("Estimated retry reserve exceeds remaining extraction budget")
            return self.inner.extract(content, **kwargs)
        finally:
            self.inner.close()


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
            if row["analyzed_document_code"] == selected["codigo_alfresco"] and row["document_analysis_reason"] == "pdf_exceeds_analysis_limit" and settings.prod4_max_analysis_pdf_bytes <= 20 * 1024 * 1024:
                continue  # Known oversized source needs explicit size-policy review, not repeated downloads.
            extracted = conn.execute("""SELECT id, requires_human_review FROM prod4_document_extractions
                WHERE id_procedimiento = %s AND codigo_alfresco = %s AND is_current
                  AND quality <> 'failed'""", (pid, selected["codigo_alfresco"])).fetchone()
            if not extracted or (args.retry_review and extracted["requires_human_review"]):
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
                        extractor=BudgetedExtractor(settings, args.max_cost_usd - report["cost_usd"]),
                        enqueue_matching=not args.extraction_only)
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


def route_matches(settings: Settings, args) -> dict:
    """Enqueue normal production jobs; no local scoring or tenant-policy changes."""
    totals = {"documents": 0, "enqueued": 0, "limited": 0, "failed": 0}
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        rows = conn.execute("""SELECT e.id, e.id_procedimiento FROM prod4_document_extractions e
            JOIN prod4_processes p USING(id_procedimiento)
            WHERE e.is_current AND NOT e.requires_human_review AND e.quality <> 'failed'
              AND p.technology_relevant AND p.missing_since IS NULL
              AND p.document_analysis_status = 'extracted'
            ORDER BY e.id LIMIT %s""", (args.limit,)).fetchall()
        conn.commit()
        for row in rows:
            result = route_prod4_profiles_job(settings, JobRepository(conn),
                id_procedimiento=row["id_procedimiento"], extraction_id=row["id"])
            conn.commit()
            totals["documents"] += 1
            totals["enqueued"] += result["enqueued"]
            totals["limited"] += result["limited"]
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ssh-host", required=True)
    parser.add_argument("--ssh-key", required=True)
    parser.add_argument("--execute", action="store_true", help="Default is read-only inventory")
    parser.add_argument("--process-id", type=int)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--daily-limit", type=int, help="Explicit one-run extraction count override; does not modify production configuration")
    parser.add_argument("--max-pdf-bytes", type=int, help="Explicit local size override, at most 100000000 bytes; per-part Files API size is bounded")
    parser.add_argument("--retry-review", action="store_true", help="Consider the current review extraction for an explicit process; normal versioned idempotence still applies")
    parser.add_argument("--route-matches", action="store_true", help="Only enqueue normal production matching for current validated documents")
    parser.add_argument("--extraction-only", action="store_true", help="Persist document reading only; do not enqueue profile evaluation")
    parser.add_argument("--profile-daily-limit", type=int, help="One-run default matching count override; explicit tenant rules still win")
    parser.add_argument("--max-cost-usd", type=float, default=1.0, help="Extraction budget only; production match retains its own daily cap")
    parser.add_argument("--headless", action="store_true", help="Default normal headed Chromium")
    parser.add_argument("--stop-on-error", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--work-dir", type=Path, default=Path("tmp/prod4-local"))
    args = parser.parse_args()
    if args.limit < 1 or args.max_cost_usd <= 0:
        parser.error("Positive limit and budget required")
    if args.daily_limit is not None and not 1 <= args.daily_limit <= 100:
        parser.error("One-run daily limit must be between 1 and 100")
    if args.max_pdf_bytes is not None and not 1 <= args.max_pdf_bytes <= 100_000_000:
        parser.error("PDF size must be between 1 and 100000000 bytes")
    if args.retry_review and not args.process_id:
        parser.error("Review retry requires an explicit process ID")
    if args.route_matches and not args.execute:
        parser.error("Routing matching writes jobs and requires --execute")
    if args.extraction_only and args.route_matches:
        parser.error("Extraction-only cannot route profile matching")
    if args.profile_daily_limit is not None and not (args.route_matches and 1 <= args.profile_daily_limit <= 100):
        parser.error("Profile limit requires routing mode and must be between 1 and 100")
    with production_settings(args) as settings:
        if args.daily_limit is not None:
            settings = settings.model_copy(update={"prod4_document_daily_limit": args.daily_limit})
        if args.max_pdf_bytes is not None:
            settings = settings.model_copy(update={"prod4_max_analysis_pdf_bytes": args.max_pdf_bytes})
        if args.profile_daily_limit is not None:
            settings = settings.model_copy(update={"prod4_profile_evaluations_daily_default": args.profile_daily_limit})
        report = route_matches(settings, args) if args.route_matches else run(settings, args)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    if report["failed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
