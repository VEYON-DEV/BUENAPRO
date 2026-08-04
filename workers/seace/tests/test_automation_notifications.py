from __future__ import annotations

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from buenapro_worker.jobs.analyze_match import clamp_score
from buenapro_worker.jobs.send_notification import notification_score_passes
from buenapro_worker.normalization.facets import derive_summary
from buenapro_worker.notifications.templates import build_notification_payload


class AutomationNotificationTests(unittest.TestCase):
    def test_notification_threshold_is_strictly_greater_than_fifty(self) -> None:
        self.assertFalse(notification_score_passes(50, 50))
        self.assertTrue(notification_score_passes(51, 50))
        self.assertTrue(notification_score_passes(85, 50))

    def test_verdict_score_clamp_keeps_current_bands(self) -> None:
        self.assertEqual(clamp_score("ambar", 90), 84)
        self.assertEqual(clamp_score("verde", 70), 85)

    def test_modality_is_normalized_without_inventing_a_value(self) -> None:
        virtual = derive_summary({"contract": {"modalidad": "Trabajo remoto virtual"}})
        mixed = derive_summary({"contract": {"modalidad": "Modalidad mixta: virtual y presencial"}})
        missing = derive_summary({"contract": {}})
        self.assertEqual(virtual["modalidad"], "virtual")
        self.assertEqual(mixed["modalidad"], "mixta")
        self.assertEqual(missing["modalidad"], "no_indicada")

    def test_telegram_template_is_short_and_explains_the_decision(self) -> None:
        payload = build_notification_payload(
            {
                "id_contrato": 83675,
                "codigo": "CM-203-2026-SEDA AYACUCHO",
                "descripcion": "Servicio de soporte técnico informático para redes y catastro",
                "descripcion_corta": "Soporte de sistemas y redes durante 135 días.",
                "entidad_nombre": "SEDA Ayacucho",
                "fec_fin_cotizacion": datetime(2026, 8, 3, 20, tzinfo=timezone.utc),
                "score": 76,
                "verdict": "ambar",
                "modalidad": "presencial",
                "fortalezas": ["Experiencia directamente relacionada", "Capacidad económica suficiente"],
                "riesgo_principal": "Confirmar especialista titulado en sistemas",
                "app_base_url": "https://app.buenapro.pe",
            }
        )
        body = payload["body"]
        self.assertIn("76/100", body)
        self.assertIn("Modalidad: Presencial", body)
        self.assertIn("Qué necesitan:", body)
        self.assertIn("Confirmar especialista", body)
        self.assertIn("/oportunidad/83675", body)
        self.assertEqual(payload["entidad_nombre"], "SEDA Ayacucho")
        self.assertIn("soporte técnico", payload["descripcion"].lower())
        self.assertLess(len(body), 1000)


if __name__ == "__main__":
    unittest.main()
