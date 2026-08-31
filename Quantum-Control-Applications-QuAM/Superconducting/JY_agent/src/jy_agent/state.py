from __future__ import annotations

import copy
import json
import math
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .util import atomic_write_json, exclusive_file_lock, sha256_file


class StateError(ValueError):
    pass


def load_state(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8-sig") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise StateError(f"State root must be an object: {path}")
    return value


def pointer_parts(pointer: str) -> list[str]:
    if not pointer.startswith("/"):
        raise StateError(f"Invalid JSON pointer: {pointer}")
    if pointer == "/":
        return [""]
    return [
        part.replace("~1", "/").replace("~0", "~")
        for part in pointer[1:].split("/")
    ]


def pointer_get(document: Any, pointer: str) -> Any:
    current = document
    for part in pointer_parts(pointer):
        if isinstance(current, dict):
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise StateError(f"Cannot traverse {pointer}")
    return current


def _pointer_parent(document: Any, pointer: str) -> tuple[Any, str]:
    parts = pointer_parts(pointer)
    if not parts:
        raise StateError("Cannot modify the document root")
    current = document
    for part in parts[:-1]:
        if isinstance(current, dict):
            if part not in current:
                current[part] = {}
            current = current[part]
        elif isinstance(current, list):
            current = current[int(part)]
        else:
            raise StateError(f"Cannot traverse {pointer}")
    return current, parts[-1]


def apply_json_patch(
    document: dict[str, Any], patch: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    result = copy.deepcopy(document)
    for item in patch:
        operation = item.get("op")
        pointer = item.get("path")
        if operation not in {"add", "replace"} or not isinstance(pointer, str):
            raise StateError(f"Unsupported patch item: {item}")
        parent, key = _pointer_parent(result, pointer)
        if isinstance(parent, dict):
            if operation == "replace" and key not in parent:
                raise StateError(f"replace target does not exist: {pointer}")
            parent[key] = copy.deepcopy(item.get("value"))
        elif isinstance(parent, list):
            index = int(key)
            if operation == "replace":
                parent[index] = copy.deepcopy(item.get("value"))
            else:
                parent.insert(index, copy.deepcopy(item.get("value")))
        else:
            raise StateError(f"Cannot modify {pointer}")
    return result


def json_diff(before: Any, after: Any, pointer: str = "") -> list[dict[str, Any]]:
    """Return scalar add/replace operations; list structure changes are not proposed."""
    changes: list[dict[str, Any]] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(after.keys() | before.keys()):
            escaped = key.replace("~", "~0").replace("/", "~1")
            child = f"{pointer}/{escaped}"
            if key not in before:
                changes.append({"op": "add", "path": child, "value": after[key]})
            elif key not in after:
                continue
            else:
                changes.extend(json_diff(before[key], after[key], child))
        return changes
    if isinstance(before, list) or isinstance(after, list):
        return changes if before == after else [
            {"op": "replace", "path": pointer, "value": after}
        ]
    if before != after:
        changes.append({"op": "replace", "path": pointer, "value": after})
    return changes


def recorded_updates_to_patch(
    updates: dict[str, Any], base_state: dict[str, Any]
) -> list[dict[str, Any]]:
    """Convert Qualibrate's recorded QuAM updates into JSON Patch operations."""
    if not isinstance(updates, dict):
        raise StateError("Recorded state updates must be an object")
    patch: list[dict[str, Any]] = []
    for reference in sorted(updates):
        item = updates[reference]
        if not isinstance(item, dict):
            raise StateError(f"Invalid recorded state update at {reference!r}")
        key = item.get("key", reference)
        if not isinstance(key, str):
            raise StateError(f"Recorded state update has no reference at {reference!r}")
        if key.startswith("#/"):
            pointer = key[1:]
        elif key.startswith("/"):
            pointer = key
        else:
            raise StateError(f"Unsupported recorded state reference: {key!r}")
        try:
            pointer_get(base_state, pointer)
        except (KeyError, IndexError, StateError):
            operation = "add"
        else:
            operation = "replace"
        patch.append(
            {"op": operation, "path": pointer, "value": copy.deepcopy(item.get("new"))}
        )
    return patch


def filter_patch(
    patch: Iterable[dict[str, Any]], allowed_patterns: Iterable[str]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    patterns = [re.compile(pattern) for pattern in allowed_patterns]
    allowed: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for item in patch:
        destination = str(item.get("path", ""))
        (allowed if any(p.fullmatch(destination) for p in patterns) else rejected).append(item)
    return allowed, rejected


def operation_backing_name(qubit: dict[str, Any], operation: str) -> str:
    operations = qubit["xy"]["operations"]
    entry = operations.get(operation)
    if isinstance(entry, str) and entry.startswith("#./"):
        return entry[3:]
    if isinstance(entry, dict):
        return operation
    raise StateError(f"Cannot resolve XY operation {operation!r}")


def operation_amplitude(qubit: dict[str, Any], operation: str) -> float:
    backing = operation_backing_name(qubit, operation)
    value = qubit["xy"]["operations"][backing].get("amplitude")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise StateError(f"{operation} amplitude is not numeric")
    return float(value)


def channel_kind(qubit: dict[str, Any]) -> str:
    class_name = str(qubit.get("xy", {}).get("__class__", ""))
    if class_name.endswith("MWChannel"):
        return "mw"
    if class_name.endswith("IQChannel"):
        return "iq"
    raise StateError(f"Unsupported XY channel class: {class_name or '<missing>'}")


def bootstrap_patch(
    state: dict[str, Any],
    qubits: list[str],
    policies: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    limits = policies["instrument_limits"]
    x180_fraction = float(limits["bootstrap_x180_fraction"])
    x90_fraction = float(limits["bootstrap_x90_fraction_of_x180"])
    patch: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    for name in qubits:
        qubit = state["qubits"][name]
        kind = channel_kind(qubit)
        x180_limit = float(limits[kind]["max_x180_wf_amplitude"])
        initial_x180 = x180_limit * x180_fraction
        initial_x90 = initial_x180 * x90_fraction
        x180_backing = operation_backing_name(qubit, "x180")
        x90_backing = operation_backing_name(qubit, "x90")
        old_x180 = operation_amplitude(qubit, "x180")
        old_x90 = operation_amplitude(qubit, "x90")
        changed: dict[str, Any] = {}
        if old_x180 == 0:
            path = f"/qubits/{name}/xy/operations/{x180_backing}/amplitude"
            patch.append({"op": "replace", "path": path, "value": initial_x180})
            changed["x180"] = {"old": old_x180, "new": initial_x180}
        if old_x90 == 0:
            # Use the bootstrapped x180 value only when x180 itself was missing/zero.
            x90_value = initial_x90 if old_x180 == 0 else old_x180 * x90_fraction
            path = f"/qubits/{name}/xy/operations/{x90_backing}/amplitude"
            patch.append({"op": "replace", "path": path, "value": x90_value})
            changed["x90"] = {"old": old_x90, "new": x90_value}
        details[name] = {
            "channel_kind": kind,
            "max_x180_wf_amplitude": x180_limit,
            "changes": changed,
        }
    return patch, details


def drive_lo_recenter_patch(
    state: dict[str, Any],
    wiring: dict[str, Any],
    qubits: list[str],
    lo_grid_hz: float = 100_000_000,
    *,
    force_zero_if: bool = False,
    target_lo_hz: dict[str, float] | None = None,
    target_rf_hz: dict[str, float] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Build private-XY LO/IF patches for 03a coarse-search placement."""
    if not qubits or len(set(qubits)) != len(qubits):
        raise StateError("LO recenter qubits must be a non-empty unique list")
    if (
        not isinstance(lo_grid_hz, (int, float))
        or isinstance(lo_grid_hz, bool)
        or not math.isfinite(float(lo_grid_hz))
        or float(lo_grid_hz) <= 0
    ):
        raise StateError("LO grid must be finite and positive")
    if target_lo_hz is not None and target_rf_hz is not None:
        raise StateError("Explicit LO and RF targets are mutually exclusive")
    if target_lo_hz is not None and set(target_lo_hz) != set(qubits):
        raise StateError("Explicit LO centers must exactly match selected qubits")
    if target_rf_hz is not None and set(target_rf_hz) != set(qubits):
        raise StateError("Explicit RF centers must exactly match selected qubits")
    wiring_qubits = wiring.get("wiring", {}).get("qubits", {})
    if not isinstance(wiring_qubits, dict):
        raise StateError("Wiring does not contain a qubit map")

    patch: list[dict[str, Any]] = []
    details: dict[str, Any] = {}
    for name in qubits:
        try:
            qubit = state["qubits"][name]
            xy = qubit["xy"]
        except (KeyError, TypeError) as exc:
            raise StateError(f"Unknown qubit for LO recenter: {name}") from exc
        if not str(xy.get("__class__", "")).endswith("MWChannel"):
            raise StateError(f"{name} XY channel is not an MWChannel")

        output_ref = wiring_qubits.get(name, {}).get("xy", {}).get("opx_output")
        if not isinstance(output_ref, str):
            direct_ref = xy.get("opx_output")
            output_ref = direct_ref if isinstance(direct_ref, str) else None
        match = re.fullmatch(
            r"#/ports/mw_outputs/([^/]+)/([0-9]+)/([0-9]+)",
            output_ref or "",
        )
        if match is None:
            raise StateError(f"{name} has no resolvable MW XY output")

        owners = sorted(
            owner
            for owner, item in wiring_qubits.items()
            if isinstance(item, dict)
            and item.get("xy", {}).get("opx_output") == output_ref
        )
        if owners != [name]:
            raise StateError(
                f"{name} XY output is shared by {owners or ['unknown']}; "
                "automatic LO recenter requires a private output"
            )

        port_path = output_ref[1:]
        lo_path = f"{port_path}/upconverter_frequency"
        if_path = f"/qubits/{name}/xy/intermediate_frequency"
        try:
            old_lo = pointer_get(state, lo_path)
            old_if = pointer_get(state, if_path)
        except (KeyError, IndexError, StateError) as exc:
            raise StateError(f"Cannot resolve LO/IF state for {name}") from exc
        if not all(
            isinstance(value, (int, float)) and not isinstance(value, bool)
            for value in (old_lo, old_if)
        ):
            raise StateError(f"{name} LO and IF must be numeric")

        old_rf = float(old_lo) + float(old_if)
        grid = float(lo_grid_hz)
        if target_rf_hz is not None:
            requested_rf = target_rf_hz[name]
            if (
                not isinstance(requested_rf, (int, float))
                or isinstance(requested_rf, bool)
                or not math.isfinite(float(requested_rf))
            ):
                raise StateError(f"{name} explicit RF center must be finite")
            new_lo = math.floor(float(requested_rf) / grid + 0.5) * grid
            new_if = float(requested_rf) - new_lo
            mode = "candidate_center_preserve_rf"
        elif target_lo_hz is not None:
            requested_lo = target_lo_hz[name]
            if (
                not isinstance(requested_lo, (int, float))
                or isinstance(requested_lo, bool)
                or not math.isfinite(float(requested_lo))
            ):
                raise StateError(f"{name} explicit LO center must be finite")
            grid_units = float(requested_lo) / grid
            if not math.isclose(grid_units, round(grid_units), abs_tol=1e-9):
                raise StateError(
                    f"{name} explicit LO center must lie on the {grid:g} Hz grid"
                )
            new_lo = float(requested_lo)
            new_if = 0.0
            mode = "shifted_coarse_window"
        elif force_zero_if:
            new_lo = old_rf
            new_if = 0.0
            mode = "preserve_rf_zero_if"
        else:
            new_lo = math.floor(old_rf / grid + 0.5) * grid
            new_if = old_rf - new_lo
            mode = "nearest_grid_preserve_rf"
        patch.extend(
            [
                {"op": "replace", "path": lo_path, "value": new_lo},
                {"op": "replace", "path": if_path, "value": new_if},
            ]
        )
        details[name] = {
            "mode": mode,
            "xy_output": output_ref,
            "lo_grid_hz": grid,
            "old_lo_hz": float(old_lo),
            "new_lo_hz": new_lo,
            "old_if_hz": float(old_if),
            "new_if_hz": new_if,
            "old_rf_hz": old_rf,
            "new_rf_hz": new_lo + new_if,
            "preserved_rf_hz": old_rf,
            "target_rf_hz": (
                float(target_rf_hz[name]) if target_rf_hz is not None else None
            ),
        }
    return patch, details


def commit_state(
    state_path: Path,
    patch: list[dict[str, Any]],
    backup_dir: Path,
    expected_hash: str,
) -> dict[str, str]:
    lock_path = backup_dir.parent / "state-commit.lock"
    try:
        with exclusive_file_lock(lock_path, "active state commit"):
            actual_hash = sha256_file(state_path)
            if actual_hash != expected_hash:
                raise StateError(
                    "Active state changed after this proposal was created; request a new proposal."
                )
            original = load_state(state_path)
            updated = apply_json_patch(original, patch)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup_path = backup_dir / (
                f"{timestamp}-{actual_hash[:12]}-{uuid.uuid4().hex[:12]}-state.json"
            )
            shutil.copy2(state_path, backup_path)
            atomic_write_json(state_path, updated)
            after_hash = sha256_file(state_path)
    except RuntimeError as exc:
        raise StateError(str(exc)) from exc
    return {
        "before_hash": actual_hash,
        "after_hash": after_hash,
        "backup_path": str(backup_path),
    }


def snapshot_state_path(snapshot_path: Path) -> Path | None:
    candidates = [
        snapshot_path / "quam_state" / "state.json",
        snapshot_path / "quam_state.json",
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), None)
