from __future__ import annotations

import logging
from pathlib import Path

from buenapro_worker.documents.pdf import classify_document, detect_mime, sha256_bytes
from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def download_file_job(
    settings: Settings,
    repo: JobRepository,
    *,
    id_contrato: int,
    id_contrato_archivo: int,
    batch_id: str | None = None,
    bucket: str | None = None,
    segment: int | str | None = None,
) -> dict[str, object]:
    # El PDF no se persiste en ningun storage propio: la web hace preview y
    # descarga en streaming directo desde SEACE. Este job solo baja el archivo
    # para identificarlo (sha256/MIME/clase) y disparar la extraccion.
    row = repo.conn.execute(
        """
        SELECT d.filename, d.categoria, c.objeto_codigo
        FROM contract_documents d
        JOIN seace_contracts c ON c.id_contrato = d.id_contrato
        WHERE d.id_contrato = %s AND d.id_contrato_archivo = %s
        """,
        (id_contrato, id_contrato_archivo),
    ).fetchone()
    filename = Path(row["filename"] if row else f"{id_contrato_archivo}.pdf").name
    categoria = int(row["categoria"] or 1) if row else 1

    with SeaceClient(settings) as client:
        content = client.download_file(id_contrato_archivo)

    sha_original = sha256_bytes(content)
    mime = detect_mime(content)
    doc_class = classify_document(filename, mime)
    # La categoria 1 es el requerimiento. Su clase depende del objeto nativo
    # de SEACE; el nombre suele ser generico o incluso decir "ANEXO".
    if categoria == 1 and row:
        if int(row["objeto_codigo"]) == 1:
            doc_class = "eett"
        elif int(row["objeto_codigo"]) == 2:
            doc_class = "tdr"
        elif doc_class == "otro":
            doc_class = "tdr"
    elif categoria == 1 and doc_class == "otro":
        doc_class = "tdr"

    repo.conn.execute(
        """
        UPDATE contract_documents
        SET mime = %s,
            doc_class = %s,
            size_original_bytes = %s,
            sha256_original = %s,
            updated_at = now()
        WHERE id_contrato = %s AND id_contrato_archivo = %s
        """,
        (
            mime,
            doc_class,
            len(content),
            sha_original,
            id_contrato,
            id_contrato_archivo,
        ),
    )
    repo.conn.execute(
        """
        UPDATE seace_contracts
        SET pipeline_state = 'downloaded',
            updated_at = now()
        WHERE id_contrato = %s
          AND pipeline_state IN ('discovered', 'detail_fetched', 'files_listed')
        """,
        (id_contrato,),
    )

    # Gemini solo acepta PDF como documento; un TDR .docx quedaria en dead-letter.
    if doc_class in {"tdr", "eett"} and mime == "application/pdf":
        payload: dict[str, object] = {
            "id_contrato": id_contrato,
            "id_contrato_archivo": id_contrato_archivo,
        }
        if batch_id:
            payload["batch_id"] = batch_id
        if bucket:
            payload["bucket"] = bucket
        if segment is not None:
            payload["segment"] = segment
        repo.enqueue(
            "extract_tdr",
            payload,
            queue_name="llm",
            dedup_key=f"extract_tdr:{batch_id or 'default'}:{id_contrato_archivo}:{sha_original}",
            priority=3,
        )

    logger.info(
        "download_file_done",
        extra={
            "id_contrato": id_contrato,
            "id_contrato_archivo": id_contrato_archivo,
            "doc_class": doc_class,
            "size_original_bytes": len(content),
        },
    )
    return {
        "sha256_original": sha_original,
        "doc_class": doc_class,
        "size_original_bytes": len(content),
    }
