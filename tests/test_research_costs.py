import io
import json
import unittest
from unittest.mock import patch

from bridge_evolve.evolution.costs import receipt_usage, request_costs, resource_summary, tool_costs
from bridge_evolve.evolution.models import ModelClient
from test_research import response


class ResearchCostTests(unittest.TestCase):
    def test_receipts_are_exact_and_missing_receipts_are_not_free_calls(self):
        records = [{"status": "received", "usage": {"input_tokens": 100, "output_tokens": 10},
                    "cached_tokens": 90, "cache_write_tokens": 5, "nano_ai_credits": 1250000000}]
        costs = request_costs(records)
        self.assertEqual(costs["ai_credits"], 1.25)
        self.assertEqual(costs["cache_read_tokens"], 90)
        records.append({"status": "error", "reserved_tokens": 500})
        costs = request_costs(records)
        self.assertIsNone(costs["ai_credits"])
        self.assertEqual(costs["known_ai_credits"], 1.25)
        self.assertEqual(costs["unknown_usage_requests"], 1)
        self.assertEqual(costs["retained_token_reservations"], 500)
        self.assertEqual(costs["failed"], 1)

    def test_cached_read_and_write_partition_is_saved_with_each_actual_model_response(self):
        config = {"model": "gpt-6-astra", "reasoning_effort": "high", "max_output_tokens": 1024,
                  "max_agent_tokens": 100000, "max_agent_requests": 2, "agent_timeout": 30,
                  "max_context_bytes": 10000, "project": "."}
        state = {"agent_requests": 0}
        client = ModelClient(config, state, lambda: None, lambda: None)
        client.key = "fixture"
        result = response(text="ok", usage={"input_tokens": 100, "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 80, "cache_write_tokens": 5}})
        result["copilot_usage"] = {"total_nano_aiu": 2000000000}
        with patch("bridge_evolve.evolution.models.build_opener") as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps(result).encode())
            client.request([], [], label="g001-Z:turn:1")
        costs = request_costs(state["model_requests"])
        self.assertEqual((costs["ai_credits"], costs["cache_read_tokens"], costs["cache_write_tokens"]), (2, 80, 5))
        self.assertEqual(state["model_usage"]["charged_tokens"], 110)

    def test_shared_costs_and_duplicate_publications_do_not_inflate_candidate_counts(self):
        state = {"model_requests": [{"status": "received", "label": "g001-Z:turn:1",
                  "usage": {"input_tokens": 90, "output_tokens": 10}, "nano_ai_credits": 3000000000}],
                 "evaluated_pairs": 8, "spent_seconds": 2, "research_sessions": {},
                 "evaluation_queue": {"q1": {"candidate": "C", "status": "development_complete", "outcome": {},
                                             "occurrences": [{}, {}]}}}
        summary = resource_summary(state)
        self.assertEqual(summary["unique_candidates"], 1)
        self.assertEqual(summary["publication_occurrences"], 2)
        self.assertEqual(summary["ai_credits_per_evaluated_candidate"], 3)
        self.assertEqual(summary["tokens_per_evaluated_candidate"], 100)
        state["evaluation_queue"] = {}
        summary = resource_summary(state)
        self.assertIsNone(summary["tokens_per_evaluated_candidate"])
        self.assertIsNone(summary["pairs_per_scored_candidate"])

    def test_consult_time_is_not_counted_again_as_non_model_tool_time(self):
        costs = tool_costs({"one": {"name": "consult", "status": "completed",
            "started_at": "2026-11-06T00:00:00+00:00", "finished_at": "2026-11-06T00:00:10+00:00"},
            "two": {"name": "run_tests", "status": "completed",
            "started_at": "2026-11-06T00:00:10+00:00", "finished_at": "2026-11-06T00:00:12+00:00"}})
        self.assertEqual(costs["non_model_tool_seconds"], 2)
        self.assertEqual(costs["consult_seconds_includes_model"], 10)

    def test_optional_unknown_usage_does_not_crash_or_invent_account_quota(self):
        self.assertEqual(receipt_usage({"usage": None, "copilot_usage": None}), {})
        self.assertEqual(receipt_usage({"usage": {"input_tokens_details": None}}), {})
        self.assertIsNone(request_costs([{"status": "received", "usage": {"input_tokens": 1, "output_tokens": 2}}])["cache_read_tokens"])
        self.assertNotIn("balance", request_costs([]))
        invalid = receipt_usage({"copilot_usage": {"total_nano_aiu": False}})
        self.assertIn("receipt_error", invalid)
        self.assertNotIn("nano_ai_credits", invalid)


if __name__ == "__main__":
    unittest.main()
