"""Prepare/apply a scoped documentary reading with CAS; never call models or jobs."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workers/seace/src"))
sys.path.insert(0, str(ROOT / "scripts"))
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from buenapro_worker.prod2.runtime import production_settings
from read_company_document_text import private_json

VERSION = "company_document_reading_v1"


def amount_number(value):
    return Decimal(str(value)) if value is not None else None


def prepare(folder: Path):
    baseline = json.loads((folder / "baseline.private.json").read_text())
    profile = baseline["profile"]
    overrides = json.loads((folder / "verified-overrides.private.json").read_text())
    documents = {}
    for doc in baseline["documents"]:
        sha = doc["sha256"]
        receipt = json.loads((folder / "evidence" / f"{sha}.json").read_text())
        text = json.loads((folder / "texts" / f"{sha}.json").read_text())
        if receipt["sha256"] != sha or text["sha256"] != sha or text["page_count"] != len(text["pages"]):
            raise ValueError("Source/page binding failed")
        evidence = deepcopy(receipt["extraction"])
        verified = overrides.get(doc["id"], {})
        # OCR repairs must be tied to a reviewed original page. Keep the raw
        # extraction receipt intact; annotate each literal correction separately.
        for correction in verified.get("text_corrections", []):
            page = text["pages"][correction["page"] - 1]
            if page["page"] != correction["page"] or correction["old"] not in page["text"]:
                raise ValueError("Reviewed OCR correction does not match source page")
            page["text"] = page["text"].replace(correction["old"], correction["new"])
            page["characters"] = len(page["text"])
        if verified.get("text_corrections"):
            text["local_source_corrections"] = verified["text_corrections"]
            text["full_text"] = "\n\n".join(f"[Página {p['page']}]\n{p['text']}" for p in text["pages"])
        # Supplements are local, source-cited corrections, preserving paid output.
        for key, value in verified.get("evidence_updates", {}).items():
            evidence[key] = value
        evidence.setdefault("amounts", [])
        if verified.get("append_amounts"):
            evidence["amounts"].extend(verified["append_amounts"])
        evidence["warnings"] = list(dict.fromkeys([*evidence.get("warnings", []), *verified.get("warnings", [])]))
        index = verified.get("economic_amount_index", evidence.get("principal_amount_index"))
        evidence["principal_amount_index"] = index
        selected = evidence["amounts"][index] if index is not None else None
        if selected and (selected["currency"] not in {"PEN", "USD", "EUR"} or selected["tax_basis"] == "tax" or amount_number(selected["amount_decimal"]) <= 0):
            raise ValueError("Invalid sourced amount")
        for item in [*evidence.get("facts", []), *evidence["amounts"]]:
            if not 1 <= item["page"] <= text["page_count"] or not item["quote"].strip():
                raise ValueError("Citation outside PDF")
        if verified.get("page_notes"):
            text["visual_page_notes"] = verified["page_notes"]
        lines = [evidence["summary"].strip()]
        if evidence.get("classification"):
            c = evidence["classification"]
            lines.append(f"Clasificación propuesta: {c['rubro']} / {c['specialty']} (p. {c['page']}).")
        if selected:
            lines.append(f"Importe documental: {selected['currency']} {selected['amount_decimal']} — {selected['label']} ({selected['source_type']}, p. {selected['page']}).")
        lines.append("Tipos: " + ", ".join(evidence["document_types"]) + ".")
        lines.append("Conformidad: " + ("documento identificado; pendiente de validación humana." if evidence["conformity"] == "explicit_documentary_evidence" else "no identificada en el archivo."))
        if evidence.get("warnings"):
            lines.append("Revisar: " + " ".join(evidence["warnings"]))
        if evidence.get("unknowns"):
            lines.append("No identificado: " + "; ".join(evidence["unknowns"]) + ".")
        meta = deepcopy(doc.get("metadata_json") or {})
        meta["document_text"] = text
        meta["documentary_extraction"] = receipt
        meta["documentary_validation"] = {"version": VERSION, "status": "pending_human_confirmation",
            "sha256": sha, "extraction": evidence, "local_source_corrections": verified,
            "source_pdf_preserved": True, "currency_conversion": False}
        meta["economic_evidence"] = {"amounts": evidence["amounts"], "selected_amount": selected,
            "status": "documentary_not_legal_accreditation", "classification": evidence.get("classification"),
            "no_global_sum": True}
        documents[doc["id"]] = {"id": doc["id"], "sha256": sha, "metadata_json": meta,
            "description": "\n".join(lines), "amount": selected["amount_decimal"] if selected else doc.get("amount"),
            "evidence": evidence, "selected": selected, "filename": doc["filename"],
            "document_type": evidence["document_types"][0][:40]}
    records = deepcopy(profile["experience_json"])
    areas, archived_refs, numeric_changes = [], [], []
    for record_index, record in enumerate(records):
        original = deepcopy(record)
        good, missing = [], []
        for reference in record.get("documents", []):
            (good if reference.get("id") in documents else missing).append(reference)
        if not good:
            raise ValueError("A contract has no available source")
        record["documents"] = good
        archived_refs.extend({"record_index": record_index, "reference": reference,
            "reason": "not_found_in_scoped_and_global_pg_prior_audit", "not_read": True} for reference in missing)
        sources = []
        for reference in good:
            doc = documents[reference["id"]]
            sources.append({"document_id": doc["id"], "sha256": doc["sha256"],
                "extraction": doc["evidence"], "status": "pending_human_confirmation"})
        # The audit verified no repeated PDFs/contract refs. Never add monetary totals.
        if len(good) != 1:
            raise ValueError("Multiple source documents require contract reconciliation first")
        doc = documents[good[0]["id"]]
        selected = doc["selected"]
        evidence = doc["evidence"]
        summary = {"version": VERSION, "status": "pending_human_confirmation", "sources": sources,
            "original_description": (original.get("documentary_summary") or {}).get("original_description", original.get("descripcion")),
            "original_fields": {k: original.get(k) for k in ["monto", "moneda", "rubro", "especialidad", "tipo_objeto"]},
            "missing_document_ids": [r["id"] for r in missing], "archived_missing_references": missing,
            "source_text_field": "company_documents.metadata_json.document_text",
            "no_legal_accreditation": True, "no_currency_conversion": True}
        classification = evidence.get("classification") or {}
        if not record.get("rubro"):
            record["rubro"] = classification.get("rubro")
        if not record.get("especialidad"):
            record["especialidad"] = classification.get("specialty")
        verified_type = overrides.get(doc["id"], {}).get("record_type")
        if verified_type:
            record["tipo_objeto"] = verified_type
        # Preserve an existing declared amount if supported among source amounts;
        # otherwise correct against the specifically verified documentary basis.
        matches = [a for a in evidence["amounts"] if amount_number(a["amount_decimal"]) == amount_number(record.get("monto"))
            and (not record.get("moneda") or a["currency"] == record["moneda"])]
        amount_basis = matches[0] if matches else selected
        if amount_basis:
            record["monto"] = float(amount_number(amount_basis["amount_decimal"]))
            record["moneda"] = amount_basis["currency"]
            summary["amount_basis"] = amount_basis | {"document_id": doc["id"], "sha256": doc["sha256"]}
        if original.get("monto") != record.get("monto") or original.get("moneda") != record.get("moneda"):
            numeric_changes.append({"record_index": record_index, "before": [original.get("monto"), original.get("moneda")],
                "after": [record.get("monto"), record.get("moneda")], "basis": summary.get("amount_basis")})
        summary["amount_review_required"] = not bool(amount_basis) or bool(evidence.get("warnings"))
        record["descripcion"] = doc["description"] + ("\nReferencia anterior ausente archivada; no se leyó ese archivo." if missing else "")
        record["documentary_summary"] = summary
        if selected:
            areas.append({"rubro": record.get("rubro") or classification.get("rubro"),
                "especialidad": record.get("especialidad") or classification.get("specialty"),
                "tipo_objeto": record.get("tipo_objeto"), "monto": float(amount_number(selected["amount_decimal"])),
                "moneda": selected["currency"], "descripcion": f"{record.get('objeto', '')}\n{selected['label']} ({selected['source_type']}, p. {selected['page']}). Pendiente de validación humana; no es un total global acreditado.",
                "documents": good, "source_record_index": record_index,
                "documentary_basis": selected | {"document_id": doc["id"], "sha256": doc["sha256"]},
                "status": "pending_human_confirmation", "no_global_sum": True})
    economic = deepcopy(profile.get("econ_experience_json") or {})
    if economic.get("areas"):
        raise ValueError("Existing economic areas require reconciliation")
    economic["areas"] = areas
    economic["documentary_provenance"] = {"version": VERSION, "source_records": len(records), "currency_conversion": False,
        "global_totals_unchanged": True, "status": "pending_human_confirmation", "archived_missing_references": archived_refs}
    return baseline, {"version": VERSION, "profile_id": profile["id"], "documents": documents,
        "experience_json": records, "econ_experience_json": economic, "numeric_changes": numeric_changes,
        "archived_missing_references": archived_refs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    baseline, plan = prepare(args.folder)
    private_json(args.folder / "prepared.private.json", plan)
    compact = {"documents": len(plan["documents"]), "experience_records": len(plan["experience_json"]),
        "economic_areas": len(plan["econ_experience_json"]["areas"]), "numeric_changes": len(plan["numeric_changes"]),
        "orphan_refs_archived": len(plan["archived_missing_references"]), "models_called": 0, "jobs_enqueued": 0}
    print(json.dumps(compact), flush=True)
    if not args.apply:
        return
    profile = baseline["profile"]
    with production_settings(ssh_host="ubuntu@168.129.177.199", ssh_key=ROOT / "credentials/ssh-key-2026-07-14.key") as settings:
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            with conn.transaction():
                live = conn.execute("SELECT experience_json,econ_experience_json,updated_at FROM company_profiles WHERE id=%s AND tenant_id=%s FOR UPDATE",
                    (profile["id"], profile["tenant_id"])).fetchone()
                if not live or live["experience_json"] != profile["experience_json"] or live["econ_experience_json"] != profile["econ_experience_json"] or live["updated_at"] != datetime.fromisoformat(profile["updated_at"]):
                    raise ValueError("Profile changed after baseline; aborting CAS")
                for original in baseline["documents"]:
                    live_doc = conn.execute("SELECT metadata_json,updated_at,encode(digest(content,'sha256'),'hex') AS sha256 FROM company_documents WHERE id=%s AND tenant_id=%s AND profile_id=%s FOR UPDATE",
                        (original["id"], profile["tenant_id"], profile["id"])).fetchone()
                    if not live_doc or live_doc["metadata_json"] != original["metadata_json"] or live_doc["sha256"] != original["sha256"] or live_doc["updated_at"] != datetime.fromisoformat(original["updated_at"]):
                        raise ValueError("Document changed after baseline; aborting CAS")
                    updated = plan["documents"][original["id"]]
                    conn.execute("UPDATE company_documents SET description=%s,metadata_json=%s,amount=%s,document_type=%s,updated_at=now() WHERE id=%s AND tenant_id=%s",
                        (updated["description"], Jsonb(updated["metadata_json"]), updated["amount"], updated["document_type"], original["id"], profile["tenant_id"]))
                hash_input = json.dumps({"experience": plan["experience_json"], "economic": plan["econ_experience_json"]}, sort_keys=True, ensure_ascii=False)
                conn.execute("UPDATE company_profiles SET experience_json=%s,econ_experience_json=%s,profile_hash=md5(coalesce(profile_hash,'') || %s),updated_at=now() WHERE id=%s AND tenant_id=%s",
                    (Jsonb(plan["experience_json"]), Jsonb(plan["econ_experience_json"]), hash_input, profile["id"], profile["tenant_id"]))
        with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            live = conn.execute("SELECT experience_json,econ_experience_json,profile_hash,identity_json FROM company_profiles WHERE id=%s", (profile["id"],)).fetchone()
            assert live["experience_json"] == plan["experience_json"] and live["econ_experience_json"] == plan["econ_experience_json"]
            rows = conn.execute("SELECT id::text,metadata_json,description,encode(digest(content,'sha256'),'hex') AS sha256 FROM company_documents WHERE id=ANY(%s::uuid[]) AND tenant_id=%s", (list(plan["documents"]), profile["tenant_id"])).fetchall()
            assert len(rows) == len(plan["documents"])
            for row in rows:
                expected = plan["documents"][row["id"]]
                assert row["sha256"] == expected["sha256"] and row["metadata_json"] == expected["metadata_json"] and row["description"] == expected["description"]
            compact.update(applied=True, pg_readback_exact=True, pdf_hashes_preserved=len(rows),
                pages=sum(row["metadata_json"]["document_text"]["page_count"] for row in rows),
                profile_ingestion_enabled=live["identity_json"].get("profile_ingestion_enabled"),
                profile_hash_changed=live["profile_hash"] != profile["profile_hash"], at=datetime.now(timezone.utc).isoformat())
            private_json(args.folder / "pg-readback.private.json", compact)
            print(json.dumps(compact), flush=True)


if __name__ == "__main__":
    main()
