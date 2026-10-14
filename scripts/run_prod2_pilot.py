"""Explicit laptop pilot: frozen <=10 candidates, durable provider cache, production DB.

No scheduler, browser, Redis job, legacy worker or national sweep is started.
Private manifests/cache preserve paid responses before DB completion for recovery.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))

from dotenv import dotenv_values
import httpx
import psycopg
from psycopg.rows import dict_row

from buenapro_worker.prod2.analysis import (
    ASSEMBLY_POLICY_VERSION, SUBSTANTIVE_POLICY_VERSION, JEV_MODEL, JEV_POLICY_VERSION, JevDescriptionAnalyzer,
    analyze_substantive_profile, assemble_cached_joint, assembly_request_hash,
    extract_document, extraction_for_persistence, extraction_request_hash,
    jev_request_hash, match_for_persistence, substantive_match_request_hash,
    prepare_joint_profile, select_requirements_document,
)
from buenapro_worker.normalization.facets import canonical_hash
from buenapro_worker.prod2.client import Prod2HttpClient, SEARCH_URL
from buenapro_worker.prod2.documents import download_document
from buenapro_worker.prod2.previews import upload_preview
from buenapro_worker.prod2.repository import (
    claim_evaluation, complete_evaluation, persist_detail, persist_extraction,
    persist_match, record_document_download, upsert_pilot_discovery,
)
from buenapro_worker.prod2.runtime import production_settings
from buenapro_worker.local_document_conversion import prepare_pdf


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def private_write(path: Path, value):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as output:
        json.dump(value, output, ensure_ascii=False, default=str)


def stage(conn, folder, conv, kind, request_hash, model, policy, callback, *, profile_id):
    """Commit a reservation before paying; reconcile our saved response without retry."""
    claim = claim_evaluation(conn, conv, kind=kind, request_hash=request_hash, model=model,
                             policy_version=policy, profile_id=profile_id, lease_seconds=3600)
    if claim["status"] == "cached":
        return claim["id"], claim["result_json"], True
    stem = folder / f"{kind}-{request_hash}"
    response_path = stem.with_suffix(".response.private.json")
    owner_path = stem.with_suffix(".claim.private.json")
    if claim["status"] == "claimed":
        private_write(owner_path, claim)
    elif owner_path.exists() and response_path.exists() and claim["status"] in {"busy", "uncertain"}:
        owner = read(owner_path)
        if owner["id"] != claim["id"]:
            raise RuntimeError("Private claim does not match durable reservation")
        claim = owner
    else:
        raise RuntimeError("Provider reservation needs reconciliation; automatic retry refused")
    if response_path.exists():
        result = read(response_path)
        reused = True
    else:
        try:
            result = callback()
        except Exception as error:
            # A missing response does not establish zero provider billing.
            # Keep this reservation blocked until explicit reconciliation.
            code = getattr(error, "code", None)
            safe_error = {"error_type": type(error).__name__,
                          "http_code": code if type(code) is int else None,
                          "ended_at": datetime.now(timezone.utc).isoformat(),
                          "provider_usage_known": False}
            failure_path = stem.with_suffix(".failure.private.json")
            if not failure_path.exists():
                private_write(failure_path, safe_error)
            conn.execute("""UPDATE prod2_evaluations SET status='uncertain',updated_at=now()
              WHERE id=%s AND claim_token=%s AND status='running'""",
              (claim["id"], claim["claim_token"]))
            raise
        private_write(response_path, result)
        reused = False
    complete_evaluation(conn, claim["id"], claim["claim_token"], result,
                        input_tokens=result.get("input_tokens"), output_tokens=result.get("output_tokens"),
                        cost_usd=result.get("cost_usd"))
    return claim["id"], result, reused


def ficha(folder, row, query):
    path = folder / "detail.private.json"
    if path.exists():
        return read(path)
    with httpx.Client(timeout=60, verify=True) as http:
        source = Prod2HttpClient(http)
        source.search(query["keyword"], year=query["year"], object_type=row["object"],
            published_from=datetime.fromisoformat(query["published_from"]).strftime("%d/%m/%Y"),
            published_to=datetime.fromisoformat(query["published_to"]).strftime("%d/%m/%Y"))
        index = next((i for i, result in enumerate(source.rows) if result["convocatoria_id"] == row["convocatoria_id"]
                      and result["procedure_id"] == row["procedure_id"]), None)
        if index is None:
            raise RuntimeError("Frozen candidate not found on verified native search page")
        detail = source.detail(index, all_documents=True)
    result = {"observed_at": datetime.now(timezone.utc).isoformat(),
              "detail": {key: value for key, value in detail.items() if key not in {"http_status", "redirect_statuses"}}}
    private_write(path, result)
    return result


def analysis_pdf(folder, original, extension, original_sha):
    """Pin exact derivative bytes; never reconvert an already paid document."""
    marker = folder / "analysis-pdf.private.json"
    if marker.exists():
        pinned = read(marker)
        target = (folder / pinned["relative_path"]).resolve()
        if not target.is_relative_to(folder.resolve()) or pinned["original_sha256"] != original_sha:
            raise RuntimeError("Pinned derivative does not bind the original")
        if hashlib.sha256(target.read_bytes()).hexdigest() != pinned["analysis_sha256"]:
            raise RuntimeError("Pinned derivative bytes changed")
        return target, pinned["manifest"]
    cached = [read(path) for path in folder.glob("gemini_extraction-*.response.private.json")]
    hashes = {bundle["analysis_sha256"] for bundle in cached if bundle.get("original_sha256") == original_sha}
    if len(hashes) > 1:
        raise RuntimeError("Multiple paid derivatives require explicit reconciliation")
    if hashes:
        matches = [path for path in folder.rglob("*.pdf")
                   if path.stat().st_size <= 100 * 1024 * 1024 and hashlib.sha256(path.read_bytes()).hexdigest() in hashes]
        if not matches:
            raise RuntimeError("Paid derivative unavailable; refusing automatic reconversion")
        target = sorted(matches)[0]
    else:
        target = prepare_pdf(original, extension, max_bytes=20 * 1024 * 1024)
    document_manifest = read(folder / "local-document-manifest.json") if (folder / "local-document-manifest.json").exists() else None
    private_write(marker,{"relative_path":str(target.resolve().relative_to(folder.resolve())),
        "original_sha256":original_sha,"analysis_sha256":hashlib.sha256(target.read_bytes()).hexdigest(),
        "manifest":document_manifest})
    return target, document_manifest


def finish_joint(conn, folder, conv, settings, extraction, saved, joint, opportunity, profile_id):
    """Reuse attributable paid evidence; only new substantive reads use Gemini."""
    current = conn.execute("""SELECT 1 FROM prod2_document_extractions ex
      JOIN prod2_documents d ON d.id_convocatoria=ex.id_convocatoria AND d.codigo_alfresco=ex.codigo_alfresco AND d.is_listed
      WHERE ex.id=%s AND ex.id_convocatoria=%s AND ex.is_current AND ex.sha256_original=%s""",
      (saved["extraction_id"],conv,extraction["original_sha256"])).fetchone()
    if not current:
        raise RuntimeError("Cached extraction is not the current listed document; refusing model call")
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""SELECT id,result_json FROM prod2_evaluations
          WHERE id_convocatoria=%s AND kind='gemini_match' AND status='done' AND profile_id=%s
            AND policy_version IN ('prod2_joint_evidence_v1','prod2_substantive_evidence_v4')
            AND result_json->>'extraction_id'=%s AND result_json->>'original_sha256'=%s
            AND result_json->>'profile_hash'=%s ORDER BY id LIMIT 1""",
          (conv,profile_id,str(saved["extraction_id"]),extraction["original_sha256"],canonical_hash(prepare_joint_profile(joint))))
        source = cursor.fetchone()
    if source:
        source_id, paid = source["id"], source["result_json"]
        source_cached = True
    else:
        def substantive():
            return {**analyze_substantive_profile(settings, extraction, profile=joint, opportunity=opportunity),
                    "extraction_id": saved["extraction_id"]}
        source_id, paid, source_cached = stage(conn, folder, conv, "gemini_match",
            substantive_match_request_hash(settings, extraction, profile=joint, opportunity=opportunity),
            settings.gemini_model,SUBSTANTIVE_POLICY_VERSION,substantive,profile_id=profile_id)
    def assemble():
        return {**assemble_cached_joint(settings,paid,extraction,joint,opportunity,source_paid_evaluation_id=source_id),
                "extraction_id":saved["extraction_id"]}
    assembly_id, result, hit = stage(conn, folder, conv, "gemini_match",
        assembly_request_hash(settings,paid,extraction,joint,opportunity),paid["model"],ASSEMBLY_POLICY_VERSION,
        assemble,profile_id=profile_id)
    return assembly_id,result,hit,source_cached


def verify(conn, manifest):
    ids = [item["row"]["convocatoria_id"] for item in manifest["selected"]]
    with conn.cursor(row_factory=dict_row) as cursor:
        cursor.execute("""SELECT p.id_convocatoria,p.id_procedimiento,p.nomenclature,p.object_type,
          d.item_states,d.document_inventory_total,d.document_inventory_complete,
          ex.id AS extraction_id,ex.facet_count,ex.requires_human_review,
          m.score,m.verdict,
          (SELECT result_json->>'affinity_index' FROM prod2_evaluations e WHERE e.id_convocatoria=p.id_convocatoria
            AND kind='jev_affinity' AND status='done' ORDER BY id DESC LIMIT 1) AS affinity,
          (SELECT count(*) FROM prod2_document_downloads dd WHERE dd.id_convocatoria=p.id_convocatoria) AS downloads
          FROM prod2_discoveries p LEFT JOIN prod2_details d USING(id_convocatoria)
          LEFT JOIN prod2_document_extractions ex ON ex.id_convocatoria=p.id_convocatoria AND ex.is_current
          LEFT JOIN prod2_match_provenance mp ON mp.id_convocatoria=p.id_convocatoria AND mp.extraction_id=ex.id
          LEFT JOIN opportunity_matches m ON m.id=mp.match_id AND m.profile_id=%s
          WHERE p.id_convocatoria=ANY(%s) ORDER BY p.id_convocatoria""", (manifest["profile_id"], ids))
        rows = cursor.fetchall()
        cursor.execute("""SELECT kind,status,count(*) AS count,sum(input_tokens) AS input_tokens,
          sum(output_tokens) AS output_tokens,sum(cost_usd) AS cost_usd FROM prod2_evaluations
          WHERE id_convocatoria=ANY(%s) GROUP BY kind,status ORDER BY kind,status""", (ids,))
        usage = cursor.fetchall()
        cursor.execute("SELECT count(*) FROM prod2_pilot_observations WHERE batch_id=%s", (manifest["batch_id"],))
        total = cursor.fetchone()["count"]
    if len(rows) != len(ids) or total != len(ids):
        raise RuntimeError("Durable pilot membership does not reconcile")
    return {"batch_id": manifest["batch_id"], "records": rows, "usage": usage, "coverage": "pilot_selection"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--ssh-host", default="ubuntu@168.129.177.199")
    parser.add_argument("--ssh-key", type=Path, default=ROOT / "credentials/ssh-key-2026-07-14.key")
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--only-conv", type=int, help="Process one member of the frozen pilot; always verify all ten")
    args = parser.parse_args()
    os.umask(0o077)
    folder = args.folder.resolve()
    manifest = read(folder / "pilot-manifest.private.json")
    selected = manifest["selected"]
    if not 1 <= len(selected) <= 10 or len({item["row"]["convocatoria_id"] for item in selected}) != len(selected):
        raise ValueError("Pilot must contain 1–10 unique frozen candidates")
    if args.only_conv is not None and args.only_conv not in {item["row"]["convocatoria_id"] for item in selected}:
        raise ValueError("Requested identity is outside the frozen pilot")
    snapshots = {row["profile_id"]: row for row in read(folder / "member-profiles.private.json")}
    members = [snapshots[pid] for pid in manifest["member_profile_ids"]]
    descriptions = read(ROOT / "scripts/seeds/company-commercial-descriptions.json")
    commercial = [{"descripcion": descriptions[member["profile_id"]]["descripcion"],
                   "lineas": [line["nombre"] for line in member["business_lines"]]} for member in members]
    joint = {"members": [{"profile_id": member["profile_id"], "profile": member["profile"]} for member in members],
             "commercial_scope": {"companies": commercial}}
    jev_key = dotenv_values(ROOT / ".env.jev.local").get("TYPESAFE_API_KEY")
    with production_settings(ssh_host=args.ssh_host, ssh_key=args.ssh_key) as settings:
        with psycopg.connect(settings.database_url, autocommit=True) as conn:
            if not args.verify_only:
                for item in selected:
                    upsert_pilot_discovery(conn, item["row"], item["query_manifest"], batch_id=manifest["batch_id"],
                                           observed_at=datetime.fromisoformat(item["observed_at"]))
                jev = JevDescriptionAnalyzer(jev_key)
                for item in selected:
                    row, query = item["row"], item["query_manifest"]
                    conv = row["convocatoria_id"]
                    if args.only_conv is not None and conv != args.only_conv:
                        continue
                    previous = conn.execute("""SELECT 1 FROM prod2_match_provenance mp
                      JOIN opportunity_matches m ON m.id=mp.match_id
                      JOIN prod2_document_extractions ex ON ex.id=mp.extraction_id AND ex.is_current
                      JOIN prod2_evaluations ev ON ev.id=mp.evaluation_id AND ev.status='done'
                      WHERE mp.id_convocatoria=%s AND m.profile_id=%s AND mp.profile_hash=%s
                        AND ev.result_json->>'coverage_valid'='true'
                        AND m.breakdown_json->'meta'->>'document_sha256'=ex.sha256_original""",
                      (conv,manifest["profile_id"],canonical_hash(prepare_joint_profile(joint)))).fetchone()
                    if previous:
                        print(json.dumps({"conv": conv, "stage": "complete", "cached": True}), flush=True)
                        continue
                    candidate_folder = folder / str(conv)
                    candidate_folder.mkdir(mode=0o700, exist_ok=True)
                    try:
                        _, relevance, hit = stage(conn, candidate_folder, conv, "jev_affinity",
                            jev_request_hash(row["description"], commercial_profiles=commercial), JEV_MODEL,
                            JEV_POLICY_VERSION, lambda: jev.classify(row["description"], commercial_profiles=commercial),
                            profile_id=manifest["profile_id"])
                        print(json.dumps({"conv": conv, "stage": "jev", "affinity": relevance["affinity_index"], "cached": hit}), flush=True)
                        if relevance["affinity_index"] < manifest["jev_threshold"] and not relevance["needs_detail"]:
                            continue
                        capture = ficha(candidate_folder, row, query)
                        detail = capture["detail"]
                        persist_detail(conn, detail, observed_at=datetime.fromisoformat(capture["observed_at"]), source_url=SEARCH_URL)
                        document = select_requirements_document(detail["documents"], inventory_complete=detail["document_inventory_complete"])
                        if document is None:
                            raise RuntimeError("No observed requirements document")
                        suffix = Path(document["filename"]).suffix.lower()
                        kind = {".pdf": "pdf", ".zip": "zip", ".docx": "zip", ".rar": "rar"}.get(suffix)
                        if kind is None:
                            raise RuntimeError("Requirements document format needs review")
                        original = candidate_folder / (document["uuid"] + suffix)
                        download_path = candidate_folder / "download.private.json"
                        if download_path.exists():
                            downloaded = read(download_path)
                            if hashlib.sha256(original.read_bytes()).hexdigest() != downloaded["sha256"]:
                                raise RuntimeError("Private downloaded document hash changed")
                        else:
                            with httpx.Client(timeout=90, verify=True) as http:
                                downloaded = download_document(http, document["uuid"], original, expected_format=kind)
                            private_write(download_path, downloaded)
                        record_document_download(conn, conv, downloaded)
                        pdf_path, document_manifest = analysis_pdf(candidate_folder,original,suffix.lstrip("."),downloaded["sha256"])
                        pdf = pdf_path.read_bytes()
                        preview = upload_preview(pdf, convocation_id=conv, document_uuid=document["uuid"],
                                                 original_sha256=downloaded["sha256"], env_path=ROOT / ".env.local")
                        record_document_download(conn, conv, downloaded, storage_key=preview["storage_key"])
                        def extract():
                            return {**extract_document(settings, pdf, object_type=query["object_type"], original_sha256=downloaded["sha256"], manifest=document_manifest),
                                    "document_uuid": document["uuid"], "document_sha256": downloaded["sha256"]}
                        evaluation_id, bundle, hit = stage(conn, candidate_folder, conv, "gemini_extraction",
                            extraction_request_hash(settings, pdf, object_type=query["object_type"], original_sha256=downloaded["sha256"], manifest=document_manifest),
                            settings.gemini_model, "prod2_document_extraction_v1", extract, profile_id=manifest["profile_id"])
                        saved = persist_extraction(conn, conv, document["uuid"], downloaded["sha256"], evaluation_id=evaluation_id,
                            result=extraction_for_persistence(bundle), allowed_models=(settings.gemini_model, settings.gemini_fallback_model))
                        print(json.dumps({"conv": conv, "stage": "llm1", "facets": saved.get("facet_count", len(bundle["facets"])), "review": bundle["requires_human_review"], "cached": hit}), flush=True)
                        if bundle["requires_human_review"]:
                            continue
                        opportunity = {"codigo": row["nomenclature"], "descripcion": row["description"], "entidad": row["entity"]}
                        match_id, result, hit, source_cached = finish_joint(conn,candidate_folder,conv,settings,
                            bundle,saved,joint,opportunity,manifest["profile_id"])
                        if result.get("coverage_valid"):
                            kwargs = match_for_persistence(result)
                            kwargs["breakdown_json"]["meta"]["member_names"] = {member["profile_id"]: member["name"] for member in members}
                            persist_match(conn, conv, profile_id=manifest["profile_id"], extraction_id=saved["extraction_id"], evaluation_id=match_id, **kwargs)
                        print(json.dumps({"conv": conv, "stage": "llm2", "status": result["status"],
                            "verdict": result.get("verdict"), "cached": hit,"paid_source_cached":source_cached,
                            "pending_document_rules":len(result.get("rule_filled_ids",[]))}), flush=True)
                    except Exception as error:
                        # API errors can embed keys, URLs, response bodies or personal data.
                        print(json.dumps({"conv": conv, "stage": "needs_review", "error_type": type(error).__name__}), flush=True)
                    time.sleep(1)
            report = verify(conn, manifest)
            report["verified_at"] = datetime.now(timezone.utc).isoformat()
            path = folder / ("verify-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + ".json")
            private_write(path, report)
            print(json.dumps(report, default=str, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
