"""Persist document-backed participation terms on the canonical opportunity."""
from __future__ import annotations

import json

from buenapro_worker.normalization.participation import derive_participation


def persist_participation(repo, *, opportunity_id, raw: dict, extraction_id: int,
                          source: str, document_sha256: str | None) -> None:
    if opportunity_id is None:
        return
    terms = derive_participation(raw)
    payload = {**terms, "source": source, "extraction_id": extraction_id,
               "document_sha256": document_sha256}
    repo.conn.execute(
        """UPDATE opportunities SET consortium_status = %s,
                  subcontracting_status = %s, participation_terms_json = %s::jsonb,
                  updated_at = now() WHERE id = %s""",
        (terms["consorcio"]["status"], terms["subcontratacion"]["status"],
         json.dumps(payload, ensure_ascii=False), opportunity_id),
    )
