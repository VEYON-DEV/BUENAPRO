import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from buenapro_worker.extraction.gemini import (
    EETT_PROMPT_VERSION,
    PROMPT_VERSION,
    GeminiExtractor,
    prompt_version_for_doc_class,
)
from buenapro_worker.extraction.schemas import EettExtractionV1, TdrExtractionV2
from buenapro_worker.jobs.download_file import download_file_job
from buenapro_worker.jobs.extract_tdr import extract_tdr_job, find_reusable_extraction


class GoodsDocumentsTest(unittest.TestCase):
    def _download(self, *, objeto_codigo: int, categoria: int, filename: str):
        repo = MagicMock()
        repo.conn.execute.return_value.fetchone.return_value = {
            "filename": filename,
            "categoria": categoria,
            "objeto_codigo": objeto_codigo,
        }
        with patch("buenapro_worker.jobs.download_file.SeaceClient") as client_class:
            client_class.return_value.__enter__.return_value.download_file.return_value = b"%PDF-1.7\n"
            result = download_file_job(MagicMock(), repo, id_contrato=10, id_contrato_archivo=20)
        return result, repo

    def test_goods_category_one_is_eett_even_with_generic_name(self):
        result, repo = self._download(objeto_codigo=1, categoria=1, filename="P513.pdf")
        self.assertEqual(result["doc_class"], "eett")
        self.assertEqual(repo.enqueue.call_args.args[0], "extract_tdr")

    def test_service_category_one_remains_tdr_even_with_eett_filename(self):
        result, repo = self._download(objeto_codigo=2, categoria=1, filename="EETT.pdf")
        self.assertEqual(result["doc_class"], "tdr")
        self.assertEqual(repo.enqueue.call_args.args[0], "extract_tdr")

    def test_category_two_does_not_trigger_extraction(self):
        result, repo = self._download(objeto_codigo=1, categoria=2, filename="Anexo.pdf")
        self.assertEqual(result["doc_class"], "otro")
        repo.enqueue.assert_not_called()

    def test_gemini_uses_distinct_schema_and_prompt_for_goods(self):
        settings = SimpleNamespace(
            gemini_api_key="test-key", gemini_model="gemini-3.1-flash-lite",
            gemini_fallback_model="gemini-2.5-flash",
        )
        payload = {
            "contract": {"objeto": "Compra de computadoras", "plazo_dias": 30},
            "goods": {
                "items": [{"nombre": "Laptop", "cantidad": 10, "unidad_medida": "unidad",
                           "especificaciones": ["RAM 16 GB"]}],
                "entrega": {"lugar": "Lima", "plazo_texto": "30 dias", "plazo_dias": 30},
                "garantia": {"plazo_texto": "12 meses", "cobertura": "defectos de fabrica"},
            },
            "summary": {"descripcion_corta": "Diez computadoras para entrega en Lima."},
        }
        response = SimpleNamespace(
            text=json.dumps(payload), candidates=[],
            usage_metadata=SimpleNamespace(prompt_token_count=100, candidates_token_count=50),
        )
        with patch("buenapro_worker.extraction.gemini.genai.Client") as client_class:
            client_class.return_value.models.generate_content.return_value = response
            extractor = GeminiExtractor(settings)
            result = extractor.extract(b"%PDF-1.7", doc_class="eett")
            config = client_class.return_value.models.generate_content.call_args.kwargs["config"]
            self.assertIs(config.response_schema, EettExtractionV1)
            self.assertIn("Especificaciones Tecnicas", config.system_instruction)
        self.assertEqual(result.prompt_version, EETT_PROMPT_VERSION)
        self.assertEqual(result.extraction.goods.items[0].cantidad, 10)
        self.assertEqual(result.extraction.goods.garantia.plazo_texto, "12 meses")

    def test_gemini_keeps_tdr_schema_and_version_for_services(self):
        settings = SimpleNamespace(
            gemini_api_key="test-key", gemini_model="gemini-3.1-flash-lite",
            gemini_fallback_model="gemini-2.5-flash",
        )
        response = SimpleNamespace(
            text='{"contract":{"objeto":"Servicio de soporte"}}', candidates=[],
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
        )
        with patch("buenapro_worker.extraction.gemini.genai.Client") as client_class:
            client_class.return_value.models.generate_content.return_value = response
            result = GeminiExtractor(settings).extract(b"%PDF-1.7")
            config = client_class.return_value.models.generate_content.call_args.kwargs["config"]
            self.assertIs(config.response_schema, TdrExtractionV2)
        self.assertEqual(result.prompt_version, PROMPT_VERSION)
        self.assertEqual(prompt_version_for_doc_class("tdr"), PROMPT_VERSION)

    def test_goods_without_item_details_requires_review(self):
        settings = SimpleNamespace(
            gemini_api_key="test-key", gemini_model="gemini-3.1-flash-lite",
            gemini_fallback_model="gemini-2.5-flash",
        )
        response = SimpleNamespace(
            text='{"summary":{"descripcion_corta":"Compra de bienes"}}', candidates=[],
            usage_metadata=SimpleNamespace(prompt_token_count=1, candidates_token_count=1),
        )
        with patch("buenapro_worker.extraction.gemini.genai.Client") as client_class:
            client_class.return_value.models.generate_content.return_value = response
            result = GeminiExtractor(settings).extract(b"%PDF-1.7", doc_class="eett")
        self.assertTrue(result.requires_human_review)

    def test_extract_job_selects_goods_prompt_for_reuse_and_generation(self):
        repo = MagicMock()

        def execute(sql, params=()):
            cursor = MagicMock()
            if "SELECT d.id" in sql:
                cursor.fetchone.return_value = {
                    "id": 7, "mime": "application/pdf", "sha256_original": "abc",
                    "doc_class": "tdr", "objeto_codigo": 1,
                }
            elif "INSERT INTO tdr_extractions" in sql:
                cursor.fetchone.return_value = {"id": 88}
            else:
                cursor.fetchone.return_value = None
            return cursor

        repo.conn.execute.side_effect = execute
        extraction = SimpleNamespace(
            model="gemini-3.1-flash-lite", prompt_version=EETT_PROMPT_VERSION,
            schema_version="eett_extraction_schema_v1", input_tokens=1,
            output_tokens=1, cost_usd=0.1, raw_json={"goods": {}},
            summary_json={"descripcion_corta": "Bienes"}, requires_human_review=False,
        )
        with (
            patch("buenapro_worker.jobs.extract_tdr.find_reusable_extraction", return_value=None) as reuse,
            patch("buenapro_worker.jobs.extract_tdr.SeaceClient") as client_class,
            patch("buenapro_worker.jobs.extract_tdr.GeminiExtractor") as extractor_class,
        ):
            client_class.return_value.__enter__.return_value.download_file.return_value = b"%PDF-1.7"
            extractor_class.return_value.extract.return_value = extraction
            result = extract_tdr_job(MagicMock(), repo, id_contrato=10, id_contrato_archivo=20)
            self.assertEqual(reuse.call_args.kwargs["prompt_version"], EETT_PROMPT_VERSION)
            self.assertEqual(extractor_class.return_value.extract.call_args.kwargs["doc_class"], "eett")
        self.assertEqual(result, 88)

    def test_reuse_query_filters_by_prompt_version(self):
        repo = MagicMock()
        repo.conn.execute.return_value.fetchone.return_value = None
        find_reusable_extraction(
            repo, document_id=7, sha256_original="abc", prompt_version=EETT_PROMPT_VERSION
        )
        self.assertEqual(repo.conn.execute.call_args.args[1][-1], EETT_PROMPT_VERSION)


if __name__ == "__main__":
    unittest.main()
