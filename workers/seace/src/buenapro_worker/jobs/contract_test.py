from __future__ import annotations

import logging
from typing import Any

from buenapro_worker.seace.client import SeaceClient
from buenapro_worker.settings import Settings


logger = logging.getLogger(__name__)


def run_contract_test(settings: Settings, *, anio: int) -> dict[str, Any]:
    segments_to_try = settings.allowed_segments or [None]
    tested: list[dict[str, int | None]] = []

    with SeaceClient(settings) as client:
        for objeto in settings.allowed_codigo_objeto:
            search = None
            tested_segment = None
            for segment in segments_to_try:
                candidate = client.search_contracts(
                    anio=anio,
                    estado=settings.primary_estado_contrato,
                    objeto=objeto,
                    segmento=segment,
                    page=1,
                    page_size=1,
                )
                search = candidate
                tested_segment = segment
                if candidate.data:
                    if candidate.data[0].objeto_codigo != objeto:
                        raise ValueError(f"SEACE search returned object {candidate.data[0].objeto_codigo} for requested {objeto}")
                    break
            if search is None:
                raise RuntimeError(f"No SEACE search response was tested for object {objeto}")
            tested.append({
                "objeto": objeto,
                "segment": tested_segment,
                "search_items": len(search.data),
                "total_elements": search.pageable.total_elements,
            })
        segments = client.list_cubso_segments(anio)

    result = {
        "ok": True,
        "tested": tested,
        "segments": len(segments),
    }
    logger.info("seace_contract_test_ok", extra=result)
    return result
