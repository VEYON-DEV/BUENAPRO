from decimal import Decimal
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

from buenapro_worker.historical.outcomes import classify_outcome, parse_contract_code
from buenapro_worker.jobs.historical_outcomes import (
    _location_parts,
    _matches_requested_object,
    _published_in_window,
    _three_year_cutoff,
    backfill_historical_outcomes,
    upsert_historical_outcome,
)
from buenapro_worker.jobs.process_contract import quotation_window_from_detail
from buenapro_worker.jobs.send_notification import verdict_meets_threshold


class HistoricalOutcomeTest(unittest.TestCase):
    def test_parse_contract_code(self) -> None:
        self.assertEqual(parse_contract_code("CM-110-2026-UNAAT"), {
            "codigo_completo": "CM-110-2026-UNAAT",
            "codigo_tipo": "CM",
            "codigo_correlativo": 110,
            "codigo_anio": 2026,
            "codigo_sigla": "UNAAT",
        })

    def test_parse_contract_code_keeps_unknown_full_code(self) -> None:
        parsed = parse_contract_code("CODIGO LIBRE")
        self.assertEqual(parsed["codigo_completo"], "CODIGO LIBRE")
        self.assertIsNone(parsed["codigo_tipo"])

    def test_parse_contract_code_accepts_institutional_sigla_with_spaces(self) -> None:
        parsed = parse_contract_code("CM-796-2026-DIRESA -ANCASH")
        self.assertEqual(parsed["codigo_sigla"], "DIRESA -ANCASH")
        self.assertEqual(parsed["codigo_correlativo"], 796)

    def test_parse_contract_code_accepts_big_correlative(self) -> None:
        parsed = parse_contract_code("CM-9437000002-2026-OACGD")
        self.assertEqual(parsed["codigo_correlativo"], 9437000002)
        self.assertEqual(parsed["codigo_sigla"], "OACGD")

    def test_classify_awarded(self) -> None:
        result = classify_outcome({
            "uitContratoItemProjectionList": [{
                "codRuc": "20605872141",
                "nomRazonSocial": "IDEAS MULTIPLES DE SISTEMAS S.A.C.",
                "precioTotal": 550,
                "nomEstadoCotiza": "ADJUDICADO",
            }]
        })
        self.assertEqual(result.state, "ADJUDICADO")
        self.assertEqual(result.price_total, Decimal("550"))
        self.assertEqual(result.supplier_ruc, "20605872141")

    def test_zero_price_is_not_market_price(self) -> None:
        result = classify_outcome({
            "uitContratoItemProjectionList": [{
                "codRuc": "20123456789",
                "nomRazonSocial": "PROVEEDOR",
                "precioTotal": 0,
            }]
        })
        self.assertEqual(result.state, "ADJUDICADO")
        self.assertIsNone(result.price_total)

    def test_classify_deserted_without_price(self) -> None:
        result = classify_outcome({
            "uitContratoItemProjectionList": [{"nomEstadoCotiza": "DESIERTO"}]
        })
        self.assertEqual(result.state, "DESIERTO")
        self.assertIsNone(result.price_total)

    def test_classify_inconclusive(self) -> None:
        result = classify_outcome({"uitContratoItemProjectionList": [{}]})
        self.assertEqual(result.state, "SIN_RESULTADO")

    def test_extracts_quotation_window_from_detail_stages(self) -> None:
        start, end = quotation_window_from_detail({
            "uitContratoEtapaProjectionList": [
                {
                    "idEtapaContrato": 1,
                    "nomEtapaContrato": "ETAPA DE CONSULTAS",
                    "fecIni": "31/07/2026 18:00:00",
                    "fecFin": "31/07/2026 19:00:00",
                },
                {
                    "idEtapaContrato": 2,
                    "nomEtapaContrato": "ETAPA DE COTIZACIÓN",
                    "fecIni": "31/07/2026 18:16:44",
                    "fecFin": "01/08/2026 09:30:00",
                },
            ]
        })
        self.assertEqual(start.isoformat(), "2026-07-31T23:16:44+00:00")
        self.assertEqual(end.isoformat(), "2026-08-01T14:30:00+00:00")

    def test_missing_quotation_stage_has_no_window(self) -> None:
        self.assertEqual(quotation_window_from_detail({}), (None, None))

    def test_normalizes_seace_location_for_market_filters(self) -> None:
        self.assertEqual(
            _location_parts("LIMA / LIMA / SAN ISIDRO"),
            ("LIMA", "LIMA", "SAN ISIDRO"),
        )

    @patch("buenapro_worker.jobs.historical_outcomes.sync_historical_prod6_opportunity")
    def test_historical_upsert_binds_one_parameter_per_column(self, sync_identity: MagicMock) -> None:
        repo = MagicMock()
        repo.conn.execute.return_value.fetchone.return_value = None

        result = upsert_historical_outcome(
            repo,
            detail={
                "uitContratoCompletoProjection": {
                    "idContrato": 83675,
                    "desContratacion": "CM-110-2026-UNAAT",
                    "desObjetoContrato": "Servicio de soporte informatico",
                },
                "uitContratoItemProjectionList": [{"nomEstadoCotiza": "DESIERTO"}],
            },
            segment=81,
        )

        insert_call = next(
            call
            for call in repo.conn.execute.call_args_list
            if "INSERT INTO historical_contract_outcomes" in call.args[0]
        )
        sql, params = insert_call.args
        self.assertEqual(sql.count("%s"), 27)
        self.assertEqual(len(params), 27)
        self.assertEqual(params[1], 2)
        self.assertEqual(result, "DESIERTO")
        sync_identity.assert_called_once()

    @patch("buenapro_worker.jobs.historical_outcomes.sync_historical_prod6_opportunity")
    def test_historical_upsert_uses_goods_object_from_detail(self, sync_identity: MagicMock) -> None:
        repo = MagicMock()
        repo.conn.execute.return_value.fetchone.return_value = None
        upsert_historical_outcome(
            repo,
            detail={
                "uitContratoCompletoProjection": {
                    "idContrato": 83676,
                    "idObjetoContrato": 1,
                    "desContratacion": "CM-111-2026-UNAAT",
                },
                "uitContratoItemProjectionList": [{}],
            },
            segment=43,
            objeto_codigo=1,
        )
        insert_call = next(call for call in repo.conn.execute.call_args_list
                           if "INSERT INTO historical_contract_outcomes" in call.args[0])
        self.assertEqual(insert_call.args[1][1], 1)
        self.assertIn("objeto_codigo=EXCLUDED.objeto_codigo", insert_call.args[0])
        self.assertEqual(sync_identity.call_args.kwargs["objeto_codigo"], 1)

    def test_historical_window_uses_actual_publication_date(self) -> None:
        cutoff = _three_year_cutoff(datetime(2026, 9, 29, tzinfo=timezone.utc))
        self.assertEqual(cutoff.year, 2023)
        self.assertFalse(_published_in_window(
            {"uitContratoCompletoProjection": {"fecPublica": "01/01/2020 12:00:00"}},
            {"fecPublica": "01/09/2026 12:00:00"}, cutoff,
        ))
        self.assertTrue(_published_in_window(
            {"uitContratoCompletoProjection": {"fecPublica": "28/09/2026 12:00:00"}},
            {}, cutoff,
        ))
        self.assertFalse(_published_in_window({}, {}, cutoff))

    def test_rejects_mismatched_object_from_search_or_detail(self) -> None:
        self.assertFalse(_matches_requested_object(
            {"uitContratoCompletoProjection": {"idObjetoContrato": 2}},
            {"idObjetoContrato": 1}, 1,
        ))
        self.assertFalse(_matches_requested_object({}, {"idObjetoContrato": 2}, 1))
        self.assertTrue(_matches_requested_object({}, {"idObjetoContrato": 1}, 1))

    @patch("buenapro_worker.jobs.historical_outcomes.SeaceClient")
    def test_goods_backfill_uses_separate_checkpoint_and_skips_old_contracts(self, client_class: MagicMock) -> None:
        repo = MagicMock()
        repo.conn.execute.return_value.fetchone.return_value = None
        search = MagicMock()
        search.id_contrato = 42
        search.model_dump.return_value = {
            "idContrato": 42,
            "idObjetoContrato": 1,
            "fecPublica": "01/01/2020 12:00:00",
        }
        response = MagicMock()
        response.data = [search]
        response.pageable.total_elements = 1
        response.pageable.page_size = 50
        client = client_class.return_value.__enter__.return_value
        client.search_contracts.return_value = response
        client.contract_detail.return_value = {
            "uitContratoCompletoProjection": {
                "idContrato": 42,
                "idObjetoContrato": 1,
                "fecPublica": "01/01/2020 12:00:00",
            },
        }

        stats = backfill_historical_outcomes(MagicMock(), repo, segment=43, objeto_codigo=1, include_files=False)

        self.assertEqual(stats["saved"], 0)
        self.assertEqual(stats["skipped_out_of_range"], 1)
        self.assertEqual(client.search_contracts.call_args.kwargs["objeto"], 1)
        self.assertEqual(repo.conn.execute.call_args_list[0].args[1], (1, "43"))
        self.assertFalse(any("INSERT INTO historical_contract_outcomes" in call.args[0]
                             for call in repo.conn.execute.call_args_list))

    @patch("buenapro_worker.jobs.historical_outcomes.sync_historical_prod6_opportunity")
    @patch("buenapro_worker.jobs.historical_outcomes.SeaceClient")
    def test_goods_backfill_saves_recent_goods_as_object_one(
        self, client_class: MagicMock, sync_identity: MagicMock,
    ) -> None:
        repo = MagicMock()
        repo.conn.execute.return_value.fetchone.return_value = None
        published = datetime.now(ZoneInfo("America/Lima")).strftime("%d/%m/%Y %H:%M:%S")
        search = MagicMock()
        search.id_contrato = 43
        search.model_dump.return_value = {
            "idContrato": 43,
            "idObjetoContrato": 1,
            "desContratacion": "CM-43-2026-UNAAT",
            "fecPublica": published,
        }
        response = MagicMock()
        response.data = [search]
        response.pageable.total_elements = 1
        response.pageable.page_size = 50
        client = client_class.return_value.__enter__.return_value
        client.search_contracts.return_value = response
        client.contract_detail.return_value = {
            "uitContratoCompletoProjection": {
                "idContrato": 43,
                "idObjetoContrato": 1,
                "fecPublica": published,
            },
            "uitContratoItemProjectionList": [{}],
        }

        stats = backfill_historical_outcomes(MagicMock(), repo, segment=43, objeto_codigo=1, include_files=False)

        self.assertEqual(stats["saved"], 1)
        sync_identity.assert_called_once()
        insert = next(call for call in repo.conn.execute.call_args_list
                      if "INSERT INTO historical_contract_outcomes" in call.args[0])
        self.assertEqual(insert.args[1][1], 1)
        self.assertTrue(any("ON CONFLICT (objeto_codigo, cubso_segmento)" in call.args[0]
                            for call in repo.conn.execute.call_args_list))

    def test_notification_threshold_only_accepts_relevant_verdicts(self) -> None:
        self.assertTrue(verdict_meets_threshold("verde", "verde"))
        self.assertTrue(verdict_meets_threshold("verde", "ambar"))
        self.assertTrue(verdict_meets_threshold("ambar", "ambar"))
        self.assertFalse(verdict_meets_threshold("ambar", "verde"))
        self.assertFalse(verdict_meets_threshold("rojo", "ambar"))


if __name__ == "__main__":
    unittest.main()
