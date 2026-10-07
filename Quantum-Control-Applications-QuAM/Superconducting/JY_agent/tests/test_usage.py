from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from jy_agent.approval_web import _usage_section
from jy_agent.usage import attribute_usage, call_cost, load_calls, parse_time

PRICING = {
    "cache_write_5m_multiplier": 1.25,
    "cache_write_1h_multiplier": 2.0,
    "fast_multiplier": 2.0,
    "models": {"claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2}},
}


def assistant(mid: str, ts: str, tools=(), output=100, read=1000, write_1h=0):
    usage = {
        "input_tokens": 10,
        "output_tokens": output,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": write_1h,
        "cache_creation": {"ephemeral_1h_input_tokens": write_1h, "ephemeral_5m_input_tokens": 0},
        "speed": "standard",
    }
    content = [{"type": "tool_use", "name": name, "id": f"t{mid}{i}", "input": {}} for i, name in enumerate(tools)]
    return {"type": "assistant", "timestamp": ts, "message": {"id": mid, "model": "claude-opus-5-5", "usage": usage, "content": content}}


def user(text: str, ts: str):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": text}}


def tool_result(ts: str):
    return {"type": "user", "timestamp": ts, "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]}}


class UsageTests(unittest.TestCase):
    def transcript(self, folder: str) -> Path:
        lines = [
            user("enter", "2026-10-02T11:00:00Z"),
            assistant("m1", "2026-10-02T11:00:05Z", ["mcp__jy_bringup__jy_enter_autonomy_mode"]),
            user("approved", "2026-10-02T11:05:00Z"),
            # Run A starts 11:10; its waiting call and analysis belong to it.
            assistant("m2", "2026-10-02T11:09:59Z"),
            assistant("m3", "2026-10-02T11:10:01Z", ["mcp__jy_bringup__jy_autonomy_start_run"]),
            # The same API call written as two lines (one per content block).
            assistant("m4", "2026-10-02T11:12:00Z", ["Bash"]),
            assistant("m4", "2026-10-02T11:12:00Z", ["mcp__jy_bringup__jy_autonomy_analyze_run"]),
            tool_result("2026-10-02T11:12:01Z"),
            # Code fix inside the same turn: reading, then editing.
            assistant("m5", "2026-10-02T11:13:00Z", ["Read"]),
            assistant("m6", "2026-10-02T11:14:00Z", ["Edit"], write_1h=1000),
            # Run B starts 11:20.
            assistant("m7", "2026-10-02T11:20:01Z", ["mcp__jy_bringup__jy_autonomy_start_run"]),
            user("a question", "2026-10-02T11:30:00Z"),
            assistant("m8", "2026-10-02T11:30:05Z"),
            # Before the workflow existed: excluded.
            assistant("m0", "2026-10-01T09:00:00Z"),
        ]
        root = Path(folder)
        (root / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
        return root

    def runs(self):
        return [
            {"id": "A", "node_id": "02x", "qubits": ["q1"], "started_at": "2026-10-02T11:10:00+00:00"},
            {"id": "B", "node_id": "02c", "qubits": ["q1"], "started_at": "2026-10-02T11:20:00+00:00"},
        ]

    def test_duplicate_lines_count_once(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            calls = load_calls(self.transcript(folder))
            self.assertEqual(sorted(c.message_id for c in calls), ["m0", "m1", "m2", "m3", "m4", "m5", "m6", "m7", "m8"])

    def test_calls_are_attributed_per_run(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            calls = load_calls(self.transcript(folder))
            result = attribute_usage(calls, self.runs(), PRICING, parse_time("2026-10-02T10:59:00Z"))
            by_run = {row["run_id"]: row["calls"] for row in result["runs"]}
            # m3 and m4 (analysis) belong to A; m2 waits for m3 but precedes A's start.
            self.assertEqual(by_run, {"A": 2, "B": 1})
            self.assertEqual(result["setup"]["calls"], 2)  # m1 entry, m2
            self.assertEqual(result["development"]["calls"], 2)  # m5 read for m6 edit
            self.assertEqual(result["conversation"]["calls"], 1)  # m8
            self.assertEqual(result["total"]["calls"], 8)  # m0 excluded
            self.assertEqual(result["nodes"]["02x"]["runs"], 1)

    def test_cost_uses_cache_multipliers(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            call = next(c for c in load_calls(self.transcript(folder)) if c.message_id == "m6")
            expected = (10 * 4 + 1000 * 4 * 2.0 + 1000 * 0.2 + 100 * 20) / 1e6
            self.assertAlmostEqual(call_cost(call, PRICING), expected)

    def test_page_renders(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = self.transcript(folder)

            class Service:
                def workflow_usage(self, workflow_id):
                    calls = load_calls(root)
                    result = attribute_usage(
                        calls, UsageTests().runs(), PRICING, parse_time("2026-10-02T10:59:00Z")
                    )
                    return {**result, "pricing": PRICING, "transcript_dir": str(root)}

            html = _usage_section(Service(), "wf", "zh-Hant")
            self.assertIn("Opus 5.5", html)
            self.assertIn("02c", html)
            self.assertIn("估計總費用", html)


if __name__ == "__main__":
    unittest.main()
