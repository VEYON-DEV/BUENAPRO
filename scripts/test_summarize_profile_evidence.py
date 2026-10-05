import copy
import unittest
from summarize_profile_evidence import VERSION, contract_summary, digest, valid_cache


class EvidenceSummaryTests(unittest.TestCase):
    def fixtures(self):
        doc = {"id": "a", "filename": "evidence.pdf", "content": b"%PDF-evidence"}
        sha = digest(doc["content"])
        cache = {"version": VERSION, "sha256": sha, "extraction": {
            "summary": "Orden de compra, sin ejecución acreditada.",
            "facts": [{"field": "monto total", "value": "USD 41,023", "page": 1}],
            "unknowns": ["conformidad"], "warnings": [], "inferred_labels": []}}
        record = {"objeto": "Hardware", "monto": 157000, "descripcion": "Declaración antigua",
            "documents": [{"id": "a"}, {"id": "a"}, {"id": "missing"}], "custom": {"preserve": True}}
        return doc, cache, record

    def test_summary_deduplicates_and_reports_missing(self):
        doc, cache, record = self.fixtures()
        text, meta = contract_summary(record, {"a": doc}, {cache["sha256"]: cache})
        self.assertEqual(len(meta["sources"]), 1)
        self.assertEqual(meta["missing_document_ids"], ["missing"])
        self.assertIn("USD 41,023", text)
        self.assertIn("p. 1", text)
        self.assertTrue(meta["amount_review_required"])

    def test_preserves_original_data_and_description(self):
        doc, cache, record = self.fixtures()
        original = copy.deepcopy(record)
        _, meta = contract_summary(record, {"a": doc}, {cache["sha256"]: cache})
        self.assertEqual(record, original)
        self.assertEqual(meta["original_description"], "Declaración antigua")
        record["documentary_summary"] = meta
        record["descripcion"] = "Resumen posterior"
        _, rerun = contract_summary(record, {"a": doc}, {cache["sha256"]: cache})
        self.assertEqual(rerun["original_description"], "Declaración antigua")

    def test_cache_version_and_hash_invalidation(self):
        _, cache, _ = self.fixtures()
        self.assertTrue(valid_cache(cache, cache["sha256"]))
        self.assertFalse(valid_cache(cache, "modified"))
        self.assertFalse(valid_cache({**cache, "version": "old"}, cache["sha256"]))

    def test_uncached_available_pdf_fails_before_apply(self):
        doc, _, record = self.fixtures()
        with self.assertRaisesRegex(ValueError, "Missing extraction cache"):
            contract_summary(record, {"a": doc}, {})


if __name__ == "__main__":
    unittest.main()
