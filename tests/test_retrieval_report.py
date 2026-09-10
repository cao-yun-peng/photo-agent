"""Full-size synthetic fixtures for offline report gates; not experiment scores."""

import json
import random
import tempfile
import unittest
from pathlib import Path

from scripts.retrieval_eval import report


class RetrievalReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.queries = {}
        self._dataset("development")
        self._dataset("validation")
        self.frozen = {"sources": self._dataset_hashes("development"), "query_order_seed": 20260908,
                       "variants": {v: {"verify_constraints": v != "A", "verify_semantic": v in "CD", "visual": v == "D"} for v in "ABCD"},
                       "settings": {"search_cache_revision": "test-only"}, "request": {"limit": 5}}
        self.save(self.evidence / "freeze-v1.json", self.frozen)
        self.freeze_hash = report.digest(self.evidence / "freeze-v1.json")
        for variant in "ABCD":
            self._run("development", variant)

    @staticmethod
    def save(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf8")

    def _dataset(self, name):
        nq, np = report.EXPECTED[name]
        directory = self.root / report.DATASETS[name]
        directory.mkdir(parents=True, exist_ok=True)
        self.save(directory / "corpus.json", [{"photo_id": f"p{i}"} for i in range(np)])
        queries = [{"id": f"q{i:03}", "query": "fixture query", "family_id": f"family-{i // 3}",
                    "relevant_photo_ids": [] if i % 10 == 0 else ["p0"], "hard_negative_photo_ids": ["p1"],
                    "expected_empty": i % 10 == 0, "tags": ["zero" if i % 10 == 0 else "visual"]} for i in range(nq)]
        (directory / "queries.jsonl").write_text("\n".join(json.dumps(q) for q in queries), encoding="utf8")
        self.queries[name] = queries

    def _dataset_hashes(self, name):
        return {report.DATASETS[name] + "/" + filename: report.digest(self.root / report.DATASETS[name] / filename)
                for filename in ("corpus.json", "queries.jsonl")}

    def _run(self, dataset, variant):
        directory = self.evidence / "runs" / dataset / variant
        order = [q["id"] for q in self.queries[dataset]]
        random.Random(self.frozen["query_order_seed"]).shuffle(order)
        self.save(directory / "schedule.json", {"dataset": dataset, "variant": variant,
                                                "freeze_sha256": self.freeze_hash, "query_ids": order})
        for query in self.queries[dataset]:
            self.save(directory / f"{query['id']}.json", {
                "query_id": query["id"], "variant": variant, "dataset": dataset, "freeze_sha256": self.freeze_hash,
                "returned_photo_ids": query["relevant_photo_ids"], "attempted": True, "success": True,
                "stop_reason": "candidates_exhausted", "latency_ms": (4 - "ABCD".index(variant)) * 10,
                "model_calls": 1, "visual_calls": int(variant == "D"), "input_tokens": 100, "output_tokens": 20,
                "estimated_cost_cny": .001, "completed_at": "fixture-completion", "error_code": None,
                "result_meta": {"search_usage": {"cache_hits": 0, "cache_misses": 1}},
            })

    def result_path(self, variant="D", query_id="q001", dataset="development"):
        return self.evidence / "runs" / dataset / variant / f"{query_id}.json"

    def change_result(self, **fields):
        path = self.result_path()
        row = report.read(path)
        row.update(fields)
        self.save(path, row)

    def dev(self):
        return report.development(self.root, self.evidence)

    def validation_freeze(self, selected):
        self.save(self.evidence / "validation-freeze.json", {
            "sources": self._dataset_hashes("validation"), "selected_variant": selected,
            "development_freeze_sha256": self.freeze_hash,
            "selection_sha256": report.digest(self.evidence / "report/selection.json"),
        })

    def test_full_217_four_variants_select_and_emit_reproducible_reports(self):
        summary = self.dev()
        self.assertEqual(summary["status"], "complete")
        selection = report.read(self.evidence / "report/selection.json")
        self.assertEqual(selection["selected_variant"], "D")
        self.assertEqual(selection["pareto_variants"], ["D"])
        self.assertEqual(selection["freeze_sha256"], self.freeze_hash)
        self.assertEqual(selection["selected_variant_config"], self.frozen["variants"]["D"])
        before = (self.evidence / "report/selection.json").read_bytes()
        self.dev()
        self.assertEqual((self.evidence / "report/selection.json").read_bytes(), before)
        paired = report.read(self.evidence / "report/paired-bootstrap.json")
        self.assertEqual(set(paired["comparisons"]), {"B_minus_A", "C_minus_A", "D_minus_A"})
        self.assertEqual(paired["comparisons"]["D_minus_A"]["n_resamples"], 2000)
        self.assertTrue((self.evidence / "report/development-report.md").exists())

    def test_missing_result_makes_selection_incomplete(self):
        self.result_path().unlink()
        summary = self.dev()
        self.assertEqual(summary["status"], "incomplete")
        self.assertIsNone(report.read(self.evidence / "report/selection.json")["selected_variant"])
        self.assertIn("result_missing_or_invalid", {i["kind"] for i in summary["issues"]})

    def test_unattempted_budget_row_cannot_count_as_complete_comparison(self):
        self.change_result(attempted=False, success=False, returned_photo_ids=[], stop_reason="budget_exhausted")
        summary = self.dev()
        self.assertEqual(summary["status"], "incomplete")
        self.assertIn("query_not_attempted", {i["kind"] for i in summary["issues"]})

    def test_harness_failure_blocks_selection_even_with_all_result_files(self):
        self.save(self.result_path().parent / "harness-failures.json", {"error_types": ["RuntimeError"]})
        self.assertEqual(self.dev()["status"], "incomplete")
        self.assertIsNone(report.read(self.evidence / "report/selection.json")["selected_variant"])

    def test_wrong_freeze_identity_and_label_mutation_rejected(self):
        self.change_result(freeze_sha256="wrong")
        self.assertEqual(self.dev()["status"], "incomplete")
        self.change_result(freeze_sha256=self.freeze_hash)
        labels = self.root / report.DATASETS["development"] / "queries.jsonl"
        labels.write_text(labels.read_text() + "\n")
        summary = self.dev()
        self.assertIn("frozen_input_changed", {i["kind"] for i in summary["issues"]})

    def test_actual_operational_failure_is_an_observation_not_missing_execution(self):
        path = self.result_path(query_id="q000")
        row = report.read(path)
        row.update(success=False, error_code="TimeoutError", stop_reason="deadline_exceeded")
        self.save(path, row)
        summary = self.dev()
        self.assertEqual(summary["status"], "complete")
        aggregate = summary["variants"]["D"]["score"]["aggregate"]
        self.assertEqual(aggregate["operational_error_count"], 1)
        self.assertLess(aggregate["zero_result_accuracy"], 1)
        failures = report.read(self.evidence / "report/failures.json")["cases"]
        self.assertTrue(any(case["query_id"] == "q000" and "operational_error" in case["categories"] for case in failures))

    def test_selection_loss_tolerance_then_latency_then_cost(self):
        summary = {"status": "complete", "freeze_sha256": "fixture", "variants": {}}
        for variant, loss, latency, cost in [("A", .2, 100, 1), ("B", .219, 50, 2),
                                             ("C", .221, 1, .1), ("D", .219, 50, 1)]:
            summary["variants"][variant] = {"score": {"aggregate": {
                "mean_loss": loss, "latency_median_ms": latency, "estimated_cost_cny": cost}}}
        selection = report.select_configuration(summary, self.frozen)
        self.assertEqual(selection["eligible_variants"], ["A", "B", "D"])
        self.assertEqual(selection["selected_variant"], "D")
        self.assertEqual(selection["pareto_variants"], ["A", "C", "D"])

    def test_partial_dataset_cannot_be_redefined_as_complete(self):
        path = self.root / report.DATASETS["development"] / "queries.jsonl"
        path.write_text("\n".join(json.dumps(q) for q in self.queries["development"][:-1]))
        self.frozen["sources"] = self._dataset_hashes("development")
        self.save(self.evidence / "freeze-v1.json", self.frozen)
        summary = self.dev()
        self.assertIn("dataset_size_mismatch", {i["kind"] for i in summary["issues"]})

    def test_validation_uses_frozen_selection_and_cannot_tune_on_bad_outcomes(self):
        self.dev()
        selection_before = (self.evidence / "report/selection.json").read_bytes()
        self.validation_freeze("D")
        for variant in "AD":
            self._run("validation", variant)
        # Deliberately unfavorable validation for the selected configuration.
        for query in self.queries["validation"]:
            path = self.result_path("D", query["id"], "validation")
            row = report.read(path)
            row["returned_photo_ids"] = ["p1"]
            self.save(path, row)
        summary = report.validation(self.root, self.evidence)
        self.assertEqual(summary["status"], "complete")
        self.assertEqual(set(summary["variants"]), {"A", "D"})
        self.assertEqual(summary["selected_variant"], "D")
        self.assertEqual((self.evidence / "report/selection.json").read_bytes(), selection_before)
        self.assertGreater(summary["variants"]["D"]["score"]["aggregate"]["mean_loss"], summary["variants"]["A"]["score"]["aggregate"]["mean_loss"])
        # Even retaining the same selected variant cannot bypass the frozen selection hash.
        selection_path = self.evidence / "report/selection.json"
        selection_path.write_bytes(selection_before + b"\n")
        changed = report.validation(self.root, self.evidence)
        self.assertEqual(changed["status"], "incomplete")
        self.assertIn("validation_selection_file_changed", {i["kind"] for i in changed["issues"]})

    def test_validation_requires_80_rows_and_selected_variant_matches_freeze(self):
        self.dev()
        self.validation_freeze("C")
        summary = report.validation(self.root, self.evidence)
        self.assertEqual(summary["status"], "incomplete")
        self.assertIn("validation_selection_mismatch", {i["kind"] for i in summary["issues"]})

    def test_unknown_returned_database_ids_are_harness_integrity_failure(self):
        self.change_result(invalid_returned_database_ids=["unexpected-photo"])
        self.assertEqual(self.dev()["status"], "incomplete")

    def test_missing_core_configuration_is_incomplete_not_an_unrecorded_exception(self):
        self.frozen.pop("query_order_seed")
        self.save(self.evidence / "freeze-v1.json", self.frozen)
        summary = self.dev()
        self.assertEqual(summary["status"], "incomplete")
        self.assertIn("frozen_configuration_incomplete", {i["kind"] for i in summary["issues"]})


if __name__ == "__main__":
    unittest.main()
