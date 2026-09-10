"""Offline runner boundary tests, with no database, Redis, or model calls.

The real run() entry point is never invoked. Freeze checks use only temporary
fixtures. The nested per-query adapter is extracted from the actual source AST
and supplied in-memory services so its request and persistence paths can be
exercised without opening an external connection.
"""

import ast
import asyncio
import contextvars
import copy
import hashlib
import importlib
import json
import os
import tempfile
import time
import types
import unittest
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from scripts.retrieval_eval import environment


SOURCE = Path(__file__).resolve().parents[1] / "scripts/retrieval_eval/run.py"
with patch.object(environment, "configure", lambda: None), patch.dict(os.environ):
    runner = importlib.import_module("scripts.retrieval_eval.run")


class FreezeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.task = self.root / "task"
        self.evidence = self.task / "evidence"
        for name in ["app/service.py", *["scripts/retrieval_eval/" + filename for filename in
                     ("environment.py", "run.py", "provider.py", "metrics.py", "finalize_review.py", "seed_development.py", "probe.py")],
                     "tests/test_retrieval_evaluation.py", "tests/test_retrieval_provider.py",
                     "tests/test_retrieval_runner.py", "requirements.txt", "scripts/retrieval_eval/compose.yml",
                     "task/PROTOCOL.md", "task/evidence/development-index-snapshot.json",
                     "tests/eval/retrieval_v2/corpus.json", "tests/eval/retrieval_v2/queries.jsonl"]:
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("[]\n")
        fake_config = types.ModuleType("app.config")
        fake_config.settings = types.SimpleNamespace(model_dump=lambda: {
            "search_cache_revision": "fixture", "qwen_chat_model": "qwen-plus"})
        patches = [patch.object(runner, "ROOT", self.root), patch.object(runner, "TASK", self.task),
                   patch.object(runner, "EVIDENCE", self.evidence),
                   patch.object(runner, "FREEZE", self.evidence / "freeze-v1.json"),
                   patch.dict("sys.modules", {"app.config": fake_config}),
                   patch.object(runner.subprocess, "check_output", return_value="fixture-head\n"),
                   patch("builtins.print")]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def test_unchanged_freeze_verifies_and_changed_existing_source_rejected(self):
        runner.freeze()
        self.assertEqual(runner.verify_freeze()["version"], "retrieval-freeze-v1")
        (self.root / "app/service.py").write_text("changed")
        with self.assertRaises(RuntimeError):
            runner.verify_freeze()

    def test_new_production_source_cannot_silently_escape_frozen_inventory(self):
        runner.freeze()
        (self.root / "app/new_retrieval_module.py").write_text("new behavior")
        with self.assertRaises(RuntimeError):
            runner.verify_freeze()

    def test_new_evaluation_source_cannot_silently_escape_frozen_inventory(self):
        runner.freeze()
        (self.root / "scripts/retrieval_eval/new_helper.py").write_text("new behavior")
        with self.assertRaises(RuntimeError):
            runner.verify_freeze()

    def test_existing_freeze_is_not_overwritten(self):
        runner.freeze()
        before = runner.FREEZE.read_bytes()
        with self.assertRaises(RuntimeError):
            runner.freeze()
        self.assertEqual(runner.FREEZE.read_bytes(), before)

    def test_image_adapter_preserves_original_bytes_and_rejects_escape(self):
        image = self.root / "fixture.png"
        image.write_bytes(b"exact-image-byte-fixture")
        import base64
        self.assertEqual(base64.b64decode(runner.image_input("eval-local:fixture.png").split(",", 1)[1]),
                         image.read_bytes())
        for key in ("remote-key", "eval-local:../outside.png"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                runner.image_input(key)

    def test_atomic_save_failure_preserves_old_result_without_partial_file(self):
        target = self.root / "result.json"
        target.write_text('{"old": true}')
        with patch.object(runner.os, "replace", side_effect=OSError("fixture")):
            with self.assertRaises(OSError):
                runner.save(target, {"new": True})
        self.assertEqual(json.loads(target.read_text()), {"old": True})
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_index_canonicalization_handles_equivalent_dates_and_vector_encoding(self):
        a = {"id": "a", "embedding": "[0.1, 0.2]", "updated_at": "2026-09-08T08:00:00+08:00"}
        b = {"id": "a", "embedding": [.1, .2], "updated_at": "2026-09-08T00:00:00+00:00"}
        self.assertEqual(runner.canonical_index([a]), runner.canonical_index([b]))
        b["ai_description"] = "changed evidence"
        self.assertNotEqual(runner.canonical_index([a]), runner.canonical_index([b]))


class PerQueryAdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.out = Path(self.temp.name)
        self.requests = []
        self.response = {"items": [{"id": "uuid-a"}], "stop_reason": "candidates_exhausted"}
        self.failure = None
        self.exhausted = False
        self.freeze = self.out / "fixture-freeze.txt"
        self.freeze.write_text("fixture")
        self.query = {"id": "query-one", "query": "find a red flower", "relevant_photo_ids": ["a"],
                      "hard_negative_photo_ids": ["b"], "required_visible_text": ["SECRET_LABEL"],
                      "exclude_ids_from_request": ["DO_NOT_PASS_LABELS"], "notes": "SECRET_LABEL"}

    def adapter(self):
        owner = self

        class Plan:
            id = "test-plan"

            def model_copy(self, **kwargs):
                return self

            def model_dump(self, **kwargs):
                return {"raw_query": "find a red flower"}

        class Service:
            def __init__(self, *_args):
                pass

            async def create_plan(self, request):
                return Plan()

            async def search(self, request, **kwargs):
                if owner.failure:
                    raise owner.failure
                return copy.deepcopy(owner.response)

        class Store:
            def __init__(self, *_args):
                pass

            @asynccontextmanager
            async def mutation(self, *_args):
                yield {}

            async def load(self, *_args):
                return {"plan": {}, "rows": [], "meta": {}}

        @asynccontextmanager
        async def session():
            yield object()

        @contextmanager
        def call_context(*_args):
            yield

        def request(**kwargs):
            owner.requests.append(kwargs)
            return kwargs

        def save(path, value):
            path.write_text(json.dumps(value))

        tree = ast.parse(SOURCE.read_text(encoding="utf8"))
        run_node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "run")
        one_node = next(n for n in run_node.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "one")
        factory = ast.parse("def adapter_factory():\n    completed = 0\n    return None\n").body[0]
        factory.body[-1:] = [copy.deepcopy(one_node), ast.Return(ast.Name("one", ast.Load()))]
        module = ast.fix_missing_locations(ast.Module(body=[factory], type_ignores=[]))
        telemetry = {"model_calls": 0, "visual_calls": 0, "input_tokens": 0, "output_tokens": 0,
                     "calls_with_unknown_usage": 0, "cost_micro": 0}
        globals_ = {"out": self.out, "semaphore": asyncio.Semaphore(1), "TRACE": contextvars.ContextVar("test_trace"),
                    "datetime": datetime, "timezone": timezone, "time": time, "json": json, "save": save,
                    "call_context": call_context, "dataset": "development", "variant": "A",
                    "VARIANTS": runner.VARIANTS, "SearchRequest": request, "AsyncSessionLocal": session,
                    "SearchService": Service, "SearchStore": Store, "redis": object(), "user": "test-user",
                    "frozen": {"request": {"limit": 5, "auto_parse": False, "result_mode": "browse"},
                               "scoring_time": "2026-09-08T00:00:00+00:00"},
                    "short_ids": {"uuid-a": "a"},
                    "ledger": types.SimpleNamespace(summary=lambda *_args: telemetry, exhausted=self.exhausted),
                    "existing_result": runner.existing_result, "digest": runner.digest, "FREEZE": self.freeze,
                    "order": [self.query], "print": lambda *_args, **_kwargs: None}
        exec(compile(module, str(SOURCE), "exec"), globals_)
        return globals_["adapter_factory"]()

    async def test_actual_request_adapter_never_passes_judgments_or_label_exclusions(self):
        await self.adapter()(self.query)
        self.assertEqual(self.requests, [{"q": "find a red flower", "limit": 5, "auto_parse": False,
                                         "result_mode": "browse", "verify_constraints": False,
                                         "verify_semantic": False}])
        row = json.loads((self.out / "query-one.json").read_text())
        self.assertTrue(row["success"])
        self.assertEqual(row["returned_photo_ids"], ["a"])

    async def test_budget_failure_is_retained_as_failed_result(self):
        from scripts.retrieval_eval.provider import MoneyCapExceeded
        self.failure = MoneyCapExceeded("estimated_30_cny_cap")
        await self.adapter()(self.query)
        row = json.loads((self.out / "query-one.json").read_text())
        self.assertFalse(row["success"])
        self.assertEqual(row["returned_photo_ids"], [])
        self.assertTrue(row["error_code"])

    async def test_interrupted_marker_does_not_silently_repeat_a_paid_query(self):
        (self.out / "query-one.started.json").write_text("{}")
        with self.assertRaises(RuntimeError):
            await self.adapter()(self.query)
        self.assertFalse(self.requests)

    async def test_truncated_result_file_is_not_treated_as_completed(self):
        (self.out / "query-one.json").write_text('{"query_id":')
        with self.assertRaises((ValueError, RuntimeError)):
            await self.adapter()(self.query)
        self.assertFalse(self.requests)

    async def test_result_file_with_other_query_identity_is_not_treated_as_completed(self):
        (self.out / "query-one.json").write_text(json.dumps({"query_id": "different-query", "variant": "D"}))
        with self.assertRaises((ValueError, RuntimeError)):
            await self.adapter()(self.query)
        self.assertFalse(self.requests)

    async def test_unknown_returned_photo_is_recorded_as_failure_instead_of_losing_query(self):
        self.response["items"] = [{"id": "unknown-database-photo"}]
        await self.adapter()(self.query)
        row = json.loads((self.out / "query-one.json").read_text())
        self.assertFalse(row["success"])
        self.assertTrue(row["error_code"])

    async def test_exhausted_budget_records_unattempted_query_without_service_calls(self):
        self.exhausted = True
        await self.adapter()(self.query)
        row = json.loads((self.out / "query-one.json").read_text())
        self.assertFalse(self.requests)
        self.assertFalse(row["attempted"])
        self.assertFalse(row["success"])
        self.assertEqual(row["stop_reason"], "budget_exhausted")
        self.assertEqual(row["model_calls"], 0)

    async def test_valid_existing_result_is_reused_without_calls(self):
        await self.adapter()(self.query)
        self.requests.clear()
        await self.adapter()(self.query)
        self.assertFalse(self.requests)


class DatabaseFingerprintTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_database_must_match_snapshot_and_have_no_user_profile(self):
        expected = [{"id": "a", "embedding": [1.0, 0.0], "hash": "fixture", "ai_description": "original"}]
        with tempfile.TemporaryDirectory() as directory:
            evidence = Path(directory)
            (evidence / "development-index-snapshot.json").write_text(json.dumps(expected))
            for changed, profiles in ((False, 0), (True, 0), (False, 1)):
                actual = copy.deepcopy(expected)
                if changed:
                    actual[0]["ai_description"] = "different evidence"

                class Database:
                    async def scalar(self, statement, parameters):
                        self_owner.assertEqual(parameters, {"u": "fixture-user"})
                        return profiles if "user_profiles" in str(statement) else actual

                @asynccontextmanager
                async def session():
                    yield Database()

                self_owner = self
                with self.subTest(changed=changed, profiles=profiles), patch.object(runner, "EVIDENCE", evidence):
                    if changed or profiles:
                        with self.assertRaisesRegex(RuntimeError, "isolated_database_drift"):
                            await runner.verify_database("development", "fixture-user", session)
                    else:
                        await runner.verify_database("development", "fixture-user", session)


if __name__ == "__main__":
    unittest.main()
