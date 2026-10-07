"""Model and token usage per JY run, read from Claude Code session transcripts.

Operator request 2026-10-03: the Dashboard shows which model each experiment
used, how many tokens, and an estimated cost. MCP does not pass token usage to
the server, so this reads the transcripts Claude Code keeps under
``~/.claude/projects/<project>/``. Only numbers are extracted; no conversation
text leaves this module.

One API call can appear on several transcript lines (one per content block)
that repeat the same usage, so calls are de-duplicated by message id.
Attribution, per call: a call that uses a JY MCP tool is measurement work and
belongs to the JY run whose window [started_at, next run's started_at)
contains it (before the first run: session setup). A call that edits files
(Edit, Write, NotebookEdit) is development. Any other call (reading, waiting,
answering) takes the label of the next labelled call in the same user turn, or
the previous one; a turn with neither kind is conversation.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

DEVELOPMENT_TOOLS = frozenset({"Edit", "Write", "NotebookEdit", "MultiEdit"})
JY_TOOL_PREFIX = "mcp__jy_bringup__"
TOKEN_KEYS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")

_CACHE: dict[str, tuple[tuple[float, int], list["Call"]]] = {}


@dataclass
class Call:
    message_id: str
    timestamp: datetime
    model: str
    speed: str
    tokens: dict[str, int]
    turn: str
    tools: set[str] = field(default_factory=set)


def parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def default_transcript_dir(agent_root: Path) -> Path | None:
    """The Claude Code project folder for the repository that holds .mcp.json."""
    repo = next(
        (p for p in [agent_root, *agent_root.parents] if (p / ".mcp.json").is_file()),
        None,
    )
    projects = Path.home() / ".claude" / "projects"
    if repo is None or not projects.is_dir():
        return None
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(repo)).lower()
    for candidate in projects.iterdir():
        if candidate.is_dir() and candidate.name.lower() == slug:
            return candidate
    return None


def load_calls(root: Path) -> list[Call]:
    """Every distinct API call in every transcript below ``root``."""
    calls: dict[str, Call] = {}
    for path in sorted(root.rglob("*.jsonl")):
        if "memory" in path.parts or "tool-results" in path.parts:
            continue
        for call in _file_calls(path):
            known = calls.get(call.message_id)
            if known is None:
                calls[call.message_id] = call
            else:
                known.tools |= call.tools
    return sorted(calls.values(), key=lambda item: item.timestamp)


def _file_calls(path: Path) -> list[Call]:
    stat = path.stat()
    key = str(path)
    signature = (stat.st_mtime, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    by_id: dict[str, Call] = {}
    turn = 0
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            kind = entry.get("type")
            message = entry.get("message") if isinstance(entry.get("message"), dict) else {}
            if kind == "user" and _is_user_input(message.get("content")):
                turn += 1
                continue
            if kind != "assistant" or not isinstance(message.get("usage"), dict):
                continue
            stamp = parse_time(entry.get("timestamp"))
            message_id = str(message.get("id") or entry.get("requestId") or "")
            if stamp is None or not message_id:
                continue
            tools = {
                str(block.get("name"))
                for block in message.get("content") or []
                if isinstance(block, dict) and block.get("type") == "tool_use"
            }
            call = by_id.get(message_id)
            if call is None:
                usage = message["usage"]
                by_id[message_id] = Call(
                    message_id=message_id,
                    timestamp=stamp,
                    model=str(message.get("model") or "unknown"),
                    speed=str(usage.get("speed") or "standard"),
                    tokens=_tokens(usage),
                    turn=f"{path.name}#{turn}",
                    tools=tools,
                )
            else:
                call.tools |= tools
    calls = list(by_id.values())
    _CACHE[key] = (signature, calls)
    return calls


def _is_user_input(content: Any) -> bool:
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        return any(
            isinstance(block, dict) and block.get("type") != "tool_result"
            for block in content
        )
    return False


def _tokens(usage: dict[str, Any]) -> dict[str, int]:
    detail = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    write_total = int(usage.get("cache_creation_input_tokens") or 0)
    write_1h = int(detail.get("ephemeral_1h_input_tokens") or 0)
    write_5m = int(detail.get("ephemeral_5m_input_tokens") or (write_total - write_1h))
    return {
        "input": int(usage.get("input_tokens") or 0),
        "cache_write_5m": max(write_5m, 0),
        "cache_write_1h": write_1h,
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }


def call_cost(call: Call, pricing: dict[str, Any]) -> float | None:
    """Estimated USD at API list prices; None for a model without a price."""
    prices = (pricing.get("models") or {}).get(call.model)
    if not isinstance(prices, dict):
        return None
    rate_in = float(prices["input"])
    cost = (
        call.tokens["input"] * rate_in
        + call.tokens["cache_write_5m"] * rate_in * float(pricing.get("cache_write_5m_multiplier", 1.25))
        + call.tokens["cache_write_1h"] * rate_in * float(pricing.get("cache_write_1h_multiplier", 2.0))
        + call.tokens["cache_read"] * float(prices["cache_read"])
        + call.tokens["output"] * float(prices["output"])
    ) / 1_000_000
    if call.speed == "fast":
        cost *= float(pricing.get("fast_multiplier", 2.0))
    return cost


def _empty() -> dict[str, Any]:
    return {"calls": 0, "cost_usd": 0.0, "unpriced_calls": 0, "models": {}, **{k: 0 for k in TOKEN_KEYS}}


def _add(bucket: dict[str, Any], call: Call, cost: float | None) -> None:
    bucket["calls"] += 1
    for key in TOKEN_KEYS:
        bucket[key] += call.tokens[key]
    if cost is None:
        bucket["unpriced_calls"] += 1
    else:
        bucket["cost_usd"] += cost
    bucket["models"][call.model] = bucket["models"].get(call.model, 0) + 1


def attribute_usage(
    calls: Iterable[Call],
    runs: list[dict[str, Any]],
    pricing: dict[str, Any],
    start: datetime,
    end: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate calls into per-run, per-node, setup and development buckets."""
    ordered = sorted(
        (run for run in runs if parse_time(run.get("started_at")) is not None),
        key=lambda run: parse_time(run["started_at"]),
    )
    starts = [parse_time(run["started_at"]) for run in ordered]
    selected = [
        call
        for call in calls
        if call.timestamp >= start and (end is None or call.timestamp <= end)
    ]
    labels = _labels(selected)
    per_run = {str(run["id"]): _empty() for run in ordered}
    setup, development, conversation, total = _empty(), _empty(), _empty(), _empty()
    for call in selected:
        cost = call_cost(call, pricing)
        _add(total, call, cost)
        label = labels[call.message_id]
        if label == "development":
            _add(development, call, cost)
            continue
        if label == "conversation":
            _add(conversation, call, cost)
            continue
        index = _window(starts, call.timestamp)
        if index is None:
            _add(setup, call, cost)
        else:
            _add(per_run[str(ordered[index]["id"])], call, cost)
    run_rows = []
    per_node: dict[str, dict[str, Any]] = {}
    for run in ordered:
        bucket = per_run[str(run["id"])]
        node = str(run.get("node_id"))
        run_rows.append(
            {
                "run_id": str(run["id"]),
                "node_id": node,
                "qubits": list(run.get("qubits") or []),
                "started_at": run.get("started_at"),
                **bucket,
            }
        )
        node_bucket = per_node.setdefault(node, {**_empty(), "runs": 0})
        node_bucket["runs"] += 1
        for key in ("calls", "cost_usd", "unpriced_calls", *TOKEN_KEYS):
            node_bucket[key] += bucket[key]
        for model, count in bucket["models"].items():
            node_bucket["models"][model] = node_bucket["models"].get(model, 0) + count
    return {
        "runs": run_rows,
        "nodes": per_node,
        "setup": setup,
        "development": development,
        "conversation": conversation,
        "total": total,
    }


def _labels(calls: list[Call]) -> dict[str, str]:
    """measurement / development / conversation for every call."""
    turns: dict[str, list[Call]] = {}
    for call in sorted(calls, key=lambda item: item.timestamp):
        turns.setdefault(call.turn, []).append(call)
    labels: dict[str, str] = {}
    for members in turns.values():
        own: list[str | None] = []
        for call in members:
            if call.tools & DEVELOPMENT_TOOLS:
                own.append("development")
            elif any(name.startswith(JY_TOOL_PREFIX) for name in call.tools):
                own.append("measurement")
            else:
                own.append(None)
        for position, call in enumerate(members):
            label = own[position]
            if label is None:
                later = next((item for item in own[position + 1 :] if item), None)
                earlier = next((item for item in reversed(own[:position]) if item), None)
                label = later or earlier or "conversation"
            labels[call.message_id] = label
    return labels


def _window(starts: list[datetime], stamp: datetime) -> int | None:
    index = None
    for position, begin in enumerate(starts):
        if begin <= stamp:
            index = position
        else:
            break
    return index
