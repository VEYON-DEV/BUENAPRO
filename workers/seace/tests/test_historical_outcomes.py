from decimal import Decimal
import unittest

from buenapro_worker.historical.outcomes import classify_outcome, parse_contract_code
from buenapro_worker.jobs.process_contract import quotation_window_from_detail


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


if __name__ == "__main__":
    unittest.main()
