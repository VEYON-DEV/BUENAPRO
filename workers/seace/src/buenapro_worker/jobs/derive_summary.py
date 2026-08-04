from __future__ import annotations

import json
import logging

from buenapro_worker.normalization.facets import derive_facets, derive_summary, facet_hash
from buenapro_worker.queue.repository import JobRepository


logger = logging.getLogger(__name__)


def derive_summary_job(
    repo: JobRepository,
    *,
    id_contrato: int,
    extraction_id: int,
    batch_id: str | None = None,
    bucket: str | None = None,
    segment: int | str | None = None,
) -> dict[str, object]:
    row = repo.conn.execute(
        """
        SELECT raw_extraction_json
        FROM tdr_extractions
        WHERE id = %s
        """,
        (extraction_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"Extraction not found: {extraction_id}")

    raw = dict(row["raw_extraction_json"])
    summary = derive_summary(raw)
    facets = derive_facets(raw)
    old_hashes = _current_hashes(repo, id_contrato)
    new_hashes = {facet_hash(item) for item in facets}

    repo.conn.execute(
        """
        UPDATE tdr_extractions
        SET summary_json = %s::jsonb
        WHERE id = %s
        """,
        (json.dumps(summary, ensure_ascii=False), extraction_id),
    )
    repo.conn.execute(
        """
        UPDATE requirement_facets
        SET is_current = false
        WHERE id_contrato = %s AND is_current = true
        """,
        (id_contrato,),
    )

    for item in facets:
        current_hash = facet_hash(item)
        repo.conn.execute(
            """
            INSERT INTO requirement_facets (
              id_contrato, extraction_id, facet, label, required,
              details_json, evidence_json, facet_hash, is_current
            )
            VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, true)
            """,
            (
                id_contrato,
                extraction_id,
                item["facet"],
                item["label"],
                item["required"],
                json.dumps(item["details"], ensure_ascii=False),
                json.dumps(item["evidence"], ensure_ascii=False),
                current_hash,
            ),
        )

    repo.conn.execute(
        """
        INSERT INTO contract_filter_index (
          id_contrato,
          valor_estimado,
          plazo_ejecucion_dias,
          tipo_pago,
          penalidad_tope_pct,
          entregables_count,
          roles_requeridos,
          facets,
          documentos_clave,
          updated_at
        )
        VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, %s, now())
        ON CONFLICT (id_contrato)
        DO UPDATE SET
          plazo_ejecucion_dias = EXCLUDED.plazo_ejecucion_dias,
          tipo_pago = EXCLUDED.tipo_pago,
          penalidad_tope_pct = EXCLUDED.penalidad_tope_pct,
          entregables_count = EXCLUDED.entregables_count,
          roles_requeridos = EXCLUDED.roles_requeridos,
          facets = EXCLUDED.facets,
          documentos_clave = EXCLUDED.documentos_clave,
          updated_at = now()
        """,
        (
            id_contrato,
            summary.get("plazo_ejecucion_dias"),
            summary.get("tipo_pago"),
            summary.get("penalidad_tope_pct"),
            summary.get("entregables_count"),
            summary.get("roles_requeridos") or [],
            summary.get("requirement_facets") or [],
            summary.get("documentos_clave") or [],
        ),
    )
    repo.conn.execute(
        """
        UPDATE seace_contracts
        SET pipeline_state = 'validated',
            updated_at = now()
        WHERE id_contrato = %s
          AND pipeline_state IN ('discovered', 'detail_fetched', 'files_listed', 'downloaded', 'extracted')
        """,
        (id_contrato,),
    )

    # Una extracción validada entra al gate barato por perfil. La cola y su
    # dedup evitan que dos documentos del mismo contrato disparen trabajo doble.
    repo.enqueue(
        "route_contract_profiles",
        {"id_contrato": id_contrato, "extraction_id": extraction_id},
        queue_name="match",
        dedup_key=f"route_contract_profiles:{id_contrato}",
        priority=4,
    )

    diff = sorted(old_hashes.symmetric_difference(new_hashes))
    if diff and old_hashes:
        # Los requisitos cambiaron: re-analizar solo los matches ya evaluados
        # (analisis LLM bajo demanda; el guard de hashes evita trabajo repetido).
        matches = repo.conn.execute(
            "SELECT profile_id FROM matches WHERE id_contrato = %s",
            (id_contrato,),
        ).fetchall()
        for match in matches:
            repo.enqueue(
                "analyze_match",
                {"id_contrato": id_contrato, "profile_id": str(match["profile_id"])},
                queue_name="llm",
                dedup_key=f"analyze_match:{match['profile_id']}:{id_contrato}",
                priority=4,
            )

    logger.info(
        "derive_summary_done",
        extra={"id_contrato": id_contrato, "extraction_id": extraction_id, "facets": len(facets), "diff": len(diff)},
    )
    return {"facets": len(facets), "diff": diff}


def _current_hashes(repo: JobRepository, id_contrato: int) -> set[str]:
    rows = repo.conn.execute(
        """
        SELECT facet_hash
        FROM requirement_facets
        WHERE id_contrato = %s AND is_current = true
        """,
        (id_contrato,),
    ).fetchall()
    return {str(row["facet_hash"]) for row in rows}
