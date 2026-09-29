from __future__ import annotations

import json
import logging

from buenapro_worker.extraction.gemini import GeminiExtractor, prompt_version_for_doc_class
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def extract_tdr_job(
    settings: Settings,
    repo: JobRepository,
    *,
    id_contrato: int,
    id_contrato_archivo: int,
    batch_id: str | None = None,
    bucket: str | None = None,
    segment: int | str | None = None,
) -> int:
    document = repo.conn.execute(
        """
        SELECT d.id, d.mime, d.sha256_original, c.objeto_codigo
        FROM contract_documents d
        JOIN seace_contracts c ON c.id_contrato = d.id_contrato
        WHERE d.id_contrato = %s AND d.id_contrato_archivo = %s
        """,
        (id_contrato, id_contrato_archivo),
    ).fetchone()
    if document is None:
        raise ValueError(f"Document not found: {id_contrato}/{id_contrato_archivo}")

    # El objeto nativo resuelve documentos ya descargados antes de introducir EETT.
    doc_class = "eett" if int(document["objeto_codigo"]) == 1 else "tdr"
    reusable = find_reusable_extraction(
        repo,
        document_id=int(document["id"]),
        sha256_original=str(document["sha256_original"]),
        prompt_version=prompt_version_for_doc_class(doc_class),
    )
    if reusable is not None:
        extraction_id = insert_reused_extraction(repo, document_id=int(document["id"]), source=reusable)
        mark_contract_extracted(repo, id_contrato)
        enqueue_derive_summary(
            repo,
            id_contrato=id_contrato,
            extraction_id=extraction_id,
            batch_id=batch_id,
            bucket=bucket,
            segment=segment,
        )
        logger.info(
            "extract_tdr_reused",
            extra={
                "id_contrato": id_contrato,
                "id_contrato_archivo": id_contrato_archivo,
                "extraction_id": extraction_id,
                "reused_from_extraction_id": reusable["id"],
                "sha256_original": document["sha256_original"],
            },
        )
        return extraction_id

    with SeaceClient(settings) as client:
        pdf_bytes = client.download_file(id_contrato_archivo)

    result = GeminiExtractor(settings).extract(
        pdf_bytes, mime=document["mime"] or "application/pdf", doc_class=doc_class
    )

    row = repo.conn.execute(
        """
        UPDATE tdr_extractions
        SET is_current = false
        WHERE contract_document_id = %s AND is_current = true
        RETURNING id
        """,
        (document["id"],),
    ).fetchone()

    extraction = repo.conn.execute(
        """
        INSERT INTO tdr_extractions (
          contract_document_id,
          model,
          prompt_version,
          schema_version,
          input_tokens,
          output_tokens,
          cost_usd,
          raw_extraction_json,
          summary_json,
          requires_human_review,
          reused_from_extraction_id,
          is_current
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, NULL, true)
        RETURNING id
        """,
        (
            document["id"],
            result.model,
            result.prompt_version,
            result.schema_version,
            result.input_tokens,
            result.output_tokens,
            result.cost_usd,
            json.dumps(result.raw_json, ensure_ascii=False),
            json.dumps(result.summary_json, ensure_ascii=False),
            result.requires_human_review,
        ),
    ).fetchone()
    extraction_id = int(extraction["id"])
    mark_contract_extracted(repo, id_contrato)

    enqueue_derive_summary(
        repo,
        id_contrato=id_contrato,
        extraction_id=extraction_id,
        batch_id=batch_id,
        bucket=bucket,
        segment=segment,
    )

    logger.info(
        "extract_tdr_done",
        extra={
            "id_contrato": id_contrato,
            "id_contrato_archivo": id_contrato_archivo,
            "extraction_id": extraction_id,
            "model": result.model,
            "input_tokens": result.input_tokens,
            "output_tokens": result.output_tokens,
            "cost_usd": result.cost_usd,
            "replaced_previous": row is not None,
        },
    )
    return extraction_id


def mark_contract_extracted(repo: JobRepository, id_contrato: int) -> None:
    repo.conn.execute(
        """
        UPDATE seace_contracts
        SET pipeline_state = 'extracted',
            updated_at = now()
        WHERE id_contrato = %s
          AND pipeline_state IN ('discovered', 'detail_fetched', 'files_listed', 'downloaded')
        """,
        (id_contrato,),
    )


def find_reusable_extraction(
    repo: JobRepository, *, document_id: int, sha256_original: str, prompt_version: str
) -> dict | None:
    if not sha256_original or sha256_original.startswith("pending:"):
        return None
    row = repo.conn.execute(
        """
        SELECT
          e.id,
          e.model,
          e.prompt_version,
          e.schema_version,
          e.raw_extraction_json,
          e.summary_json,
          e.requires_human_review,
          e.quality
        FROM tdr_extractions e
        JOIN contract_documents d ON d.id = e.contract_document_id
        WHERE d.sha256_original = %s
          AND d.id <> %s
          AND e.is_current = true
          AND e.quality <> 'failed'
          AND e.prompt_version = %s
        ORDER BY e.requires_human_review ASC, e.created_at DESC, e.id DESC
        LIMIT 1
        """,
        (sha256_original, document_id, prompt_version),
    ).fetchone()
    return None if row is None else dict(row)


def insert_reused_extraction(repo: JobRepository, *, document_id: int, source: dict) -> int:
    repo.conn.execute(
        """
        UPDATE tdr_extractions
        SET is_current = false
        WHERE contract_document_id = %s AND is_current = true
        """,
        (document_id,),
    )
    row = repo.conn.execute(
        """
        INSERT INTO tdr_extractions (
          contract_document_id,
          model,
          prompt_version,
          schema_version,
          input_tokens,
          output_tokens,
          cost_usd,
          raw_extraction_json,
          summary_json,
          requires_human_review,
          quality,
          reused_from_extraction_id,
          is_current
        )
        VALUES (%s, %s, %s, %s, 0, 0, 0, %s::jsonb, %s::jsonb, %s, %s, %s, true)
        RETURNING id
        """,
        (
            document_id,
            source["model"],
            source["prompt_version"],
            source["schema_version"],
            json.dumps(source["raw_extraction_json"], ensure_ascii=False),
            json.dumps(source["summary_json"], ensure_ascii=False),
            source["requires_human_review"],
            source["quality"],
            source["id"],
        ),
    ).fetchone()
    return int(row["id"])


def enqueue_derive_summary(
    repo: JobRepository,
    *,
    id_contrato: int,
    extraction_id: int,
    batch_id: str | None = None,
    bucket: str | None = None,
    segment: int | str | None = None,
) -> None:
    payload: dict[str, object] = {"id_contrato": id_contrato, "extraction_id": extraction_id}
    if batch_id:
        payload["batch_id"] = batch_id
    if bucket:
        payload["bucket"] = bucket
    if segment is not None:
        payload["segment"] = segment
    repo.enqueue(
        "derive_summary",
        payload,
        queue_name="match",
        dedup_key=f"derive_summary:{id_contrato}:{extraction_id}",
        priority=3,
    )
