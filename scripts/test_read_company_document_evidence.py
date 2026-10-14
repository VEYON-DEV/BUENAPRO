"""Local reader guards. Every provider/DB boundary is stubbed; no inference."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import read_company_document_evidence as reader
import read_company_document_text as text_reader


def valid_evidence():
    return {
        "summary": "Orden de suministro; conformidad no identificada.",
        "document_types": ["orden de compra"],
        "facts": [{"field": "Objeto", "value": "Variadores", "quote": "Suministro de variadores", "page": 1}],
        "amounts": [{"amount_decimal": "1234.56", "literal": "USD 1,234.56", "currency": "USD",
                     "label": "Total orden", "tax_basis": "includes_tax", "source_type": "orden de compra",
                     "quote": "Total USD 1,234.56", "page": 2}],
        "principal_amount_index": 0,
        "classification": {"rubro": "Automatización industrial", "specialty": "Variadores",
                           "quote": "Suministro de variadores", "page": 1, "inferred": True},
        "conformity": "not_identified", "unknowns": ["Acta de conformidad"], "warnings": [],
    }


class CompanyEvidenceGuards(unittest.TestCase):
    def test_valid_decimal_currency_pages_and_inference_are_preserved(self):
        original = valid_evidence()
        self.assertEqual(reader.validate_evidence(original, 2), original)
        self.assertEqual(original["amounts"][0]["amount_decimal"], "1234.56")

    def test_invalid_decimal_formats_are_rejected_without_guessing(self):
        for value in ("1,234.56", "1.234,56", "1234,56", "-1", "0", "NaN", "1e4", "1.12345"):
            with self.subTest(value=value):
                evidence = valid_evidence()
                evidence["amounts"][0]["amount_decimal"] = value
                with self.assertRaises(ValueError):
                    reader.validate_evidence(evidence, 2)

    def test_every_source_quote_needs_a_real_original_page(self):
        for key in ("facts", "amounts", "classification"):
            for change in ({"page": 0}, {"page": 3}, {"quote": " "}):
                with self.subTest(key=key, change=change):
                    evidence = valid_evidence()
                    target = evidence[key] if key == "classification" else evidence[key][0]
                    target.update(change)
                    with self.assertRaises(ValueError):
                        reader.validate_evidence(evidence, 2)

    def test_principal_amount_cannot_be_only_tax_unknown_currency_or_outside_sources(self):
        for change in ({"currency": "unknown"}, {"tax_basis": "tax"}):
            evidence = valid_evidence()
            evidence["amounts"][0].update(change)
            with self.assertRaises(ValueError):
                reader.validate_evidence(evidence, 2)
        for index in (-1, 2):
            evidence = valid_evidence()
            evidence["principal_amount_index"] = index
            with self.assertRaises(ValueError):
                reader.validate_evidence(evidence, 2)
        evidence["principal_amount_index"] = None
        self.assertIsNone(reader.validate_evidence(evidence, 2)["principal_amount_index"])

    def test_classification_must_remain_inference(self):
        evidence = valid_evidence()
        evidence["classification"]["inferred"] = False
        with self.assertRaisesRegex(ValueError, "inference"):
            reader.validate_evidence(evidence, 2)

    def baseline(self, folder):
        content = b"%PDF-synthetic-offline-only"
        sha = hashlib.sha256(content).hexdigest()
        pdf = folder / "synthetic.pdf"
        pdf.write_bytes(content)
        document = {"id": "00000000-0000-0000-0000-000000000001", "sha256": sha,
                    "local_path": str(pdf), "metadata_json": {}}
        (folder / "baseline.private.json").write_text(json.dumps({
            "profile": {"id": "synthetic-profile"}, "documents": [document],
        }))
        (folder / "evidence").mkdir()
        return document

    def run_main_offline(self, folder, client):
        settings = SimpleNamespace(gemini_api_key="synthetic-not-real", gemini_model="synthetic-model")
        with patch("sys.argv", ["reader", "--folder", str(folder), "--generate"]), \
                patch.object(reader, "Settings", return_value=settings), \
                patch.object(reader.genai, "Client", return_value=client), \
                patch("google.genai._transformers.t_schema", return_value={}), \
                patch.object(reader, "PdfReader", return_value=SimpleNamespace(pages=[None, None])):
            reader.main()

    def test_started_journal_blocks_replay_before_any_provider_call(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            doc = self.baseline(folder)
            journal = folder / "evidence" / f"{doc['sha256']}.request.private.json"
            journal.write_text(json.dumps({"status": "started_no_retry", "sha256": doc["sha256"]}))
            client = MagicMock()
            with self.assertRaisesRegex(RuntimeError, "without a second paid call"):
                self.run_main_offline(folder, client)
            client.models.generate_content.assert_not_called()
            self.assertEqual(json.loads(journal.read_text())["status"], "started_no_retry")

    def test_malformed_response_is_preserved_and_next_run_cannot_repeat_the_request(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            doc = self.baseline(folder)
            client = MagicMock()
            response = SimpleNamespace(
                text="{not valid JSON}", candidates=[],
                usage_metadata=SimpleNamespace(prompt_token_count=None, candidates_token_count=None),
                model_dump=lambda **_: {"text": "{not valid JSON}", "usage_metadata": {}},
            )
            client.models.generate_content.return_value = response
            with self.assertRaisesRegex(RuntimeError, "preserved request"):
                self.run_main_offline(folder, client)
            self.assertEqual(client.models.generate_content.call_count, 1)
            response_path = folder / "evidence" / f"{doc['sha256']}.response.private.json"
            journal = folder / "evidence" / f"{doc['sha256']}.request.private.json"
            receipt = json.loads(journal.read_text())
            self.assertEqual(receipt["status"], "stopped_preserve_no_retry")
            self.assertTrue(receipt["response_preserved"])
            self.assertIsNone(receipt["cost_usd"])
            self.assertEqual(json.loads(response_path.read_text())["text"], "{not valid JSON}")
            with self.assertRaisesRegex(RuntimeError, "without a second paid call"):
                self.run_main_offline(folder, client)
            self.assertEqual(client.models.generate_content.call_count, 1)

    def test_exact_hash_prior_evidence_is_reused_with_no_provider_request(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            doc = self.baseline(folder)
            existing = {"sha256": doc["sha256"], "version": "company_evidence_v2", "extraction": valid_evidence()}
            receipt = folder / "evidence" / f"{doc['sha256']}.json"
            receipt.write_text(json.dumps(existing))
            before = receipt.read_bytes()
            client = MagicMock()
            self.run_main_offline(folder, client)
            client.models.generate_content.assert_not_called()
            self.assertEqual(receipt.read_bytes(), before)


class CompanyLocalTextGuards(unittest.TestCase):
    def test_cache_reuse_never_runs_ocr_or_native_text_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "test.pdf"
            pdf.write_bytes(b"%PDF-offline-test")
            sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
            output = folder / "text.json"
            existing = {"sha256": sha, "version": text_reader.VERSION, "pages": [{"page": 1, "text": "Prior source"}]}
            output.write_text(json.dumps(existing))
            with patch.object(text_reader.subprocess, "run") as process:
                self.assertEqual(text_reader.extract_pdf(pdf, sha, output), existing)
            process.assert_not_called()

    def test_current_pdf_hash_checked_before_accepting_old_text(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            pdf = folder / "test.pdf"
            pdf.write_bytes(b"changed PDF")
            output = folder / "text.json"
            output.write_text(json.dumps({"sha256": "a" * 64, "version": text_reader.VERSION}))
            with patch.object(text_reader.subprocess, "run") as process:
                with self.assertRaisesRegex(ValueError, "hash changed"):
                    text_reader.extract_pdf(pdf, "a" * 64, output)
            process.assert_not_called()

    def test_explicit_dni_redacted_without_erasing_corporate_ruc_and_order_number(self):
        text = "DNI: 12345678; RUC 20343877294; Pedido 4200085413; importe 12345678"
        cleaned = text_reader.redact_identifiers(text)
        self.assertIn("DNI: [omitido]", cleaned)
        self.assertIn("RUC 20343877294", cleaned)
        self.assertIn("Pedido 4200085413", cleaned)
        self.assertIn("importe 12345678", cleaned)


if __name__ == "__main__":
    unittest.main()
