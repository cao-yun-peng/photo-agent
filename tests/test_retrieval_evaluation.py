"""Boundary and independently calculated fixtures for the offline evaluator."""

import math
import unittest

from scripts.retrieval_eval.metrics import paired_cluster_bootstrap, score_results


CORPUS = list("abcdef")


def query(query_id="q", relevant=None, negative=None, *, family="family-one", tags=None):
    relevant = ["a"] if relevant is None else relevant
    return {"id": query_id, "relevant_photo_ids": relevant,
            "hard_negative_photo_ids": negative or [], "expected_empty": not relevant,
            "family_id": family, "tags": tags or ["visual"]}


def result(query_id="q", returned=None, **overrides):
    record = {"query_id": query_id, "returned_photo_ids": returned or [],
              "success": True, "stop_reason": "completed", "variant": "A",
              "latency_ms": 10.0, "model_calls": 1, "visual_calls": 0,
              "input_tokens": 100, "output_tokens": 20, "estimated_cost_cny": .001}
    record.update(overrides)
    return record


class RetrievalEvaluationTests(unittest.TestCase):
    def score(self, queries, results):
        return score_results(queries, results, CORPUS)

    def test_single_positive_precision_uses_fixed_five_denominator(self):
        aggregate = self.score([query()], [result(returned=["a"])])["aggregate"]
        self.assertEqual(aggregate["precision_at_5"], .2)
        self.assertEqual(aggregate["recall_at_5"], 1)
        self.assertEqual(aggregate["hit_at_1"], 1)
        self.assertEqual(aggregate["mrr_at_5"], 1)
        self.assertEqual(aggregate["zero_result_accuracy"], None)
        self.assertEqual(aggregate["metric_denominators"]["precision_at_5"], 1)

    def test_failed_or_incomplete_empty_never_gets_correct_rejection_credit(self):
        for overrides in [
            {"success": False}, {"stop_reason": "verification_incomplete"},
            {"stop_reason": "verification_unavailable"}, {"stop_reason": "deadline_exceeded"},
            {"stop_reason": "budget_exhausted"}, {"error_code": "provider_failure"},
        ]:
            with self.subTest(overrides=overrides):
                aggregate = self.score([query(relevant=[])], [result(**overrides)])["aggregate"]
                self.assertEqual(aggregate["zero_result_accuracy"], 0)
                self.assertEqual(aggregate["operational_error_rate"], 1)
                self.assertEqual(aggregate["mean_loss"], 1)
        aggregate = self.score([query(relevant=[])], [result()])["aggregate"]
        self.assertEqual(aggregate["zero_result_accuracy"], 1)
        self.assertEqual(aggregate["mean_loss"], 0)

    def test_duplicate_occupies_rank_without_double_relevance_credit(self):
        row = self.score([query(relevant=["a", "b"])],
                         [result(returned=["a", "a", "c", "d", "e", "b"])])["per_query"][0]
        self.assertEqual(row["duplicate_count"], 1)
        self.assertEqual(row["result_count"], 6)
        self.assertEqual(row["recall_at_5"], .5)
        self.assertEqual(row["precision_at_5"], .2)
        self.assertAlmostEqual(row["ndcg_at_5"], 1 / (1 + 1 / math.log2(3)))
        self.assertEqual(row["returned_photo_ids"], ["a", "a", "c", "d", "e", "b"])

    def test_multi_positive_ndcg_mrr_and_recall_are_not_hit_rate(self):
        row = self.score([query(relevant=["a", "b", "c"])],
                         [result(returned=["d", "a", "e", "b"])])["per_query"][0]
        self.assertEqual(row["recall_at_1"], 0)
        self.assertEqual(row["recall_at_5"], 2 / 3)
        self.assertEqual(row["hit_at_5"], 1)
        self.assertEqual(row["mrr_at_5"], .5)
        expected = (1 / math.log2(3) + 1 / math.log2(5)) / (1 + 1 / math.log2(3) + .5)
        self.assertAlmostEqual(row["ndcg_at_5"], expected)

    def test_selection_loss_matches_protocol_query_weights(self):
        queries = [query("partial", ["a", "b"], ["c"]),
                   query("empty-failure", [], ["c"]),
                   query("empty-correct", []), query("false-positive", [], ["c"])]
        results = [result("partial", ["a", "c"]), result("empty-failure", success=False),
                   result("empty-correct"), result("false-positive", ["c"])]
        report = self.score(queries, results)
        self.assertEqual([row["loss"] for row in report["per_query"]], [1.0, 1.0, 0.0, 1.5])
        self.assertEqual(report["aggregate"]["mean_loss"], 3.5 / 4)
        self.assertEqual(report["aggregate"]["hard_negative_hit_at_5"], .5)
        self.assertEqual(report["aggregate"]["zero_result_accuracy"], 1 / 3)

    def test_positive_failed_attempt_preserves_observed_quality_separately(self):
        aggregate = self.score([query()], [result(returned=["a"], success=False)])["aggregate"]
        self.assertEqual(aggregate["recall_at_5"], 1)
        self.assertEqual(aggregate["mean_loss"], 0)
        self.assertEqual(aggregate["operational_error_rate"], 1)

    def test_hard_negative_at_six_does_not_count_at_five(self):
        row = self.score([query(negative=["f"])],
                         [result(returned=list("abcdef"))])["per_query"][0]
        self.assertEqual(row["hard_negative_hit_at_5"], 0)

    def test_missing_duplicate_and_unknown_query_results_rejected(self):
        for results in [[], [result(), result()], [result("other")]]:
            with self.subTest(results=results), self.assertRaises(ValueError):
                self.score([query()], results)
        with self.assertRaises(ValueError):
            self.score([query(), query()], [result()])

    def test_unknown_photo_or_invalid_label_rejected(self):
        with self.assertRaises(ValueError):
            self.score([query()], [result(returned=["outside"])])
        for invalid in [query(relevant=["outside"]), query(negative=["a"]), query(relevant=["a", "a"])]:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.score([invalid], [result()])

    def test_mixed_variants_and_invalid_execution_values_rejected(self):
        with self.assertRaises(ValueError):
            self.score([query("one"), query("two")], [result("one"), result("two", variant="B")])
        for overrides in [{"success": "false"}, {"latency_ms": float("nan")},
                          {"estimated_cost_cny": float("inf")}, {"model_calls": -1},
                          {"input_tokens": 1.5}, {"visual_calls": True}]:
            with self.subTest(overrides=overrides), self.assertRaises(ValueError):
                self.score([query()], [result(**overrides)])

    def test_slices_count_all_tags_and_exact_relevant_population(self):
        queries = [query("one", tags=["visual", "ocr"]), query("two", [], tags=["ocr"])]
        report = self.score(queries, [result("one", ["a"]), result("two")])
        self.assertEqual(report["slices"]["by_tag"]["ocr"]["query_count"], 2)
        self.assertEqual(report["slices"]["by_tag"]["visual"]["query_count"], 1)
        self.assertEqual(report["slices"]["by_positive_count"]["0"]["zero_result_accuracy"], 1)
        self.assertEqual(report["slices"]["by_positive_count"]["1"]["precision_at_5"], .2)
        self.assertEqual(report["aggregate"]["model_calls"], 2)
        self.assertEqual(report["aggregate"]["input_tokens"], 200)

    def test_latency_percentiles_and_missing_populations(self):
        aggregate = self.score([query("one", []), query("two", [])],
                               [result("one", latency_ms=10), result("two", latency_ms=110)])["aggregate"]
        self.assertEqual(aggregate["latency_median_ms"], 60)
        self.assertEqual(aggregate["latency_p95_ms"], 105)
        self.assertIsNone(aggregate["recall_at_5"])
        self.assertEqual(aggregate["metric_denominators"]["recall_at_5"], 0)


class PairedClusterBootstrapTests(unittest.TestCase):
    def setUp(self):
        # Three correlated queries in the first family, one in the second.
        self.queries = [query(f"q{i}", family="large" if i < 3 else "small") for i in range(4)]
        self.baseline = [result(f"q{i}", ["a"] if i == 3 else []) for i in range(4)]
        self.comparison = [result(f"q{i}", [] if i == 3 else ["a"], variant="B") for i in range(4)]

    def bootstrap(self, baseline=None, comparison=None, **options):
        return paired_cluster_bootstrap(self.queries, baseline or self.baseline,
                                        comparison or self.comparison, CORPUS, **options)

    def test_cluster_bootstrap_preserves_families_and_query_weighted_point(self):
        report = self.bootstrap(metrics=["recall_at_5", "mean_loss"])
        recall = report["metrics"]["recall_at_5"]
        self.assertEqual(report["family_count"], 2)
        self.assertEqual(report["n_resamples"], 2000)
        # Equal family weighting would incorrectly produce 0 rather than .5.
        self.assertEqual(recall["difference"], .5)
        # Resampling whole correlated families admits both -1 and +1 extremes.
        self.assertEqual(recall["ci95"], [-1.0, 1.0])
        self.assertEqual(report["metrics"]["mean_loss"]["difference"], -.5)
        self.assertEqual(report, self.bootstrap(metrics=["recall_at_5", "mean_loss"]))

    def test_result_order_does_not_change_pairing_and_identical_runs_have_zero_delta(self):
        report = self.bootstrap(comparison=list(reversed(self.baseline)), metrics=["recall_at_5"])
        self.assertEqual(report["metrics"]["recall_at_5"]["difference"], 0)
        self.assertEqual(report["metrics"]["recall_at_5"]["ci95"], [0.0, 0.0])

    def test_reversing_pair_reverses_delta_and_interval(self):
        forward = self.bootstrap(metrics=["recall_at_5"])["metrics"]["recall_at_5"]
        reverse = self.bootstrap(self.comparison, self.baseline, metrics=["recall_at_5"])["metrics"]["recall_at_5"]
        self.assertEqual(reverse["difference"], -forward["difference"])
        self.assertEqual(reverse["ci95"], [-forward["ci95"][1], -forward["ci95"][0]])

    def test_missing_pairs_or_families_cannot_fall_back_to_query_bootstrap(self):
        with self.assertRaises(ValueError):
            self.bootstrap(comparison=self.comparison[:-1])
        self.queries[0].pop("family_id")
        with self.assertRaises(ValueError):
            self.bootstrap()

    def test_single_eligible_family_does_not_claim_inferential_interval(self):
        queries = [query("positive", family="positive-family"), query("zero", [], family="zero-family")]
        results = [result("positive", ["a"]), result("zero")]
        report = paired_cluster_bootstrap(queries, results, results, CORPUS,
                                          metrics=["recall_at_5", "zero_result_accuracy"])
        for metric in report["metrics"].values():
            self.assertEqual(metric["difference"], 0)
            self.assertEqual(metric["eligible_family_count"], 1)
            self.assertIsNone(metric["ci95"])
            self.assertGreater(metric["undefined_resamples"], 0)
            self.assertEqual(metric["status"], "insufficient_eligible_families")

    def test_unknown_metric_and_invalid_resample_count_rejected(self):
        for options in [{"metrics": ["latency_p95_ms"]}, {"n_resamples": 0}, {"seed": True}]:
            with self.subTest(options=options), self.assertRaises(ValueError):
                self.bootstrap(**options)


if __name__ == "__main__":
    unittest.main()
