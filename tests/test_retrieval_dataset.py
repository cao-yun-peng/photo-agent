import unittest

from scripts.eval_retrieval import score


class RetrievalScoringTests(unittest.TestCase):
    def setUp(self):
        self.corpus = [{"photo_id": i, "database_photo_id": "uuid-" + i} for i in ("a", "b", "c")]
        self.queries = [{"id": "q", "relevant_photo_ids": ["a", "b"], "hard_negative_photo_ids": ["c"], "tags": ["visual"]},
                        {"id": "empty", "relevant_photo_ids": [], "hard_negative_photo_ids": ["c"], "tags": ["zero"]}]

    def test_duplicate_consumes_rank_and_uuid_normalizes(self):
        report = score(self.queries, [{"query_id": "q", "photo_ids": ["uuid-a", "a", "b", "c"]},
                                      {"query_id": "empty", "photo_ids": []}], self.corpus)
        row = report["details"][0]
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(row["recall_at_1"], .5)
        self.assertEqual(row["recall_at_5"], 1)
        self.assertLess(row["ndcg_at_10"], 1)
        self.assertEqual(row["hard_negative_hit_at_10"], 1)
        self.assertEqual(report["overall"]["recall_at_5"]["count"], 1)
        self.assertEqual(report["overall"]["empty_accuracy"]["mean"], 1)

    def test_missing_query_rejected(self):
        with self.assertRaises(ValueError):
            score(self.queries, [{"query_id": "q", "photo_ids": []}], self.corpus)

    def test_unknown_photo_rejected(self):
        with self.assertRaises(ValueError):
            score(self.queries, [{"query_id": "q", "photo_ids": ["outside"]}, {"query_id": "empty", "photo_ids": []}], self.corpus)

    def test_empty_query_false_positive(self):
        report = score(self.queries, [{"query_id": "q", "photo_ids": []}, {"query_id": "empty", "photo_ids": ["c"]}], self.corpus)
        self.assertEqual(report["overall"]["empty_accuracy"]["mean"], 0)
        self.assertEqual(report["overall"]["mrr_at_10"]["mean"], 0)


if __name__ == "__main__":
    unittest.main()
