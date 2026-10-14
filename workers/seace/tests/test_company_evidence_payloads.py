"""Offline contracts for reusable company evidence at the matching boundary.

No model client is constructed. Capture the effective job input, then the JSON
serialization boundary, so a documentary reading needs no PDF retransmission.
"""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from buenapro_worker.jobs.analyze_match import PROFILE_FIELDS, analyze_match_job, _econ_capacity
from buenapro_worker.matching.analyzer import ANALYSIS_PROMPT_VERSION, MatchAnalyzer
from buenapro_worker.prod2.analysis import (
    analyze_profile,
    analyze_substantive_profile,
    match_request_hash,
    substantive_match_request_hash,
)


class CaptureOnly(Exception):
    """Stop immediately at the model boundary, before payment or persistence."""


def profile():
    contract = {
        "objeto": "Suministro de variadores",
        "tipo_objeto": "bienes",
        "rubro": "Automatización industrial",
        "especialidad": "Variadores de velocidad",
        "monto": 15000,
        "moneda": "USD",
        "acreditacion": "documentada",
        "descripcion": "La orden registra variadores; no se identificó conformidad.",
        "documents": [{"id": "00000000-0000-0000-0000-000000000043"}],
        "documentary_summary": {
            "version": "company_evidence_v2",
            "status": "pending_human_confirmation",
            "amount_review_required": True,
            "sources": [{
                "document_id": "00000000-0000-0000-0000-000000000043",
                "sha256": "a" * 64,
                "extraction": {
                    "summary": "Orden de suministro; ejecución no acreditada.",
                    "document_types": ["orden de compra"],
                    "facts": [{"field": "monto", "value": "USD 15,000",
                               "quote": "Total USD 15,000", "page": 2}],
                    "conformity": "not_identified",
                    "unknowns": ["Acta de conformidad"],
                    "warnings": [],
                    "inferred_labels": ["Automatización industrial"],
                },
            }],
        },
    }
    return {
        **{field: [] for field in PROFILE_FIELDS},
        "razon_social": "Empresa industrial sintética",
        "ruc": "00000000000",
        "identity_json": {},
        "experience_json": [contract],
        "econ_experience_json": {},
        "profile_hash": "synthetic-current-profile",
    }


def extraction():
    return {
        "cache_key": "synthetic-tdr",
        "original_sha256": "b" * 64,
        "requires_human_review": False,
        "summary_json": {},
        "facets": [{"facet": "specific_experience", "label": "Experiencia en variadores",
                    "required": True, "details": {}, "evidence": []}],
    }


def settings():
    return SimpleNamespace(gemini_model="synthetic-model", gemini_fallback_model="synthetic-fallback")


def assert_documentary_evidence(record, *, compact=False):
    summary = record["documentary_summary"]
    source = summary["sources"][0]
    assert summary["status"] == "pending_human_confirmation"
    assert summary["amount_review_required"] is True
    assert source["document_id"] == "00000000-0000-0000-0000-000000000043"
    if compact:
        # Compact model input carries the cited source once, instead of a
        # duplicate documents list; its identity and facts remain mandatory.
        assert "documents" not in record
    else:
        assert source["document_id"] == record["documents"][0]["id"]
    assert source["sha256"] == "a" * 64
    assert source["extraction"]["facts"][0] == {
        "field": "monto", "value": "USD 15,000", "quote": "Total USD 15,000", "page": 2,
    }
    assert source["extraction"]["conformity"] == "not_identified"
    assert source["extraction"]["unknowns"] == ["Acta de conformidad"]
    assert record["moneda"] == "USD" and record["monto"] == 15000


def test_prod6_job_passes_current_documentary_summary_before_any_model_or_write():
    original = profile()
    repo = MagicMock()
    repo.conn.execute.side_effect = [
        SimpleNamespace(fetchone=lambda: original),
        SimpleNamespace(fetchone=lambda: {"codigo": "SYN-1", "descripcion": "Variadores",
                                         "entidad_nombre": "Entidad sintética", "summary_json": {},
                                         "extraction_id": 101, "quality": "good",
                                         "requires_human_review": False}),
        SimpleNamespace(fetchall=lambda: [{"facet": "specific_experience", "label": "Variadores",
                                          "required": True, "details_json": {}, "facet_hash": "synthetic"}]),
        SimpleNamespace(fetchone=lambda: None),
    ]
    client = MagicMock()
    client.analyze.side_effect = CaptureOnly
    before = copy.deepcopy(original)
    with patch("buenapro_worker.jobs.analyze_match.MatchAnalyzer", return_value=client):
        with pytest.raises(CaptureOnly):
            analyze_match_job(settings(), repo, id_contrato=1, profile_id="synthetic-profile")
    payload = client.analyze.call_args.kwargs
    assert_documentary_evidence(payload["perfil"]["experience_json"][0])
    assert payload["perfil"]["econ_experience_json"] == {}
    assert original == before
    assert repo.conn.execute.call_count == 4
    assert all("SELECT" in call.args[0] for call in repo.conn.execute.call_args_list)
    assert repo.conn.execute.call_args_list[2].args[1] == (1, 101)


@pytest.mark.parametrize("reading", [
    {"extraction_id": None, "quality": None, "requires_human_review": None},
    {"extraction_id": 101, "quality": "failed", "requires_human_review": False},
    {"extraction_id": 101, "quality": "good", "requires_human_review": True},
])
def test_prod6_job_rejects_unread_or_invalid_source_before_model_or_write(reading):
    repo = MagicMock()
    repo.conn.execute.side_effect = [
        SimpleNamespace(fetchone=profile),
        SimpleNamespace(fetchone=lambda: {"codigo": "SYN-1", "descripcion": "Variadores",
                                         "entidad_nombre": "Entidad sintética", "summary_json": {}, **reading}),
    ]
    with patch("buenapro_worker.jobs.analyze_match.MatchAnalyzer") as analyzer:
        with pytest.raises(ValueError, match="Current valid documentary extraction required"):
            analyze_match_job(settings(), repo, id_contrato=1, profile_id="synthetic-profile")
    analyzer.assert_not_called()
    assert repo.conn.execute.call_count == 2
    assert all("SELECT" in call.args[0] for call in repo.conn.execute.call_args_list)


@pytest.mark.parametrize("prompt_version", [ANALYSIS_PROMPT_VERSION, "match_analysis_v4_substantive"])
def test_prod6_serialized_model_input_contains_citations_and_no_pdf_content(prompt_version):
    client = MatchAnalyzer.__new__(MatchAnalyzer)
    client.settings = settings()
    client.prompt_version = prompt_version
    client._analyze_with_model = MagicMock(return_value="capture-only")
    original = profile()
    before = copy.deepcopy(original)
    assert client.analyze(perfil=original, oportunidad={}, requisitos=[]) == "capture-only"
    body = json.loads(client._analyze_with_model.call_args.args[1])
    assert_documentary_evidence(body["perfil"]["experience_json"][0], compact=True)
    assert body["perfil"]["econ_experience_json"] == {
        "areas": [], "no_global_sum": True, "currency_conversion": False,
    }
    assert "%PDF" not in json.dumps(body)
    assert "content" not in body["perfil"]["experience_json"][0]["documentary_summary"]["sources"][0]
    assert original == before


@pytest.mark.parametrize("evaluate", [analyze_profile, analyze_substantive_profile])
def test_prod2_effective_input_keeps_member_owned_summary_and_evidence_catalog(evaluate):
    joint = {"members": [
        {"profile_id": "industrial", "profile": profile()},
        {"profile_id": "digital", "profile": {"razon_social": "Empresa digital sintética",
                                                 "experience_json": [], "econ_experience_json": {}}},
    ]}
    before = copy.deepcopy(joint)
    client = MagicMock()
    client.analyze.side_effect = CaptureOnly
    with pytest.raises(CaptureOnly):
        evaluate(settings(), extraction(), profile=joint, opportunity={}, analyzer=client)
    body = client.analyze.call_args.kwargs["perfil"]
    members = {member["profile_id"]: member["profile"] for member in body["members"]}
    assert_documentary_evidence(members["industrial"]["experience_json"][0])
    assert members["digital"]["experience_json"] == []
    catalog = body["evidence_catalog"]["industrial:experience_json:0001"]
    assert catalog["profile_id"] == "industrial"
    assert_documentary_evidence(catalog["record"])
    assert "digital:experience_json:0001" not in body["evidence_catalog"]
    assert body["joint_qualification_verified"] is False
    assert joint == before


@pytest.mark.parametrize("fingerprint", [match_request_hash, substantive_match_request_hash])
def test_prod2_current_documentary_facts_invalidate_old_matching_fingerprint(fingerprint):
    original = {"members": [{"profile_id": "industrial", "profile": profile()}]}
    changed = copy.deepcopy(original)
    changed["members"][0]["profile"]["experience_json"][0]["documentary_summary"]["sources"][0]["extraction"]["facts"][0]["page"] = 3
    args = {"opportunity": {}}
    assert fingerprint(settings(), extraction(), profile=original, **args) != fingerprint(
        settings(), extraction(), profile=changed, **args,
    )


def test_documentary_amounts_do_not_become_global_capacity_or_sum_across_currency():
    current = profile()
    # Documentary source amounts need an object/currency comparison. Unknown
    # compatible capacity is not a verified zero or the largest global amount.
    assert _econ_capacity(current) is None
    current["econ_experience_json"] = {"servicios": 9000000, "areas": [
        {"rubro": "Software", "moneda": "PEN", "monto": 30000},
        {"rubro": "Variadores", "moneda": "USD", "monto": 15000},
    ]}
    assert _econ_capacity(current) is None
    assert _econ_capacity({"experience_json": [], "econ_experience_json": {}}) == 0
