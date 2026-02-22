import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

SERVICES_DIR = Path(__file__).resolve().parents[1] / "app" / "services"
if str(SERVICES_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICES_DIR))

from query_rewrite import (  # type: ignore
    build_multi_queries,
    build_retrieval_query,
    coerce_keyword_dimensions,
    format_keyword_dimensions,
    parse_keyword_dimensions,
    require_keyword_dimensions,
    validate_keyword_dimensions,
)


class QueryRewriteTestCase(unittest.TestCase):
    def test_validate_keyword_dimensions_success(self):
        payload = {
            "topic_terms": ["cloud storage", "backup"],
            "entity_terms": ["invoice.pdf", "contract.docx"],
            "time_terms": ["2025 Q4"],
            "file_type_terms": ["pdf"],
            "action_terms": ["summarize"],
            "synonym_terms": ["archive"],
        }
        dimensions = validate_keyword_dimensions(payload)
        self.assertEqual(dimensions.topic_terms, ["cloud storage", "backup"])
        self.assertEqual(
            dimensions.entity_terms,
            ["invoice.pdf", "contract.docx"],
        )
        self.assertEqual(dimensions.time_terms, ["2025 Q4"])
        self.assertEqual(dimensions.file_type_terms, ["pdf"])
        self.assertEqual(dimensions.action_terms, ["summarize"])
        self.assertEqual(dimensions.synonym_terms, ["archive"])

    def test_validate_keyword_dimensions_rejects_extra_keys(self):
        payload = {
            "topic_terms": ["contract"],
            "entity_terms": [],
            "time_terms": [],
            "file_type_terms": [],
            "action_terms": [],
            "synonym_terms": [],
            "extra_terms": ["bad"],
        }
        with self.assertRaises(ValidationError):
            validate_keyword_dimensions(payload)

    def test_validate_keyword_dimensions_rejects_wrong_field_type(self):
        payload = {
            "topic_terms": "contract",
            "entity_terms": [],
            "time_terms": [],
            "file_type_terms": [],
            "action_terms": [],
            "synonym_terms": [],
        }
        with self.assertRaises(ValidationError):
            validate_keyword_dimensions(payload)

    def test_parse_keyword_dimensions_fallback_for_invalid_json(self):
        dimensions = parse_keyword_dimensions("roadmap, sprint plan", "")
        self.assertEqual(dimensions.topic_terms, ["roadmap", "sprint plan"])

    def test_coerce_keyword_dimensions_fallback_to_question(self):
        dimensions = coerce_keyword_dimensions(
            {
                "topic_terms": "contract",
                "entity_terms": [],
                "time_terms": [],
                "file_type_terms": [],
                "action_terms": [],
                "synonym_terms": [],
            },
            "请总结合同",
        )
        self.assertEqual(dimensions.topic_terms, ["请总结合同"])

    def test_require_keyword_dimensions_rejects_non_object(self):
        with self.assertRaises(ValueError):
            require_keyword_dimensions("topic_terms: contract")

    def test_require_keyword_dimensions_accepts_valid_object(self):
        dimensions = require_keyword_dimensions(
            {
                "topic_terms": ["contract"],
                "entity_terms": [],
                "time_terms": [],
                "file_type_terms": [],
                "action_terms": [],
                "synonym_terms": [],
            }
        )
        self.assertEqual(dimensions.topic_terms, ["contract"])

    def test_build_retrieval_query_merges_question_and_dimensions(self):
        dimensions = coerce_keyword_dimensions(
            {
                "topic_terms": ["contract", "vendor"],
                "entity_terms": ["acme"],
                "time_terms": [],
                "file_type_terms": ["pdf"],
                "action_terms": ["summarize"],
                "synonym_terms": ["agreement"],
            }
        )
        query = build_retrieval_query("请总结这份合同", dimensions)
        self.assertEqual(
            query,
            "请总结这份合同 contract vendor acme pdf summarize agreement",
        )

    def test_format_keyword_dimensions(self):
        dimensions = coerce_keyword_dimensions(
            {
                "topic_terms": ["contract"],
                "entity_terms": ["acme"],
                "time_terms": [],
                "file_type_terms": ["pdf"],
                "action_terms": [],
                "synonym_terms": ["agreement"],
            }
        )
        display = format_keyword_dimensions(dimensions)
        self.assertIn("主题: contract", display)
        self.assertIn("实体: acme", display)
        self.assertIn("类型: pdf", display)
        self.assertIn("同义扩展: agreement", display)

    def test_build_multi_queries_generates_dimension_queries(self):
        dimensions = coerce_keyword_dimensions(
            {
                "topic_terms": ["contract"],
                "entity_terms": ["acme"],
                "time_terms": ["2025 q4"],
                "file_type_terms": ["pdf"],
                "action_terms": ["summarize"],
                "synonym_terms": ["agreement"],
            }
        )
        queries = build_multi_queries("请总结合同", dimensions, max_queries=6)
        self.assertGreaterEqual(len(queries), 2)
        self.assertIn("请总结合同", queries)
        self.assertIn("contract", " ".join(queries))

    def test_build_multi_queries_respects_max_queries(self):
        dimensions = coerce_keyword_dimensions(
            {
                "topic_terms": ["contract"],
                "entity_terms": ["acme"],
                "time_terms": ["2025 q4"],
                "file_type_terms": ["pdf"],
                "action_terms": ["summarize"],
                "synonym_terms": ["agreement"],
            }
        )
        queries = build_multi_queries("请总结合同", dimensions, max_queries=3)
        self.assertLessEqual(len(queries), 3)


if __name__ == "__main__":
    unittest.main()
