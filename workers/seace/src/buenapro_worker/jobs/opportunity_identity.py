from __future__ import annotations

import json

from buenapro_worker.queue.repository import JobRepository
from buenapro_worker.seace.schemas import SearchItem


OBJECT_TYPES = {
    1: "good",
    2: "service",
    3: "work",
    4: "works_consulting",
}

LIFECYCLE_STAGES = {
    2: "open",
    3: "evaluation",
    4: "closed",
}


def sync_prod6_opportunity(repo: JobRepository, item: SearchItem) -> None:
    """Keep the source identity and canonical classification in the same transaction.

    A source ID is an exact-match key. Other sources may be linked to this UUID
    later, but a title, buyer name or amount must never create that link alone.
    """
    external_id = str(item.id_contrato)
    repo.conn.execute(
        "SELECT pg_advisory_xact_lock(hashtext('seace_prod6'), hashtext(%s))",
        (external_id,),
    )
    source = repo.conn.execute(
        """
        SELECT opportunity_id
        FROM opportunity_sources
        WHERE source_key = 'seace_prod6'
          AND id_kind = 'id_contrato'
          AND external_id = %s
        """,
        (external_id,),
    ).fetchone()
    object_type = OBJECT_TYPES.get(item.objeto_codigo, "unknown")
    lifecycle_stage = LIFECYCLE_STAGES.get(item.estado_codigo, "unknown")
    native_values = json.dumps(
        {
            "codigo": item.codigo,
            "objeto_codigo": item.objeto_codigo,
            "estado_codigo": item.estado_codigo,
        },
        ensure_ascii=False,
    )

    if source is None:
        opportunity = repo.conn.execute(
            """
            INSERT INTO opportunities (
              record_kind, object_type, procurement_method, lifecycle_stage,
              participation_access, actionability
            )
            VALUES ('opportunity', %s, 'minor_purchase', %s,
                    'registration_required', 'unknown')
            RETURNING id
            """,
            (object_type, lifecycle_stage),
        ).fetchone()
        if opportunity is None:
            raise RuntimeError("Failed to create canonical opportunity")
        opportunity_id = opportunity["id"]
        repo.conn.execute(
            """
            INSERT INTO opportunity_sources (
              opportunity_id, source_key, id_kind, external_id, native_values
            )
            VALUES (%s, 'seace_prod6', 'id_contrato', %s, %s::jsonb)
            """,
            (opportunity_id, external_id, native_values),
        )
    else:
        opportunity_id = source["opportunity_id"]
        repo.conn.execute(
            """
            UPDATE opportunities
            SET object_type = %s,
                lifecycle_stage = %s,
                updated_at = now()
            WHERE id = %s
            """,
            (object_type, lifecycle_stage, opportunity_id),
        )
        repo.conn.execute(
            """
            UPDATE opportunity_sources
            SET native_values = %s::jsonb
            WHERE source_key = 'seace_prod6'
              AND id_kind = 'id_contrato'
              AND external_id = %s
            """,
            (native_values, external_id),
        )

    repo.conn.execute(
        """
        UPDATE seace_contracts
        SET opportunity_id = %s
        WHERE id_contrato = %s
          AND opportunity_id IS DISTINCT FROM %s
        """,
        (opportunity_id, item.id_contrato, opportunity_id),
    )


def refresh_prod6_classification(repo: JobRepository, id_contrato: int) -> None:
    """Reflect lifecycle changes discovered by detail refresh, not only search polls."""
    repo.conn.execute(
        """
        UPDATE opportunities o
        SET object_type = CASE c.objeto_codigo
              WHEN 1 THEN 'good'
              WHEN 2 THEN 'service'
              WHEN 3 THEN 'work'
              WHEN 4 THEN 'works_consulting'
              ELSE 'unknown'
            END,
            lifecycle_stage = CASE c.estado_codigo
              WHEN 2 THEN 'open'
              WHEN 3 THEN 'evaluation'
              WHEN 4 THEN 'closed'
              ELSE 'unknown'
            END,
            updated_at = now()
        FROM seace_contracts c
        WHERE c.id_contrato = %s
          AND c.opportunity_id = o.id
          AND (o.object_type IS DISTINCT FROM CASE c.objeto_codigo
                WHEN 1 THEN 'good' WHEN 2 THEN 'service'
                WHEN 3 THEN 'work' WHEN 4 THEN 'works_consulting'
                ELSE 'unknown' END
            OR o.lifecycle_stage IS DISTINCT FROM CASE c.estado_codigo
                WHEN 2 THEN 'open' WHEN 3 THEN 'evaluation'
                WHEN 4 THEN 'closed' ELSE 'unknown' END)
        """,
        (id_contrato,),
    )
    repo.conn.execute(
        """
        UPDATE opportunity_sources s
        SET native_values = s.native_values || jsonb_build_object(
          'objeto_codigo', c.objeto_codigo, 'estado_codigo', c.estado_codigo
        )
        FROM seace_contracts c
        WHERE c.id_contrato = %s
          AND s.opportunity_id = c.opportunity_id
          AND s.source_key = 'seace_prod6'
          AND s.id_kind = 'id_contrato'
          AND s.external_id = c.id_contrato::text
        """,
        (id_contrato,),
    )
