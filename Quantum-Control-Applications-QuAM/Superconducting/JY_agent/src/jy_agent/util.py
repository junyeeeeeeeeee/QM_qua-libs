from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
import uuid


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso_datetime(value: object) -> datetime:
    """Parse ISO timestamps emitted by Python or PowerShell/.NET.

    Windows PowerShell's round-trip ``o`` format writes seven fractional-second
    digits.  Python versions before 3.11 accept at most six, so trim only the
    excess precision before handing the value to ``datetime.fromisoformat``.
    """
    text = str(value).strip()
    text = re.sub(
        r"(?<=[T ])(\d{2}:\d{2}:\d{2})[.,](\d{6})\d+(?=Z|[+-]\d{2}:\d{2}|$)",
        r"\1.\2",
        text,
    )
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if value is None:
        return default
    return json.loads(value)


def json_compatible(value: Any) -> Any:
    """Recursively unwrap JSON-shaped third-party container/scalar types.

    QuAM uses list-like wrappers in recorded state updates.  Preserve their
    exact JSON structure for the audit file without falling back to lossy string
    representations of unknown objects.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {str(key): json_compatible(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return [json_compatible(item) for item in value]
    to_list = getattr(value, "tolist", None)
    if callable(to_list):
        return json_compatible(to_list())
    to_item = getattr(value, "item", None)
    if callable(to_item):
        return json_compatible(to_item())
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON compatible")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=4, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


@contextmanager
def exclusive_file_lock(path: Path, purpose: str) -> Iterator[dict[str, Any]]:
    """Acquire a fail-closed cross-process lock using atomic file creation.

    Locks are deliberately not stolen when their owner is unknown.  A stale
    safety lock must be inspected and removed by an operator instead of being
    guessed away by a second process.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    payload = {
        "token": token,
        "pid": os.getpid(),
        "purpose": purpose,
        "created_at": utc_now(),
    }
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(path, flags)
    except FileExistsError as exc:
        try:
            owner: Any = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            owner = {"status": "unreadable"}
        raise RuntimeError(f"Safety lock is already held at {path}: {owner}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        yield payload
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            current = None
        if isinstance(current, dict) and current.get("token") == token:
            path.unlink(missing_ok=True)


def is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
