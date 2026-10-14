"""Selector contracts with mocked Jev; no portal, provider, files or PostgreSQL."""
from copy import deepcopy
import json
from unittest.mock import MagicMock

import httpx
import pytest

from buenapro_worker.prod2 import document_selection as subject


def candidates(count=1):
    return [{"candidate_id": f"candidate-{i}", "filename": f"archivo-{i}.pdf", "type": "Documento",
             "stage": "Convocatoria", "published_at": "06/10/2026 12:00"} for i in range(count)]


def response(*rows):
    if not rows:
        rows = [("bases", .95, .05)]
    answers = {}
    for i, (role, needed, ambiguity) in enumerate(rows):
        stem = f"d{i:04d}"
        answers[stem + "_role"] = {"type": "choice", "choice": role, "confidence": .9, "probabilities": {role: 1.0}}
        answers[stem + "_needed"] = {"type": "noul", "noul": needed}
        answers[stem + "_ambiguity"] = {"type": "noul", "noul": ambiguity}
    return {"model": subject.JEV_DOCUMENT_MODEL, "answers": answers,
            "usage": {"input_tokens": 1000, "output_tokens": 100, "provider_extra": {"questions": len(answers)}}}


def client(data=None):
    fake = MagicMock()
    fake.post.return_value.status_code = 200
    fake.post.return_value.json.return_value = data or response()
    return fake


def selector(fake):
    return subject.JevDocumentSelector("fake-test-key", client=fake)


def test_one_text_only_batch_can_select_multiple_principals_and_complements():
    fake = client(response(("integrated_bases", .98, .05), ("tdr_eett", .97, .05), ("technical_annex", .9, .1),
                           ("form", .86, .1), ("award_record", .05, .01)))
    docs = candidates(5)
    # A misleading filename must not become a business rule in code.
    docs[1]["filename"] = "acta_buena_pro.pdf"
    result = selector(fake).select("Licencias software", docs)
    assert fake.post.call_count == 1
    assert result["selected_ids"] == [f"candidate-{i}" for i in range(4)]
    assert result["excluded_ids"] == ["candidate-4"] and result["pending_ids"] == []
    assert result["selection_status"] == "ready"
    assert result["coverage_verified"] is False
    assert result["cost_usd"] == pytest.approx(.000042)
    assert result["usage"] == response()["usage"] | {"provider_extra": {"questions": 15}}
    payload = fake.post.call_args.kwargs["json"]
    assert len(payload["questions"]) == 15
    for i in range(5):
        for suffix in ("role", "needed", "ambiguity"):
            assert f"state.documents[{i}]" in payload["questions"][f"d{i:04d}_{suffix}"]["instructions"]
    assert payload["questions"]["d0000_needed"]["type"] == "noul"
    assert "criteria" not in payload["questions"]["d0000_needed"]
    assert result["results"][0]["role_confidence"] == .9
    assert "needed_confidence" not in result["results"][0]


def test_provider_cache_is_immutable_and_threshold_changes_only_rebuild_plan():
    fake, cache = client(response(("bases", .7, .1))), {}
    docs = candidates()
    first = selector(fake).select("Software", docs, cache=cache)
    snapshot = deepcopy(cache)
    repeat = selector(fake).select("Software", docs, high_threshold=60, low_threshold=20, cache=cache)
    assert fake.post.call_count == 1
    assert repeat["cache_hit"] and repeat["incurred_cost_usd"] == 0
    assert first["request_hash"] == repeat["request_hash"]
    assert first["plan_hash"] != repeat["plan_hash"]
    assert first["pending_ids"] == ["candidate-0"] and repeat["selected_ids"] == ["candidate-0"]
    repeat["raw_response"]["answers"]["d0000_needed"]["noul"] = 0
    repeat["results"][0]["role_probabilities"]["bases"] = 0
    assert cache == snapshot
    assert subject.document_selection_request_hash("Software", docs) == first["request_hash"]


def test_ids_hashes_urls_and_private_extra_fields_never_enter_model_context():
    fake, docs = client(), candidates()
    uuid = "bcbc122b-c585-48fc-b345-07221a994975"
    docs[0].update(candidate_id=uuid, document_uuid=uuid, sha256="a" * 64, dni="12345678", ruc="20123456789",
                   source_url="https://example.test/private?token=abc", contracts=[{"name": "Private Person"}],
                   filename="Bases " + uuid + ".pdf",
                   preview_text="DNI 12345678 https://example.test/private apikey_secret token=secret user@example.test")
    result = selector(fake).select("Software", docs)
    payload = json.dumps(fake.post.call_args.kwargs["json"])
    for private in (uuid, "a" * 64, "12345678", "20123456789", "Private Person", "example.test", "apikey_secret", "token=secret"):
        assert private not in payload
    assert result["selected_ids"] == [uuid]
    assert set(fake.post.call_args.kwargs["json"]["state"]["documents"][0]) == set(subject._CONTEXT_FIELDS)


def test_type_alias_and_archive_context_are_supported_but_competing_aliases_fail():
    fake, docs = client(), candidates()
    docs[0].update(official_type="Documento", parent_filename="Bases.zip", member_path="docs/TDR.pdf", preview_text="Requisitos tecnicos")
    selector(fake).select("Software", docs)
    row = fake.post.call_args.kwargs["json"]["state"]["documents"][0]
    assert row["parent_filename"] == "Bases.zip" and row["member_path"] == "docs/TDR.pdf"
    docs[0]["official_type"] = "Acta"
    with pytest.raises(subject.DocumentSelectionError, match="aliases"):
        selector(fake).select("Software", docs)
    assert fake.post.call_count == 1


@pytest.mark.parametrize("role,needed,ambiguity", [
    ("unknown", .01, .01), ("unknown", .99, .01), ("bases", .1, .01),
    ("bases", .9, .3), ("bases", .6, .1), ("award_record", .9, .01), ("technical_annex", .5, .01)])
def test_generic_ambiguous_or_conflicting_candidates_are_pending_not_silently_discarded(role, needed, ambiguity):
    result = selector(client(response((role, needed, ambiguity)))).select("Software", candidates())
    assert result["pending_ids"] == ["candidate-0"]
    assert result["selected_ids"] == result["excluded_ids"] == []
    assert result["selection_status"] == "needs_review"


@pytest.mark.parametrize("role", ["form", "technical_annex", "clarification"])
def test_complements_without_a_main_document_do_not_produce_a_ready_plan(role):
    result = selector(client(response((role, .95, .05)))).select("Software", candidates())
    assert result["pending_ids"] == ["candidate-0"]
    assert result["decisions"][0]["reason"] == "principal_document_not_identified"


def test_unsupported_members_are_excluded_without_model_call_and_caller_order_is_preserved():
    fake, docs = client(), candidates(2)
    docs[0]["filename"] = "program.exe"
    result = selector(fake).select("Software", docs)
    assert result["selected_ids"] == ["candidate-1"] and result["excluded_ids"] == ["candidate-0"]
    assert [d["candidate_id"] for d in result["decisions"]] == ["candidate-0", "candidate-1"]
    assert len(fake.post.call_args.kwargs["json"]["state"]["documents"]) == 1
    docs[1]["filename"] = "nested.zip"
    fake.reset_mock()
    result = selector(fake).select("Software", docs)
    fake.post.assert_not_called()
    assert result["input_tokens"] == result["output_tokens"] == result["incurred_cost_usd"] == 0
    assert result["provider_call_required"] is False
    assert result["selection_status"] == "needs_review"


def test_cached_text_classification_can_be_remapped_to_new_ids_without_payment():
    fake, cache, docs = client(), {}, candidates()
    first = selector(fake).select("Software", docs, cache=cache)
    docs[0].update(candidate_id="new-id", sha256="f" * 64)
    second = selector(fake).select("Software", docs, cache=cache)
    assert first["request_hash"] == second["request_hash"]
    assert first["plan_hash"] != second["plan_hash"]
    assert second["selected_ids"] == ["new-id"] and fake.post.call_count == 1


def test_full_paid_receipt_reused_for_another_convocation_never_leaks_old_plan_ids():
    fake, docs = client(response(("bases", .7, .1))), candidates()
    old = selector(fake).select("Software", docs, high_threshold=80)
    cache = {old["cache_key"]: deepcopy(old)}
    before = deepcopy(cache)
    docs[0].update(candidate_id="current-convocation-member", document_uuid="current-parent", sha256="f" * 64)
    current = selector(fake).select("Software", docs, high_threshold=60, cache=cache)
    assert fake.post.call_count == 1 and current["cache_hit"]
    assert current["selected_ids"] == ["current-convocation-member"]
    assert current["pending_ids"] == [] and "candidate-0" not in json.dumps(current)
    assert cache == before


def test_threshold_boundaries_are_inclusive_and_do_not_mutate_inputs():
    fake = client(response(("bases", .8, .2), ("award_record", .2, .2)))
    docs, before = candidates(2), candidates(2)
    result = selector(fake).select("Software", docs)
    assert result["selected_ids"] == ["candidate-0"] and result["excluded_ids"] == ["candidate-1"]
    assert docs == before


def test_missing_extension_remains_a_candidate_for_semantic_ambiguity_not_an_unsupported_discard():
    fake, docs = client(response(("unknown", .02, .9))), candidates()
    docs[0]["filename"] = "archivo sin extension"
    result = selector(fake).select("Software", docs)
    assert result["pending_ids"] == ["candidate-0"] and result["excluded_ids"] == []
    assert fake.post.call_count == 1


@pytest.mark.parametrize("field", ["filename", "stage", "published_at", "parent_filename", "member_path", "preview_text", "type"])
def test_changed_document_text_invalidates_provider_cache(field):
    fake, cache, docs = client(), {}, candidates()
    first = selector(fake).select("Software", docs, cache=cache)
    docs[0][field] = "other.pdf" if field == "filename" else "Changed public metadata"
    second = selector(fake).select("Software", docs, cache=cache)
    assert first["request_hash"] != second["request_hash"] and fake.post.call_count == 2


@pytest.mark.parametrize("mutation", ["missing", "extra", "model", "role", "choice_type", "noul_type", "confidence",
                                    "probability_nan", "probability_sum", "probability_role", "noul_nan", "noul_bool",
                                    "noul_high", "noul_missing", "usage_negative", "usage_bool", "usage_missing"])
def test_invalid_provider_response_never_enters_cache(mutation):
    data = response()
    answers = data["answers"]
    if mutation == "missing": answers.pop("d0000_role")
    elif mutation == "extra": answers["unexpected"] = {}
    elif mutation == "model": data["model"] = "wrong-model"
    elif mutation == "role": answers["d0000_role"]["choice"] = "made-up"
    elif mutation == "choice_type": answers["d0000_role"]["type"] = "noul"
    elif mutation == "noul_type": answers["d0000_needed"]["type"] = "score"
    elif mutation == "confidence": answers["d0000_role"]["confidence"] = float("inf")
    elif mutation == "probability_nan": answers["d0000_role"]["probabilities"] = {"bases": float("nan")}
    elif mutation == "probability_sum": answers["d0000_role"]["probabilities"] = {"bases": .1}
    elif mutation == "probability_role": answers["d0000_role"]["probabilities"] = {"nonexistent": 1}
    elif mutation == "noul_nan": answers["d0000_needed"]["noul"] = float("nan")
    elif mutation == "noul_bool": answers["d0000_needed"]["noul"] = True
    elif mutation == "noul_high": answers["d0000_ambiguity"]["noul"] = 80
    elif mutation == "noul_missing": answers["d0000_needed"].pop("noul")
    elif mutation == "usage_negative": data["usage"]["input_tokens"] = -1
    elif mutation == "usage_bool": data["usage"]["output_tokens"] = False
    else: data.pop("usage")
    fake, cache = client(data), {}
    with pytest.raises(subject.DocumentSelectionError):
        selector(fake).select("Software", candidates(), cache=cache)
    assert cache == {} and fake.post.call_count == 1


@pytest.mark.parametrize("mutation", ["hash", "model", "policy", "raw"])
def test_corrupted_cached_paid_receipt_requires_reconciliation_without_repayment(mutation):
    fake, cache = client(), {}
    result = selector(fake).select("Software", candidates(), cache=cache)
    cached = cache[result["cache_key"]]
    if mutation == "hash": cached["request_hash"] = "0" * 64
    elif mutation == "model": cached["model"] = "wrong-model"
    elif mutation == "policy": cached["policy_version"] = "old"
    else: cached["raw_response"]["answers"]["d0000_needed"]["noul"] = float("nan")
    with pytest.raises(subject.DocumentSelectionError):
        selector(fake).select("Software", candidates(), cache=cache)
    assert fake.post.call_count == 1


@pytest.mark.parametrize("low,high", [(20,20), (80,20), (-1,80), (20,101), (True,80), (20,float("nan"))])
def test_invalid_thresholds_rejected_before_provider_call(low, high):
    fake = client()
    with pytest.raises(subject.DocumentSelectionError):
        selector(fake).select("Software", candidates(), low_threshold=low, high_threshold=high)
    fake.post.assert_not_called()


def test_duplicate_ids_missing_metadata_and_overlarge_preview_are_rejected_without_call():
    fake = client()
    for docs in [[], [{"candidate_id": "a"}], [*candidates(), *candidates()],
                 [{**candidates()[0], "preview_text": "x" * 4001}]]:
        with pytest.raises(subject.DocumentSelectionError):
            selector(fake).select("Software", docs)
    fake.post.assert_not_called()


def test_http_and_transport_errors_do_not_echo_provider_body_or_credentials():
    fake = client()
    fake.post.return_value.status_code = 403
    fake.post.return_value.text = "fake-test-key PRIVATE RESPONSE"
    with pytest.raises(RuntimeError, match="^Jev document-selection HTTP 403$"):
        selector(fake).select("Software", candidates())
    fake.post.side_effect = httpx.ConnectError("fake-test-key PRIVATE URL")
    with pytest.raises(RuntimeError, match="^Jev document-selection transport failure: ConnectError$"):
        selector(fake).select("Software", candidates())


def test_invalid_json_body_is_not_echoed_or_cached_and_no_internal_retry_occurs():
    fake, cache = client(), {}
    fake.post.return_value.json.side_effect = ValueError("fake-test-key PRIVATE BODY")
    with pytest.raises(subject.DocumentSelectionError, match="^Invalid Jev document-selection JSON response$"):
        selector(fake).select("Software", candidates(), cache=cache)
    assert cache == {} and fake.post.call_count == 1


def test_empty_model_or_excessive_total_preview_is_rejected_before_provider_call():
    fake = client()
    with pytest.raises(subject.DocumentSelectionError, match="model"):
        subject.JevDocumentSelector("fake-test-key", model="", client=fake).select("Software", candidates())
    docs = candidates(21)
    for doc in docs:
        doc["preview_text"] = "x" * 4000
    with pytest.raises(subject.DocumentSelectionError, match="bounded payload"):
        selector(fake).select("Software", docs)
    fake.post.assert_not_called()


def test_legacy_v1_request_payload_hash_and_defaults_are_unchanged():
    """Changing the preview triage must never reinterpret a paid v1 request."""
    expected = "b4498c3ddf4af52be1206a4cc79c4f659faf7959f6151f81bb6d7b3d5d460584"
    assert subject.document_selection_request_hash("Software", candidates()) == expected
    fake = client()
    result = selector(fake).select("Software", candidates())
    assert result["request_hash"] == expected
    assert result["policy_version"] == "prod2_jev_document_selection_v1"
    assert result["plan_policy_version"] == "prod2_jev_document_plan_v2"
    payload = fake.post.call_args.kwargs["json"]
    assert set(payload["state"]["documents"][0]) == set(subject._CONTEXT_FIELDS)
    assert "administrative_record" not in payload["questions"]["d0000_role"]["criteria"]
    assert "verificar referencias/versiones" in payload["questions"]["d0000_ambiguity"]["instructions"]


def v2_selector(fake):
    return subject.JevDocumentSelector("fake-test-key", client=fake,
        policy_version=subject.DOCUMENT_SELECTION_POLICY_V2)


def test_v2_distinguishes_reading_decision_from_documentary_completeness():
    fake, docs = client(response(("bases", .95, .1))), candidates()
    docs[0].update(filename="Bases.pdf", preview_text="BASES. Capitulo III: requerimiento del servicio.")
    result = v2_selector(fake).select("Software", docs)
    assert result["selected_ids"] == ["candidate-0"]
    assert result["thresholds"] == {"high": 80, "low": 20}
    assert result["coverage_verified"] is False
    assert result["not_completeness_or_eligibility_probability"] is True
    assert result["policy_version"] == "prod2_jev_document_selection_v2"
    assert result["plan_policy_version"] == "prod2_jev_document_plan_v3"
    payload = fake.post.call_args.kwargs["json"]
    assert set(payload["state"]["documents"][0]) == set(subject._V2_CONTEXT_FIELDS)
    assert payload["state"]["documents"][0]["preview_text"] == docs[0]["preview_text"]
    ambiguity = payload["questions"]["d0000_ambiguity"]["instructions"]
    assert "La necesidad normal de leer mas paginas no es ambiguedad" in ambiguity
    assert "No valores aqui la completitud" in ambiguity
    assert "0: la decision de lectura u omision" in ambiguity


def test_v2_archive_member_does_not_inherit_container_type_as_its_content():
    fake, docs = client(response(("administrative_record", .05, .1), ("bases", .95, .1))), candidates(2)
    docs[0].update(filename="Resolucion.pdf", type="Bases Integradas", source_kind="archive_member",
        stage="Integracion de las Bases", parent_filename="Bases Integradas.zip",
        member_path="bases/Resolucion.pdf", preview_text="Resolucion que aprueba un tramite administrativo.")
    before = deepcopy(docs)
    result = v2_selector(fake).select("Software", docs)
    row = fake.post.call_args.kwargs["json"]["state"]["documents"][0]
    assert row["official_type"] == row["stage"] == row["published_at"] == ""
    assert row["container_official_type"] == "Bases Integradas"
    assert row["container_stage"] == "Integracion de las Bases"
    assert row["container_published_at"] == "06/10/2026 12:00"
    assert row["source_kind"] == "archive_member"
    assert result["excluded_ids"] == ["candidate-0"] and result["selected_ids"] == ["candidate-1"]
    assert docs == before


def test_v2_loose_document_retains_official_publication_metadata():
    fake, docs = client(), candidates()
    docs[0]["source_kind"] = "loose"
    v2_selector(fake).select("Software", docs)
    row = fake.post.call_args.kwargs["json"]["state"]["documents"][0]
    assert row["official_type"] == "Documento"
    assert row["stage"] == "Convocatoria" and row["published_at"] == "06/10/2026 12:00"
    assert row["container_official_type"] == row["container_stage"] == row["container_published_at"] == ""


def test_v2_provider_cache_is_separate_from_v1_and_preserves_legacy_paid_receipts():
    fake, docs, cache = client(), candidates(), {}
    v1 = selector(fake).select("Software", docs, cache=cache)
    frozen = deepcopy(cache[v1["cache_key"]])
    v2 = v2_selector(fake).select("Software", docs, cache=cache)
    assert fake.post.call_count == 2
    assert v2["request_hash"] != v1["request_hash"]
    assert cache[v1["cache_key"]] == frozen
    reused = v2_selector(fake).select("Software", docs, cache=cache)
    assert fake.post.call_count == 2 and reused["cache_hit"] and reused["incurred_cost_usd"] == 0
    assert subject.document_selection_request_hash("Software", docs,
        policy_version=subject.DOCUMENT_SELECTION_POLICY_V2) == v2["request_hash"]


def test_v2_changed_preview_or_container_context_requires_its_own_response():
    fake, docs, cache = client(), candidates(), {}
    docs[0].update(source_kind="archive_member", preview_text="Requisitos de la convocatoria.")
    first = v2_selector(fake).select("Software", docs, cache=cache)
    docs[0]["stage"] = "Integracion de las Bases"
    second = v2_selector(fake).select("Software", docs, cache=cache)
    docs[0]["preview_text"] = "Especificaciones tecnicas actualizadas."
    third = v2_selector(fake).select("Software", docs, cache=cache)
    assert len({r["request_hash"] for r in (first, second, third)}) == 3
    assert fake.post.call_count == 3


def test_v2_minimal_readable_preview_does_not_turn_choice_confidence_into_gate():
    data = response(("tdr_eett", .9, .1))
    data["answers"]["d0000_role"]["confidence"] = .01
    fake, docs = client(data), candidates()
    docs[0].update(filename="TDR.pdf", type="Bases Administrativas", preview_text="Terminos de referencia del servicio.")
    result = v2_selector(fake).select("Software", docs)
    assert result["selected_ids"] == ["candidate-0"] and result["coverage_verified"] is False
    assert result["decisions"][0]["role_confidence"] == .01


@pytest.mark.parametrize("role,needed,ambiguity", [
    ("unknown", .95, .1), ("bases", .95, .21), ("tdr_eett", .79, .1),
    ("administrative_record", .95, .1), ("evaluation_record", .05, .9)])
def test_v2_keeps_real_uncertainty_and_role_conflicts_pending(role, needed, ambiguity):
    result = v2_selector(client(response((role, needed, ambiguity)))).select("Software", candidates())
    assert result["pending_ids"] == ["candidate-0"]
    assert result["selected_ids"] == result["excluded_ids"] == []
    assert result["requires_human_review"] is True


@pytest.mark.parametrize("role", ["administrative_record", "evaluation_record"])
def test_v2_new_non_requirements_roles_are_rejected_by_v1_and_excluded_by_v2(role):
    fake = client(response((role, .05, .1)))
    with pytest.raises(subject.DocumentSelectionError, match="role"):
        selector(fake).select("Software", candidates())
    result = v2_selector(fake).select("Software", candidates())
    assert result["excluded_ids"] == ["candidate-0"] and result["selected_ids"] == []
    assert result["selection_status"] == "needs_review"  # No principal exists.


def test_v2_unsupported_nested_archive_and_missing_principal_stay_pending():
    fake, docs = client(response(("technical_annex", .95, .05))), candidates(2)
    docs[1].update(filename="nested.zip", inventory_status="pending_nested_archive")
    result = v2_selector(fake).select("Software", docs)
    assert result["pending_ids"] == ["candidate-0", "candidate-1"]
    assert result["decisions"][0]["reason"] == "principal_document_not_identified"
    assert result["decisions"][1]["reason"] == "nested_archive_not_inspected"


@pytest.mark.parametrize("policy", ["unknown-policy", None, [], True])
def test_unknown_selection_policy_is_rejected_before_provider_call(policy):
    fake = client()
    with pytest.raises(subject.DocumentSelectionError, match="policy"):
        subject.JevDocumentSelector("fake-test-key", client=fake, policy_version=policy)
    with pytest.raises(subject.DocumentSelectionError, match="policy"):
        subject.document_selection_request_hash("Software", candidates(), policy_version=policy)
    fake.post.assert_not_called()


@pytest.mark.parametrize("source_kind", ["url", "member", None, []])
def test_v2_invalid_source_kind_is_rejected_before_provider_call(source_kind):
    fake, docs = client(), candidates()
    docs[0]["source_kind"] = source_kind
    with pytest.raises(subject.DocumentSelectionError, match="source kind"):
        v2_selector(fake).select("Software", docs)
    fake.post.assert_not_called()


def v3_response(*rows):
    """Rows: role, action, probability of read/omit/inspect; all are distinct."""
    answers = {}
    for i, (role, action, read, omit, inspect) in enumerate(rows):
        stem = f"d{i:04d}"
        answers[stem + "_role"] = {"type": "choice", "choice": role, "confidence": .9,
                                   "probabilities": {role: 1.0}}
        answers[stem + "_action"] = {"type": "choice", "choice": action, "confidence": .9,
                                     "probabilities": {"read": read, "omit": omit, "inspect": inspect}}
    return {"model": subject.JEV_DOCUMENT_MODEL, "answers": answers,
            "usage": {"input_tokens": 1000, "output_tokens": 100}}


def v3_selector(fake):
    return subject.JevDocumentSelector("fake-test-key", client=fake,
                                      policy_version=subject.DOCUMENT_SELECTION_POLICY_V3)


def test_v3_independent_choice_actions_can_select_multiple_documents_and_omit_records():
    fake = client(v3_response(("integrated_bases", "read", .9, .01, .09),
        ("tdr_eett", "read", .92, .01, .07), ("clarification", "read", .85, .05, .1),
        ("award_record", "omit", .01, .98, .01), ("evaluation_record", "omit", .05, .9, .05)))
    result = v3_selector(fake).select("Software", candidates(5))
    assert result["selected_ids"] == ["candidate-0", "candidate-1", "candidate-2"]
    assert result["excluded_ids"] == ["candidate-3", "candidate-4"]
    assert result["pending_ids"] == [] and result["selection_status"] == "ready"
    assert result["thresholds"] == {"high": 80, "low": 20}
    assert result["coverage_verified"] is False
    assert result["policy_version"] == "prod2_jev_document_selection_v3"
    assert result["plan_policy_version"] == "prod2_jev_document_plan_v4"
    payload = fake.post.call_args.kwargs["json"]
    assert len(payload["questions"]) == 10
    for i in range(5):
        for kind in ("role", "action"):
            question = payload["questions"][f"d{i:04d}_{kind}"]
            assert question["type"] == "choice" and f"state.documents[{i}]" in question["instructions"]
        action = payload["questions"][f"d{i:04d}_action"]
        assert set(action["criteria"]) == {"read", "omit", "inspect"}
        assert len(action["instructions"]) < 400
        assert "completeness" in action["instructions"]
    for row in result["results"]:
        assert "needed_noul" not in row and "ambiguity_noul" not in row
        assert row["read_probability"] == row["action_probabilities"]["read"]
        assert row["omit_probability"] == row["action_probabilities"]["omit"]
        assert row["inspect_probability"] == row["action_probabilities"]["inspect"]


def test_v3_archive_member_and_loose_publication_context_remain_separate_without_mutation():
    fake, docs = client(v3_response(("tdr_eett", "read", .9, .05, .05))), candidates()
    docs[0].update(filename="TDR.pdf", source_kind="archive_member", type="Bases Integradas",
                   parent_filename="Bases.zip", member_path="TDR.pdf", preview_text="REQUERIMIENTO DEL SERVICIO")
    before = deepcopy(docs)
    result = v3_selector(fake).select("Software", docs)
    row = fake.post.call_args.kwargs["json"]["state"]["documents"][0]
    assert row["official_type"] == row["stage"] == row["published_at"] == ""
    assert row["container_official_type"] == "Bases Integradas"
    assert row["container_stage"] == "Convocatoria"
    assert row["preview_text"] == docs[0]["preview_text"]
    assert docs == before and result["selected_ids"] == ["candidate-0"]


@pytest.mark.parametrize("role,action,read,omit,inspect", [
    ("bases", "read", .79, .11, .1), ("bases", "read", .79, 0, .21),
    ("bases", "inspect", .1, .1, .8), ("unknown", "read", .9, .05, .05),
    ("award_record", "read", .9, .05, .05), ("tdr_eett", "omit", .05, .9, .05),
    ("administrative_record", "omit", .11, .79, .1)])
def test_v3_uncertain_action_or_document_role_conflict_stays_pending(role, action, read, omit, inspect):
    result = v3_selector(client(v3_response((role, action, read, omit, inspect)))).select("Software", candidates())
    assert result["pending_ids"] == ["candidate-0"] and result["selected_ids"] == result["excluded_ids"] == []
    assert result["requires_human_review"] is True


def test_v3_exact_probability_boundaries_are_inclusive_and_confidence_is_not_gate():
    data = v3_response(("bases", "read", .8, 0, .2), ("award_record", "omit", .2, .8, 0))
    for answer in data["answers"].values():
        answer["confidence"] = .01
    result = v3_selector(client(data)).select("Software", candidates(2))
    assert result["selected_ids"] == ["candidate-0"] and result["excluded_ids"] == ["candidate-1"]
    assert result["decisions"][0]["action_confidence"] == .01


def test_v3_complement_without_principal_does_not_claim_a_ready_set():
    result = v3_selector(client(v3_response(("technical_annex", "read", .95, .04, .01)))).select("Software", candidates())
    assert result["pending_ids"] == ["candidate-0"]
    assert result["decisions"][0]["reason"] == "principal_document_not_identified"


def test_v3_response_and_cache_do_not_reinterpret_either_legacy_policy():
    docs, cache = candidates(), {}
    legacy_fake = client(response(("bases", .9, .1)))
    v1 = selector(legacy_fake).select("Software", docs, cache=cache)
    v2 = v2_selector(legacy_fake).select("Software", docs, cache=cache)
    frozen = deepcopy(cache)
    fake = client(v3_response(("bases", "read", .9, .01, .09)))
    v3 = v3_selector(fake).select("Software", docs, cache=cache)
    assert len({r["request_hash"] for r in (v1, v2, v3)}) == 3
    assert all(cache[key] == receipt for key, receipt in frozen.items())
    replay = v3_selector(fake).select("Software", docs, cache=cache)
    assert fake.post.call_count == 1 and replay["cache_hit"] and replay["incurred_cost_usd"] == 0
    assert replay["decisions"] == v3["decisions"]
    assert subject.document_selection_request_hash("Software", docs,
        policy_version=subject.DOCUMENT_SELECTION_POLICY_V3) == v3["request_hash"]
    with pytest.raises(subject.DocumentSelectionError, match="coverage"):
        v3_selector(client(response())).select("Software", docs)


@pytest.mark.parametrize("mutation", ["type", "choice", "confidence", "missing_action",
    "missing_probability", "extra_probability", "sum", "nan", "bool", "range", "legacy_questions"])
def test_v3_invalid_action_distribution_is_never_cached_or_retried(mutation):
    data = v3_response(("bases", "read", .9, .01, .09))
    action = data["answers"]["d0000_action"]
    if mutation == "type": action["type"] = "noul"
    elif mutation == "choice": action["choice"] = "unspecified"
    elif mutation == "confidence": action["confidence"] = float("inf")
    elif mutation == "missing_action": data["answers"].pop("d0000_action")
    elif mutation == "missing_probability": action["probabilities"].pop("inspect")
    elif mutation == "extra_probability": action["probabilities"]["other"] = 0
    elif mutation == "sum": action["probabilities"]["read"] = .5
    elif mutation == "nan": action["probabilities"]["inspect"] = float("nan")
    elif mutation == "bool": action["probabilities"]["inspect"] = True
    elif mutation == "range": action["probabilities"]["inspect"] = -1
    else: data["answers"]["d0000_needed"] = {"type": "noul", "noul": .9}
    fake, cache = client(data), {}
    with pytest.raises(subject.DocumentSelectionError):
        v3_selector(fake).select("Software", candidates(), cache=cache)
    assert cache == {} and fake.post.call_count == 1


def test_v3_nested_archives_are_pending_even_with_a_clear_principal():
    fake, docs = client(v3_response(("bases", "read", .95, .03, .02))), candidates(2)
    docs[1].update(filename="nested.zip", inventory_status="pending_nested_archive")
    result = v3_selector(fake).select("Software", docs)
    assert result["selected_ids"] == ["candidate-0"] and result["pending_ids"] == ["candidate-1"]
    assert result["coverage_verified"] is False and result["selection_status"] == "needs_review"
