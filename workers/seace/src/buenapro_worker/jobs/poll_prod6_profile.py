"""Bounded, profile-driven discovery; no tenant-wide historical sweep."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

from buenapro_worker.jobs.poll_search import LIMA_TZ, upsert_search_item
from buenapro_worker.prod6.screening import (
    KEYWORD_SEGMENT, POLICY_VERSION, decide, line_matches, listing_hash, normalize, prefilter,
    profile_snapshot, record_decision,
)
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.seace.pacing import source_stop
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
            dedup_key=f"process_contract:{batch_id or 'profile'}:{item.id_contrato}:{listing_hash(item)}",
        )
        enqueued = int(job is not None)
    return {"changed": int(changed), "enqueued": enqueued}


class DiscoveryLimit(Exception):
    """The cycle's discovery request limit is spent; the stream resumes in the next cycle."""


def _search_pages(client: SeaceClient, *, year: int, objeto: int, segment: int,
                  max_pages: int, reserve=None):
    """Keep pagination sequential within one independent object/segment stream.

    ``reserve`` asks for permission before every request (the discovery limit).
    """
    for page in range(1, max_pages + 1):
        if reserve is not None and not reserve():
            raise DiscoveryLimit()
        response = client.search_contracts(
            anio=year, estado=2, objeto=objeto, page=page, page_size=100,
            segmento=None if segment == KEYWORD_SEGMENT else segment,
        )
        total_pages = (
            response.pageable.total_elements + response.pageable.page_size - 1
        ) // response.pageable.page_size
        finished = not response.data or page >= total_pages
        capped = not finished and page == max_pages
        yield response, capped
        if finished:
            break


def _mark_stop(stats: dict[str, int], error: BaseException) -> None:
    """Record why the sweep ended early (a 403/429 block, the cycle budget or the discovery limit)."""
    if isinstance(error, DiscoveryLimit):
        stats["discovery_limit_reached"] = 1
    else:
        stats["source_blocked" if source_stop(error) == "blocked" else "budget_reached"] = 1


PROFILE_KEYS = ("seen", "candidates", "changed", "enqueued", "skipped",
                "decisions_recorded", "decisions_reused", "prefilter_rejected", "keyword_misses")


def poll_prod6_profiles(
    settings: Settings,
    repo: JobRepository,
    *,
    profile_ids: list[str],
    anio: int | None = None,
    max_pages_per_segment: int = 10,
    max_candidates: int = 200,
    batch_id: str | None = None,
    search_parallelism: int = 1,
    max_requests: int | None = None,
    start_stream: int = 0,
    max_streams: int | None = None,
    keyword_streams: bool = False,
    max_keyword_pages: int | None = None,
) -> dict[str, Any]:
    """One search sweep for every profile: each (object, segment) stream is read once.

    The streams are the object x segment pairs of the UNION of the profiles' segments.
    Every result page feeds the screening decision of EACH profile whose segments include
    the stream's segment (a profile without that segment gets no row for the item), with
    the same prefilter/record_decision semantics as a single-profile sweep.

    ``keyword_streams`` appends one nationwide stream per object (``KEYWORD_SEGMENT``, no
    segment filter, up to ``max_keyword_pages`` pages). There an item only reaches a profile's
    screening when its title names one of that profile's business lines; anything else gets no
    decision at all, so the segment streams keep deciding their own items as before.

    ``max_requests`` caps the search requests of this call (the rest of the cycle's
    budget is left to the free IO jobs). The sweep starts at ``start_stream`` (an index
    into the stream list, wrapped) and the returned ``cursor`` names where the next
    sweep should start: the first stream that did not finish, or 0 after a full pass.
    ``max_streams`` limits the sweep to that many streams from the start (the rest of a
    lap that an earlier sweep began); finishing them is a complete sweep and the cursor
    then names the stream after the last one. The result holds flat integer counters (summed over profiles), ``by_profile`` and
    ``cursor``.

    Existing SEACE contracts remain global and deduplicated by official ID.
    Only profile-scoped candidates reach process_contract / LLM1. The ordinary
    worker subsequently matches other tenants as usual; no LLM2 is run here.
    """
    profile_ids = list(dict.fromkeys(profile_ids))
    if not profile_ids:
        raise ValueError("At least one profile is required")
    if max_pages_per_segment < 1 or max_candidates < 1 or (max_requests is not None and max_requests < 1):
        raise ValueError("Polling limits must be positive")
    if type(search_parallelism) is not int or not 1 <= search_parallelism <= 4:
        raise ValueError("Search parallelism must be an integer from 1 to 4")
    if type(start_stream) is not int or start_stream < 0:
        raise ValueError("The start stream must be a non-negative index")
    if max_streams is not None and (type(max_streams) is not int or max_streams < 1):
        raise ValueError("The stream limit must be a positive integer")
    if max_keyword_pages is not None and (type(max_keyword_pages) is not int or max_keyword_pages < 1):
        raise ValueError("The keyword stream page limit must be a positive integer")

    def max_pages(segment: int) -> int:
        return (max_keyword_pages or max_pages_per_segment) if segment == KEYWORD_SEGMENT else max_pages_per_segment
    snapshots: dict[str, dict[str, Any]] = {}
    profile_segments: dict[str, set[int]] = {}
    for profile_id in profile_ids:
        snapshot = profile_snapshot(repo.conn, profile_id)
        lines = snapshot["lines"]
        segments = {int(value) for line in lines for value in line["cubso_segmentos"] if str(value).isdigit()}
        if not segments or not any(line.get("keywords") or line.get("keyword_phrases") or line.get("keyword_terms") for line in lines):
            raise ValueError("Profile needs active segments and keywords")
        snapshots[profile_id], profile_segments[profile_id] = snapshot, segments
    year = anio or datetime.now(LIMA_TZ).year
    totals = {"pages": 0, "capped_segments": 0, "http_requests": 0, "http_rows": 0}
    per_profile = {profile_id: dict.fromkeys(PROFILE_KEYS, 0) | {"limit_reached": 0}
                   for profile_id in profile_ids}
    seen: dict[str, set[int]] = {profile_id: set() for profile_id in profile_ids}
    admitted: set[int] = set()
    limited: set[str] = set()
    flags: dict[str, int] = {}

    def apply_page(response, *, objeto: int, segment: int) -> bool:
        # This function is only called on the coordinator thread. Neither the
        # repository nor its PostgreSQL connection reaches HTTP worker threads.
        totals["pages"] += 1
        keyword_stream = segment == KEYWORD_SEGMENT
        for item in response.data:
            for profile_id in profile_ids:
                if profile_id in limited or (not keyword_stream and segment not in profile_segments[profile_id]):
                    continue
                stats, snapshot = per_profile[profile_id], snapshots[profile_id]
                stats["seen"] += 1
                passed, reasons, keyword_lines = prefilter(
                    item, snapshot, year=year, now=datetime.now(timezone.utc), segment=segment)
                # PROD6 sometimes ignores anio: wrong-year/object rows are not stored.
                if (item.objeto_codigo != objeto
                        or {"object_out_of_scope", "publication_year_mismatch"} & set(reasons)):
                    stats["skipped"] += 1
                    continue
                # Not a rejection: a contract of the profile's segments is decided by its own stream.
                if "no_keyword_line" in reasons:
                    stats["keyword_misses"] += 1
                    continue
                decision, decided_by = decide(passed)
                stats["prefilter_rejected"] += int(not passed)
                contract_hash = listing_hash(item)
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
                if item.id_contrato in seen[profile_id]:
                    continue
                seen[profile_id].add(item.id_contrato)
                stats["candidates"] += 1
                if item.id_contrato not in admitted:  # One admission per contract, whoever accepts it.
                    admitted.add(item.id_contrato)
                    admission = admit_contract(repo, item, anio=year, segment=segment, batch_id=batch_id)
                    stats["changed"] += admission["changed"]
                    stats["enqueued"] += admission["enqueued"]
                # Preserved historical readings count toward this same bound too.
                if stats["candidates"] >= max_candidates:
                    stats["limit_reached"] = 1
                    limited.add(profile_id)
            if len(limited) == len(profile_ids):
                return True
        return False

    streams = [(objeto, segment) for objeto in (1, 2)
               for segment in sorted(set().union(*profile_segments.values()))]
    if keyword_streams:  # After the segment streams, so a lap decides those items first.
        streams += [(objeto, KEYWORD_SEGMENT) for objeto in (1, 2)]
    start = start_stream % len(streams)
    order = (streams[start:] + streams[:start])[:max_streams]
    unfinished: int | None = None  # Position in ``order`` of the first stream that did not finish.
    if search_parallelism == 1:
        # Default retains the original lazy traversal and shared serial client:
        # it does not prefetch other streams after reaching the candidate bound.
        requested = 0

        def reserve() -> bool:
            nonlocal requested
            if max_requests is not None and requested >= max_requests:
                return False
            requested += 1
            return True

        position = 0
        try:
            with SeaceClient(settings) as client:
                for position, (objeto, segment) in enumerate(order):
                    for response, capped in _search_pages(
                        client, year=year, objeto=objeto, segment=segment,
                        max_pages=max_pages(segment), reserve=reserve,
                    ):
                        totals["http_requests"] += 1
                        totals["http_rows"] += len(response.data)
                        totals["capped_segments"] += int(capped)
                        if apply_page(response, objeto=objeto, segment=segment):
                            unfinished = position
                            break
                    if unfinished is not None:
                        break
        except Exception as error:
            # A 403/429, a spent request budget or the discovery limit ends the sweep, but
            # the pages already applied are kept: the next cycle must not pay for them again.
            if not isinstance(error, DiscoveryLimit) and source_stop(error) is None:
                raise
            unfinished = position
            _mark_stop(flags, error)
    else:
        lock = threading.Lock()
        requested = 0

        def reserve() -> bool:
            nonlocal requested
            with lock:
                if max_requests is not None and requested >= max_requests:
                    return False
                requested += 1
                return True

        def fetch_stream(stream):
            objeto, segment = stream
            pages, stop = [], None
            with SeaceClient(settings) as client:
                try:
                    for response, capped in _search_pages(
                        client, year=year, objeto=objeto, segment=segment,
                        max_pages=max_pages(segment), reserve=reserve,
                    ):
                        pages.append((response, capped))
                except Exception as error:
                    if not isinstance(error, DiscoveryLimit) and source_stop(error) is None:
                        raise
                    stop = error
            return objeto, segment, pages, stop

        # HTTP-only work finishes before any write; preserve deterministic stream
        # order so varying network timings cannot change which candidates fit.
        # (With a discovery limit, which streams get cut can still depend on timing.)
        with ThreadPoolExecutor(max_workers=search_parallelism) as executor:
            queried = list(executor.map(fetch_stream, order))
        totals["http_requests"] = sum(len(pages) for _, _, pages, _ in queried)
        totals["http_rows"] = sum(len(page.data) for _, _, pages, _ in queried for page, _ in pages)
        totals["capped_segments"] = sum(int(capped) for _, _, pages, _ in queried for _, capped in pages)
        for position, (objeto, segment, pages, _) in enumerate(queried):
            for response, _ in pages:
                if apply_page(response, objeto=objeto, segment=segment):
                    unfinished = position
                    break
            if unfinished is not None:
                break
        if unfinished is None:
            for position, (_, _, _, stop) in enumerate(queried):
                if stop is not None:
                    if unfinished is None:
                        unfinished = position
                    _mark_stop(flags, stop)

    if unfinished is not None:
        next_index = (start + unfinished) % len(streams)
    else:
        next_index = 0 if len(order) == len(streams) else (start + len(order)) % len(streams)
    result: dict[str, Any] = {
        **{key: sum(stats[key] for stats in per_profile.values()) for key in PROFILE_KEYS},
        "limit_reached": max(stats["limit_reached"] for stats in per_profile.values()),
        **totals, **flags,
        "streams": len(streams), "streams_searched": len(order) if unfinished is None else unfinished,
    }
    result["by_profile"] = per_profile
    result["cursor"] = {"next_stream_index": next_index, "next_stream": list(streams[next_index]),
                        "stream_count": len(streams), "complete": unfinished is None}
    return result


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
    """Single-profile sweep: ``poll_prod6_profiles`` with one profile (flat counters only)."""
    result = poll_prod6_profiles(
        settings, repo, profile_ids=[profile_id], anio=anio,
        max_pages_per_segment=max_pages_per_segment, max_candidates=max_candidates,
        batch_id=batch_id, search_parallelism=search_parallelism)
    return {key: value for key, value in result.items()
            if key not in {"by_profile", "cursor", "streams", "streams_searched"}}
