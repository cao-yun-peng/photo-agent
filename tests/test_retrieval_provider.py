"""Offline money-ledger and transport tests; never send HTTP requests.

Every ledger and persisted response lives in TemporaryDirectory. The real post
method is replaced before installing the metering wrapper, including failure
tests, so no path can reach a live provider or spend money.
"""

import asyncio
import gc
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import httpx

from scripts.retrieval_eval import provider


PAYLOAD = {"model": "qwen-plus", "input": {"messages": [
    {"role": "user", "content": "Find a photo"}]}, "parameters": {"max_tokens": 100}}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.ledger = provider.Ledger(Path(self.temp.name) / "ledger.sqlite3")

    def reserve(self, amount=2000, model="qwen-plus"):
        return self.ledger.reserve(stage="text", model=model, payload_hash="fixture", amount=amount)

    def record(self, cid):
        with self.ledger.connect() as db:
            db.row_factory = sqlite3.Row
            record = dict(db.execute("SELECT * FROM calls WHERE id=?", (cid,)).fetchone())
        db.close()
        return record

    def test_connection_context_closes_handle_deterministically(self):
        with self.ledger.connect() as db:
            db.execute("SELECT 1")
        try:
            with self.assertRaises(sqlite3.ProgrammingError):
                db.execute("SELECT 1")
        finally:
            db.close()

    def test_units_known_usage_and_context_summary(self):
        with provider.call_context("C", "query-one"):
            cid = self.reserve()
        self.ledger.finish(cid, status="success", elapsed=10,
                           usage={"input_tokens": 100, "output_tokens": 20}, rates=[.8, 8])
        # Prices are CNY/million tokens; numerical multiplication yields micro-CNY.
        self.assertEqual(self.ledger.summary("C", "query-one")["cost_micro"], 240)
        self.assertEqual(self.ledger.summary("A")["cost_micro"], 0)
        self.assertEqual(self.record(cid)["reserved_micro"], 2000)
        self.assertEqual(provider.CURRENT.get(), ("preflight", "probe"))

    def test_cap_counts_inflight_unknown_and_failed_attempts(self):
        self.ledger = provider.Ledger(Path(self.temp.name) / "small.sqlite3", cap_micro=300)
        first, second = self.reserve(150), self.reserve(150)
        self.ledger.finish(first, status="ambiguous_failure", elapsed=2, error="CancelledError")
        self.ledger.finish(second, status="success", elapsed=2, usage=None, rates=[.8, 8])
        self.assertEqual(self.ledger.summary()["cost_micro"], 300)
        with self.assertRaises(provider.MoneyCapExceeded):
            self.reserve(1)
        self.assertEqual(self.ledger.summary()["model_calls"], 2)

    def test_simultaneous_reservations_are_serialized_before_cap_check(self):
        self.ledger = provider.Ledger(Path(self.temp.name) / "concurrent.sqlite3", cap_micro=300000)

        def attempt(_):
            try:
                return self.reserve(50000)
            except provider.MoneyCapExceeded:
                return None

        with ThreadPoolExecutor(max_workers=12) as executor:
            accepted = list(executor.map(attempt, range(24)))
        self.assertEqual(sum(cid is not None for cid in accepted), 6)
        self.assertEqual(self.ledger.summary()["cost_micro"], 300000)

    def test_ledger_survives_reopen_with_inflight_liability(self):
        self.reserve(1234)
        reopened = provider.Ledger(self.ledger.path)
        self.assertEqual(reopened.summary()["cost_micro"], 1234)
        self.assertEqual(reopened.summary()["calls_with_unknown_usage"], 1)

    def test_ambiguous_and_http_failures_never_refund_even_if_usage_present(self):
        for status in ("ambiguous_failure", "http_failure"):
            with self.subTest(status=status):
                cid = self.reserve()
                self.ledger.finish(cid, status=status, elapsed=1,
                                   usage={"input_tokens": 1, "output_tokens": 1}, rates=[.8, 8])
                self.assertEqual(self.record(cid)["charged_micro"], 2000)
                self.assertIsNone(self.record(cid)["input_tokens"])

    def test_partial_generation_usage_must_retain_reservation(self):
        for usage in ({"input_tokens": 10}, {"total_tokens": 20}, {"prompt_tokens": 10},
                      {"completion_tokens": 2}, {"input_tokens": True, "output_tokens": 0}):
            with self.subTest(usage=usage):
                cid = self.reserve()
                self.ledger.finish(cid, status="success", elapsed=1, usage=usage, rates=[.8, 8])
                self.assertEqual(self.record(cid)["charged_micro"], 2000)
                self.assertIsNone(self.record(cid)["input_tokens"])

    def test_embedding_total_tokens_can_be_fully_known_without_generation(self):
        cid = self.reserve(model="text-embedding-v3")
        self.ledger.finish(cid, status="success", elapsed=1, usage={"total_tokens": 11}, rates=[.5, 0])
        self.assertEqual(self.record(cid)["charged_micro"], 6)

    def test_invalid_reserve_amount_cannot_create_negative_liability(self):
        for amount in (-1, 0, True, 1.5):
            with self.subTest(amount=amount), self.assertRaises((ValueError, TypeError)):
                self.reserve(amount)
        self.assertEqual(self.ledger.summary()["model_calls"], 0)

    def test_underestimated_usage_is_persisted_and_prevents_more_requests(self):
        self.ledger = provider.Ledger(Path(self.temp.name) / "overrun.sqlite3", cap_micro=10)
        cid = self.reserve(10)
        with self.assertRaisesRegex(RuntimeError, "reservation_underestimated"):
            self.ledger.finish(cid, status="success", elapsed=1,
                               usage={"input_tokens": 100, "output_tokens": 0}, rates=[.8, 8])
        self.assertEqual(self.ledger.summary()["cost_micro"], 80)
        with self.assertRaises(provider.MoneyCapExceeded):
            self.reserve(1)

    def test_openai_style_usage_aliases_have_same_unit(self):
        cid = self.reserve()
        self.ledger.finish(cid, status="success", elapsed=1,
                           usage={"prompt_tokens": 100, "completion_tokens": 20}, rates=[.8, 8])
        self.assertEqual(self.record(cid)["charged_micro"], 240)


class ReservationTests(unittest.TestCase):
    def test_rejects_unknown_model_and_missing_output_bound(self):
        for payload in ({"model": "unpriced"}, {"model": "qwen-plus"}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                provider.reservation(payload)

    def test_nonpositive_or_noninteger_generation_bound_rejected(self):
        for limit in (-100, True, 1.5):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                provider.reservation({"model": "qwen-plus", "max_tokens": limit})

    def test_image_reservation_is_independent_of_base64_length(self):
        small = {"model": "qwen-vl-plus", "max_tokens": 100,
                 "input": {"messages": [{"role": "user", "content": [{"image": "data:image/jpeg;base64,short"}]}]}}
        large = json.loads(json.dumps(small))
        large["input"]["messages"][0]["content"][0]["image"] = "data:image/jpeg;base64," + "A" * 500000
        self.assertEqual(provider.reservation(small), provider.reservation(large))
        self.assertEqual(provider.reservation(small)[2], "visual")

    def test_recursive_reasoning_fields_scrubbed_without_removing_answer(self):
        raw = {"output": {"choices": [{"message": {"content": "answer", "reasoning_content": "private"}}]},
               "nested": [{"thoughts": "private", "reasoning": "private", "usage": 5}]}
        scrubbed = provider.scrub_reasoning(raw)
        self.assertNotIn("private", json.dumps(scrubbed))
        self.assertEqual(scrubbed["output"]["choices"][0]["message"]["content"], "answer")
        self.assertIn("reasoning_content", raw["output"]["choices"][0]["message"])


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(gc.collect)
        self.root = Path(self.temp.name)
        self.ledger = provider.Ledger(self.root / "ledger.sqlite3")
        evidence = patch.object(provider, "EVIDENCE", self.root)
        evidence.start()
        self.addCleanup(evidence.stop)

    async def invoke(self, fake, *, url="https://dashscope.aliyuncs.com/api/test", payload=None):
        # Passing a plain object suffices: fake post never accesses client state.
        with patch.object(httpx.AsyncClient, "post", fake):
            with provider.metered_transport(self.ledger):
                return await httpx.AsyncClient.post(object(), url, json=payload or PAYLOAD)

    def records(self):
        with self.ledger.connect() as db:
            db.row_factory = sqlite3.Row
            records = [dict(row) for row in db.execute("SELECT * FROM calls")]
        db.close()
        return records

    async def test_call_is_reserved_before_transport_and_only_sanitized_response_persisted(self):
        async def fake(_client, _url, **_kwargs):
            self.assertEqual(self.records()[0]["status"], "reserved")
            return httpx.Response(200, json={"usage": {"input_tokens": 100, "output_tokens": 20},
                                            "output": {"text": "answer", "reasoning_content": "private-thought"}})

        response = await self.invoke(fake)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ledger.summary()["cost_micro"], 240)
        artifact = next((self.root / "provider-responses").glob("*.json"))
        self.assertNotIn("private-thought", artifact.read_text())
        self.assertIn("answer", artifact.read_text())

    async def test_cancellation_preserves_money_and_restores_original_method(self):
        previous = httpx.AsyncClient.post

        async def fake(_client, _url, **_kwargs):
            raise asyncio.CancelledError()

        with self.assertRaises(asyncio.CancelledError):
            await self.invoke(fake)
        row = self.records()[0]
        self.assertEqual(row["status"], "ambiguous_failure")
        self.assertEqual(row["charged_micro"], row["reserved_micro"])
        self.assertEqual(row["error_code"], "CancelledError")
        self.assertIs(httpx.AsyncClient.post, previous)

    async def test_http_error_preserves_reservation(self):
        async def fake(_client, _url, **_kwargs):
            return httpx.Response(429, json={"usage": {"input_tokens": 1, "output_tokens": 0}})

        await self.invoke(fake)
        row = self.records()[0]
        self.assertEqual(row["status"], "http_failure")
        self.assertEqual(row["charged_micro"], row["reserved_micro"])

    async def test_provider_business_error_with_http_200_is_not_success(self):
        async def fake(_client, _url, **_kwargs):
            return httpx.Response(200, json={"code": "InvalidParameter", "message": "rejected",
                                            "usage": {"input_tokens": 1, "output_tokens": 0}})

        await self.invoke(fake)
        row = self.records()[0]
        self.assertNotEqual(row["status"], "success")
        self.assertEqual(row["charged_micro"], row["reserved_micro"])

    async def test_unknown_host_or_model_never_invokes_transport_or_reserves(self):
        async def fake(_client, _url, **_kwargs):
            self.fail("Unknown provider must be rejected before sending anything")

        with self.assertRaises(RuntimeError):
            await self.invoke(fake, url="https://unpriced.example.invalid/api/test")
        with self.assertRaises(ValueError):
            await self.invoke(fake, payload={"model": "unknown", "max_tokens": 10})
        self.assertEqual(self.ledger.summary()["model_calls"], 0)

    async def test_non_https_or_unexpected_port_never_invokes_transport(self):
        async def fake(_client, _url, **_kwargs):
            self.fail("Unexpected provider origin must be rejected before transport")

        for url in ("http://dashscope.aliyuncs.com/api/test", "https://dashscope.aliyuncs.com:8443/api/test"):
            with self.subTest(url=url), self.assertRaises(RuntimeError):
                await self.invoke(fake, url=url)


if __name__ == "__main__":
    unittest.main()
