"""Bounded, profile-driven discovery; no tenant-wide historical sweep."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from buenapro_worker.jobs.poll_search import LIMA_TZ, canonical_hash, upsert_search_item
from buenapro_worker.prod6.screening import (
    POLICY_VERSION, decide, line_matches, normalize, prefilter, profile_snapshot, record_decision,
)
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.seace.schemas import SearchItem
from buenapro_worker.settings import Settings


def matches_profile_scope(description: str, lines: list[dict[str, Any]], segment: int) -> bool:
    """A phrase hit or two distinct configured words warrants document reading.

    This is discovery relevance, NOT evidence of legal/technical compliance.
    Matching stays within each business line instead of mixing unrelated words.
    """
    text = f" {normalize(description)} "
    return any(
        str(segment) in [str(value) for value in line.get("cubso_segmentos", [])]
        and line_matches(text, line)
        for line in lines
    )


def admit_contract(repo: JobRepository, item: SearchItem, *, anio: int, segment: int,
                   batch_id: str | None) -> dict[str, int]:
    """Make an accepted contract visible and queue its document reading.

    Existing candidates without extraction can be recovered, but complete
    documents must not re-run every 30 minutes.
    """
    changed = upsert_search_item(repo, item, anio=anio, segment=segment)
    extracted = None
    if not changed:
        extracted = repo.conn.execute(
            """SELECT 1 FROM tdr_extractions e
               JOIN contract_documents d ON d.id = e.contract_document_id
               WHERE d.id_contrato = %s AND e.is_current = true
                 AND e.quality != 'failed' LIMIT 1""",
            (item.id_contrato,),
        ).fetchone()
    enqueued = 0
    if not extracted:
        payload: dict[str, Any] = {"id_contrato": item.id_contrato, "segment": segment}
        if batch_id:
            payload["batch_id"] = batch_id
        job = repo.enqueue(
            "process_contract", payload, queue_name="io", priority=2,
            dedup_key=f"process_contract:{batch_id or 'profile'}:{item.id_contrato}:"
                      f"{canonical_hash(item.model_dump(mode='json', by_alias=True))}",
        )
        enqueued = int(job is not None)
    return {"changed": int(changed), "enqueued": enqueued}


def _search_pages(client: SeaceClient, *, year: int, objeto: int, segment: int,
                  max_pages: int):
    """Keep pagination sequential within one independent object/segment stream."""
    for page in range(1, max_pages + 1):
        response = client.search_contracts(
            anio=year, estado=2, objeto=objeto, segmento=segment, page=page, page_size=100,
        )
        total_pages = (
            response.pageable.total_elements + response.pageable.page_size - 1
        ) // response.pageable.page_size
        finished = not response.data or page >= total_pages
        capped = not finished and page == max_pages
        yield response, capped
        if finished:
            break


def poll_prod6_profile(
    settings: Settings,
    repo: JobRepository,
    *,
    profile_id: str,
    anio: int | None = None,
    max_pages_per_segment: int = 10,
    max_candidates: int = 200,
    batch_id: str | None = None,
    search_parallelism: int = 1,
) -> dict[str, int]:
    """Discover current-year vigente goods/services from active profile lines.

    Existing SEACE contracts remain global and deduplicated by official ID.
    Only profile-scoped candidates reach process_contract / LLM1. The ordinary
    worker subsequently matches other tenants as usual; no LLM2 is run here.
    """
    if max_pages_per_segment < 1 or max_candidates < 1:
        raise ValueError("Polling limits must be positive")
    if type(search_parallelism) is not int or not 1 <= search_parallelism <= 4:
        raise ValueError("Search parallelism must be an integer from 1 to 4")
    snapshot = profile_snapshot(repo.conn, profile_id)
    lines = snapshot["lines"]
    segments = sorted({int(value) for line in lines for value in line["cubso_segmentos"] if str(value).isdigit()})
    if not segments or not any(line.get("keywords") or line.get("keyword_phrases") or line.get("keyword_terms") for line in lines):
        raise ValueError("Profile needs active segments and keywords")
    year = anio or datetime.now(LIMA_TZ).year
    stats = {"seen": 0, "candidates": 0, "changed": 0, "enqueued": 0, "skipped": 0,
             "pages": 0, "capped_segments": 0, "limit_reached": 0,
             "http_requests": 0, "http_rows": 0,
             "decisions_recorded": 0, "decisions_reused": 0, "prefilter_rejected": 0}
    seen: set[int] = set()

    def apply_page(response, *, objeto: int, segment: int) -> bool:
        # This function is only called on the coordinator thread. Neither the
        # repository nor its PostgreSQL connection reaches HTTP worker threads.
        stats["pages"] += 1
        for item in response.data:
            stats["seen"] += 1
            passed, reasons, keyword_lines = prefilter(
                item, snapshot, year=year, now=datetime.now(timezone.utc), segment=segment)
            # PROD6 sometimes ignores anio: wrong-year/object rows are not stored.
            if (item.objeto_codigo != objeto
                    or {"object_out_of_scope", "publication_year_mismatch"} & set(reasons)):
                stats["skipped"] += 1
                continue
            decision, decided_by = decide(passed)
            stats["prefilter_rejected"] += int(not passed)
            contract_hash = canonical_hash(item.model_dump(mode="json", by_alias=True))
            recorded = record_decision(
                repo.conn, item=item, segment=segment, profile_id=profile_id,
                contract_hash=contract_hash, profile_hash=snapshot["profile_hash"],
                policy_version=POLICY_VERSION, prefilter_passed=passed,
                prefilter_reasons=reasons, keyword_lines=keyword_lines,
                decision=decision, decided_by=decided_by)
            stats["decisions_recorded" if recorded else "decisions_reused"] += 1
            # Only accepted contracts become seace_contracts / opportunities (the web
            # lists them); rejected and pending ones live in the decisions table alone.
            # Policy v2 never accepts here: JEV scoring accepts and admits later.
            if decision != "accepted":
                stats["skipped"] += 1
                continue
            if item.id_contrato in seen:
                continue
            seen.add(item.id_contrato)
            stats["candidates"] += 1
            admitted = admit_contract(repo, item, anio=year, segment=segment, batch_id=batch_id)
            stats["changed"] += admitted["changed"]
            stats["enqueued"] += admitted["enqueued"]
            # Preserved historical readings count toward this same bound too.
            if stats["candidates"] >= max_candidates:
                stats["limit_reached"] = 1
                return True
        return False

    streams = [(objeto, segment) for objeto in (1, 2) for segment in segments]
    if search_parallelism == 1:
        # Default retains the original lazy traversal and shared serial client:
        # it does not prefetch other streams after reaching the candidate bound.
        with SeaceClient(settings) as client:
            for objeto, segment in streams:
                for response, capped in _search_pages(
                    client, year=year, objeto=objeto, segment=segment,
                    max_pages=max_pages_per_segment,
                ):
                    stats["http_requests"] += 1
                    stats["http_rows"] += len(response.data)
                    stats["capped_segments"] += int(capped)
                    if apply_page(response, objeto=objeto, segment=segment):
                        return stats
    else:
        def fetch_stream(stream):
            objeto, segment = stream
            with SeaceClient(settings) as client:
                pages = list(_search_pages(
                    client, year=year, objeto=objeto, segment=segment,
                    max_pages=max_pages_per_segment,
                ))
            return objeto, segment, pages

        # HTTP-only work finishes before any write; preserve deterministic stream
        # order so varying network timings cannot change which candidates fit.
        with ThreadPoolExecutor(max_workers=search_parallelism) as executor:
            queried = list(executor.map(fetch_stream, streams))
        stats["http_requests"] = sum(len(pages) for _, _, pages in queried)
        stats["http_rows"] = sum(len(page.data) for _, _, pages in queried for page, _ in pages)
        stats["capped_segments"] = sum(int(capped) for _, _, pages in queried for _, capped in pages)
        for objeto, segment, pages in queried:
            for response, _ in pages:
                if apply_page(response, objeto=objeto, segment=segment):
                    return stats
    return stats
