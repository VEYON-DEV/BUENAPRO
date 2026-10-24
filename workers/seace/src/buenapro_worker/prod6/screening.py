"""PROD6 minor-purchase screening: hard prefilter and persisted per-company decisions.

One decision is stored per contract x company x (search-card hash, profile hash,
policy version), approved AND rejected, so a decided contract is never
re-evaluated. Keyword hits are only a signal here; M2 replaces the keyword step
of ``decide`` with JEV affinity scoring and fills the JEV columns.
"""
from __future__ import annotations

import json
import re
import unicodedata
from datetime import datetime
from typing import Any

import psycopg

from buenapro_worker.jobs.poll_search import LIMA_TZ, canonical_hash, parse_lima_datetime
from buenapro_worker.seace.schemas import SearchItem

POLICY_VERSION = "prod6_screening_v2"


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.lower())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(re.findall(r"[a-z0-9]+", value))


def line_matches(text: str, line: dict[str, Any]) -> bool:
    """One phrase hit or two distinct terms of this line; ``text`` is normalized and space-padded."""
    phrases = {normalize(value) for value in line.get("keyword_phrases", []) if value.strip()}
    terms = {normalize(value) for value in line.get("keyword_terms", []) if value.strip()}
    # Backward compatibility with lines populated before split keywords.
    if not phrases and not terms:
        for value in line.get("keywords", []):
            normalized = normalize(value)
            (phrases if " " in normalized else terms).add(normalized)
    if any(phrase and f" {phrase} " in text for phrase in phrases):
        return True
    return sum(bool(term) and f" {term} " in text for term in terms) >= 2


def profile_snapshot(conn: psycopg.Connection, profile_id: str) -> dict[str, Any]:
    profile = conn.execute(
        """SELECT id, identity_json->>'descripcion' AS descripcion
           FROM company_profiles WHERE id = %s AND is_active = true""",
        (profile_id,),
    ).fetchone()
    if not profile:
        raise ValueError("Active company profile not found")
    rows = conn.execute(
        """SELECT nombre, cubso_segmentos, keyword_phrases, keyword_terms, keywords, score_umbral
           FROM business_lines WHERE profile_id = %s AND is_active = true""",
        (profile_id,),
    ).fetchall()
    lines = sorted(
        ({
            "nombre": row["nombre"],
            "cubso_segmentos": sorted(str(value) for value in row["cubso_segmentos"]),
            "keyword_phrases": sorted(row["keyword_phrases"]),
            "keyword_terms": sorted(row["keyword_terms"]),
            "keywords": sorted(row["keywords"]),
            "score_umbral": int(row["score_umbral"]),
        } for row in rows),
        key=lambda line: (line["nombre"], canonical_hash(line)),
    )
    content = {"descripcion": profile["descripcion"] or "", "lines": lines}
    return {"id": str(profile["id"]), **content, "profile_hash": canonical_hash(content)}


def prefilter(item: SearchItem, snapshot: dict[str, Any], *, year: int, now: datetime,
              segment: int) -> tuple[bool, list[str], list[str]]:
    """Hard rules only; keywords never block and are reported in ``keyword_lines``.

    ``segment`` is the CUBSO segment of the search stream that returned the item
    (the search item itself does not carry it).
    """
    publication = parse_lima_datetime(item.fec_publica)
    close = parse_lima_datetime(item.fec_fin_cotizacion)
    reasons = []
    if item.estado_codigo != 2:
        reasons.append("not_vigente")
    if item.objeto_codigo not in (1, 2):
        reasons.append("object_out_of_scope")
    if publication is None or publication.astimezone(LIMA_TZ).year != year:
        reasons.append("publication_year_mismatch")
    if close is not None and close <= now:
        reasons.append("closed")
    segment_lines = [line for line in snapshot["lines"] if str(segment) in line["cubso_segmentos"]]
    if not segment_lines:
        reasons.append("segment_out_of_profile")
    text = f" {normalize(item.descripcion)} "
    keyword_lines = [line["nombre"] for line in segment_lines if line_matches(text, line)]
    return not reasons, reasons, keyword_lines


def decide(prefilter_passed: bool) -> tuple[str, str]:
    """Policy v2 as (decision, decided_by).

    A prefilter pass is never an acceptance: the contract waits as 'pending' for
    JEV, which is NOT a rejection (a JEV failure keeps it pending and retried).
    """
    if not prefilter_passed:
        return "rejected", "prefilter"
    return "pending", "jev"


def existing_decision(conn: psycopg.Connection, id_contrato: int, profile_id: str,
                      contract_hash: str, profile_hash: str, policy_version: str) -> dict[str, Any] | None:
    return conn.execute(
        """SELECT id, decision, decided_by FROM prod6_profile_decisions
           WHERE id_contrato = %s AND profile_id = %s AND contract_hash = %s
             AND profile_hash = %s AND policy_version = %s""",
        (id_contrato, profile_id, contract_hash, profile_hash, policy_version),
    ).fetchone()


def record_decision(conn: psycopg.Connection, *, item: SearchItem, segment: int, profile_id: str,
                    contract_hash: str, profile_hash: str, policy_version: str,
                    prefilter_passed: bool, prefilter_reasons: list[str],
                    keyword_lines: list[str], decision: str, decided_by: str) -> bool:
    """Insert once per key; returns False when the decision already existed.

    The row keeps its own snapshot of the search listing: rejected and pending
    contracts are deliberately not written to seace_contracts / opportunities.
    """
    listing = item.model_dump(mode="json", by_alias=True)
    row = conn.execute(
        """INSERT INTO prod6_profile_decisions (
             id_contrato, profile_id, codigo, cubso_segmento, objeto_codigo, fec_fin_cotizacion,
             listing_json, contract_hash, profile_hash, policy_version,
             prefilter_passed, prefilter_reasons, keyword_lines, decision, decided_by)
           VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (id_contrato, profile_id, contract_hash, profile_hash, policy_version)
           DO NOTHING RETURNING id""",
        (item.id_contrato, profile_id, item.codigo, str(segment), item.objeto_codigo,
         parse_lima_datetime(item.fec_fin_cotizacion), json.dumps(listing, ensure_ascii=False),
         contract_hash, profile_hash, policy_version, prefilter_passed, prefilter_reasons,
         keyword_lines, decision, decided_by),
    ).fetchone()
    return row is not None
