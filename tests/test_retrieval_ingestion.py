"""Offline ingestion recovery tests: no database or real provider connections."""
import asyncio
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import numpy as np
from PIL import Image

from scripts.retrieval_eval import seed_validation as ingestion
from scripts.retrieval_eval.provider import Ledger
from app.schemas.analysis import ImageAnalysis
from app.services.ai import build_retrieval_text


class IngestionFixture(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.evidence = self.root / "evidence"
        self.evidence.mkdir()
        self.approval = self.evidence / "outbound-approval.json"
        self.approval.write_text('{"status":"authorized"}', encoding="utf-8")
        self.ledger = Ledger(self.evidence / "ledger.sqlite3")
        self.journal = ingestion.StageJournal(self.ledger, self.evidence)

    async def test_missing_and_pending_approval_never_invoke_operation(self):
        operation = AsyncMock(return_value="must not execute")
        for state in [None, {"status": "pending_explicit_data_egress_confirmation"}, {"status": True}]:
            if state is None:
                self.approval.unlink(missing_ok=True)
            else:
                self.approval.write_text(json.dumps(state))
            with self.assertRaisesRegex(RuntimeError, "approval_pending"):
                await self.journal.execute("v-001", "describe", {}, operation)
        operation.assert_not_called()
        self.assertEqual(self.ledger.summary()["model_calls"], 0)
        self.assertFalse(self.journal.directory.exists())

    async def test_success_is_reused_and_changed_input_is_rejected(self):
        operation = AsyncMock(return_value="一张有效的真实模型生成描述")
        self.assertEqual(await self.journal.execute("v-001", "describe", {"image": "a"}, operation), operation.return_value)
        self.assertEqual(await self.journal.execute("v-001", "describe", {"image": "a"}, operation), operation.return_value)
        self.assertEqual(operation.await_count, 1)
        with self.assertRaisesRegex(RuntimeError, "completed_stage_input_changed"):
            await self.journal.execute("v-001", "describe", {"image": "b"}, operation)

    async def test_failed_stage_retry_is_explicit_and_attempts_survive(self):
        operation = AsyncMock(side_effect=[ValueError("bad response"), "valid"])
        with self.assertRaises(ValueError):
            await self.journal.execute("v-001", "describe", {}, operation)
        with self.assertRaisesRegex(ingestion.StageUnavailable, "retry_failed"):
            await self.journal.execute("v-001", "describe", {}, operation)
        retry = ingestion.StageJournal(self.ledger, self.evidence, retry_failed=True)
        self.assertEqual(await retry.execute("v-001", "describe", {}, operation), "valid")
        attempts = sorted((retry.directory / "v-001").glob("describe-attempt-*.json"))
        self.assertEqual([json.loads(p.read_text())["status"] for p in attempts], ["failed", "succeeded"])
        self.assertEqual(operation.await_count, 2)

    async def test_timeout_needs_recorded_acknowledgment_even_with_retry_flag(self):
        operation = AsyncMock(side_effect=[httpx.ReadTimeout("unknown response"), "recovered"])
        with self.assertRaises(httpx.ReadTimeout):
            await self.journal.execute("v-001", "describe", {}, operation)
        retry = ingestion.StageJournal(self.ledger, self.evidence, retry_failed=True)
        with self.assertRaisesRegex(ingestion.StageUnavailable, "acknowledgment"):
            await retry.execute("v-001", "describe", {}, operation)
        acknowledged = ingestion.StageJournal(self.ledger, self.evidence, acknowledge_ambiguous="Reviewed unknown attempt; retain its reserved cost")
        self.assertEqual(await acknowledged.execute("v-001", "describe", {}, operation), "recovered")
        last = json.loads((acknowledged.directory / "v-001/describe-attempt-0002.json").read_text())
        self.assertTrue(last["ambiguity_acknowledgment"])
        self.assertEqual(last["previous_attempt"], "describe-attempt-0001.json")

    async def test_interrupted_started_attempt_is_not_silently_repeated(self):
        ingestion.save(self.journal.directory / "v-001/describe-attempt-0001.json", {"input_sha256": ingestion.fingerprint({}), "status": "started"})
        operation = AsyncMock(return_value="unused")
        with self.assertRaisesRegex(ingestion.StageUnavailable, "acknowledgment"):
            await self.journal.execute("v-001", "describe", {}, operation)
        operation.assert_not_called()

    async def test_actual_http_boundary_is_metered_and_named_as_ingestion(self):
        async def fake_post(client, url, *args, **kwargs):
            return httpx.Response(200, json={"output": {"embeddings": []}, "usage": {"total_tokens": 100}})

        async def operation():
            async with httpx.AsyncClient() as client:
                result = await client.post("https://dashscope.aliyuncs.com/api/test", json={"model": "text-embedding-v3", "input": {"texts": ["model-derived visual evidence"]}})
            return result.json()["output"]

        with patch("httpx.AsyncClient.post", fake_post), patch("scripts.retrieval_eval.provider.EVIDENCE", self.evidence):
            await self.journal.execute("v-001", "embedding", {}, operation)
        with self.ledger.connect() as db:
            row = db.execute("SELECT variant,query_id,status,charged_micro FROM calls").fetchone()
        self.assertEqual(row[:3], ("validation-ingestion", "v-001:embedding", "success"))
        self.assertEqual(row[3], 50)

    def image_record(self):
        y, x = np.indices((96, 96))
        pixels = np.stack((x * 2, y * 2, (x + y) % 255), axis=-1).astype(np.uint8)
        output = io.BytesIO()
        Image.fromarray(pixels).save(output, format="JPEG")
        path = self.root / "input.jpg"
        path.write_bytes(output.getvalue())
        return {"photo_id": "v-001", "database_photo_id": "fixture", "path": "input.jpg", "sha256": hashlib.sha256(output.getvalue()).hexdigest(),
                "source_title": "FORBIDDEN_SOURCE_TITLE", "observation": "FORBIDDEN_HUMAN_CAPTION", "relevant_photo_ids": ["FORBIDDEN_LABEL"]}

    def service(self, embedding=None, quality="ok"):
        return SimpleNamespace(settings=SimpleNamespace(qwen_vl_model="qwen-vl-plus", qwen_embedding_model="text-embedding-v3"),
                               _VL_PROMPT="production description instruction", _VL_ANALYSIS_PROMPT="production structure instruction", VL_ANALYSIS_PROMPT_VERSION="v5",
                               describe_image=AsyncMock(return_value="一杯白色陶瓷杯中的咖啡放在木桌上。"),
                               analyze_image=AsyncMock(return_value=ImageAnalysis(scene="餐厅", summary="白色咖啡杯放在木制桌面上。", objects=["咖啡", "杯子"], analysis_version="v5", parse_quality=quality)),
                               embed_text=AsyncMock(return_value=embedding if embedding is not None else [1.0] + [0.0] * 1023),
                               build_retrieval_text=build_retrieval_text)

    async def process(self, service, journal=None, record=None):
        return await ingestion.analyze_photo(record or self.image_record(), journal or self.journal, ai_service=service, root=self.root,
                                             image_loader=lambda key: "data:image/jpeg;base64,ORIGINAL_IMAGE_ONLY")

    async def test_only_image_reaches_vl_and_model_outputs_reach_embedding(self):
        service = self.service()
        fields = await self.process(service)
        self.assertEqual(fields["status"], "done")
        service.describe_image.assert_awaited_once_with("data:image/jpeg;base64,ORIGINAL_IMAGE_ONLY")
        service.analyze_image.assert_awaited_once_with("data:image/jpeg;base64,ORIGINAL_IMAGE_ONLY")
        embedded = service.embed_text.await_args.args[0]
        self.assertIn(service.describe_image.return_value, embedded)
        self.assertNotIn("FORBIDDEN", embedded)
        self.assertEqual(fields["people_count"], 0)
        self.assertTrue(fields["thumb_key"].startswith("eval-local:"))

    async def test_embedding_recovery_reuses_both_paid_vl_stages(self):
        service = self.service()
        service.embed_text.side_effect = [ValueError("embedding unavailable"), [1.0] + [0.0] * 1023]
        record = self.image_record()
        first = await self.process(service, record=record)
        self.assertEqual(first["status"], "partial_done")
        self.assertIsNone(first["embedding"])
        self.assertTrue(first["ai_analysis"])
        retry = ingestion.StageJournal(self.ledger, self.evidence, retry_failed=True)
        second = await self.process(service, retry, record)
        self.assertEqual(second["status"], "done")
        self.assertEqual(service.describe_image.await_count, 1)
        self.assertEqual(service.analyze_image.await_count, 1)
        self.assertEqual(service.embed_text.await_count, 2)

    async def test_malformed_or_nonfinite_embedding_cannot_enter_index(self):
        for embedding in [[1.0, 2.0], [float("nan")] + [0.0] * 1023]:
            with self.subTest(embedding_length=len(embedding)):
                evidence = self.root / ("short" if len(embedding) == 2 else "nan")
                evidence.mkdir()
                (evidence / "outbound-approval.json").write_text('{"status":"authorized"}')
                journal = ingestion.StageJournal(Ledger(evidence / "ledger.sqlite3"), evidence)
                fields = await self.process(self.service(embedding), journal)
                self.assertEqual(fields["status"], "skipped")
                self.assertIsNone(fields["embedding"])
                self.assertEqual(fields["ai_analysis"], {})
                self.assertIsNone(fields["photo_type"])

    async def test_completed_analysis_fallback_matches_worker_reuse(self):
        service = self.service(quality="vl_request_error")
        record = self.image_record()
        first = await self.process(service, record=record)
        second = await self.process(service, record=record)
        self.assertEqual(first["status"], "partial_done")
        self.assertEqual(second["status"], "partial_done")
        self.assertEqual(service.analyze_image.await_count, 1)
        saved = json.loads((self.journal.directory / "v-001/analyze-result.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "completed_partial")

    async def test_changed_image_fails_before_models(self):
        service = self.service()
        record = self.image_record()
        record["sha256"] = "0" * 64
        with self.assertRaisesRegex(RuntimeError, "image_changed"):
            await self.process(service, record=record)
        service.describe_image.assert_not_called()

    async def test_check_mode_does_not_require_outbound_approval_or_open_database(self):
        with patch.object(ingestion, "load_corpus", return_value=[{}] * 40), patch.object(ingestion, "require_approval", side_effect=AssertionError("must not call")), patch("builtins.print"):
            await ingestion.main(SimpleNamespace(check=True))

    async def test_ingestion_verifies_global_freeze_before_database_or_provider(self):
        with patch.object(ingestion, "load_corpus", return_value=[{}] * 40), patch.object(ingestion, "require_approval"), \
                patch.object(ingestion, "EVIDENCE", self.evidence), \
                patch.object(ingestion, "bind_frozen_configuration", side_effect=RuntimeError("frozen_files_changed")) as bind, \
                patch.object(ingestion, "Ledger", side_effect=AssertionError("must not reach ledger")):
            with self.assertRaisesRegex(RuntimeError, "frozen_files_changed"):
                await ingestion.main(SimpleNamespace(check=False))
        bind.assert_called_once()

    async def test_frozen_models_replace_changed_environment_settings(self):
        settings = SimpleNamespace(qwen_vl_model="changed-alias", qwen_embedding_model="changed-embedding")
        frozen = {"settings": {"qwen_vl_model": "frozen-vl", "qwen_embedding_model": "frozen-embedding", "search_cache_revision": "frozen-revision"}}
        with patch("scripts.retrieval_eval.run.verify_freeze", return_value=frozen) as verify, patch("app.config.settings", settings):
            self.assertEqual(ingestion.bind_frozen_configuration(), frozen)
        verify.assert_called_once()
        self.assertEqual(settings.qwen_vl_model, "frozen-vl")
        self.assertEqual(settings.qwen_embedding_model, "frozen-embedding")
        self.assertEqual(settings.search_cache_revision, "frozen-revision")


if __name__ == "__main__":
    unittest.main()
