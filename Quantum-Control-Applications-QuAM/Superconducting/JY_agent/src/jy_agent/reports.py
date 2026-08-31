from __future__ import annotations

from typing import Any


def lightweight_report(
    analysis: dict[str, Any],
    decision: str,
    reason: str,
    next_node: str | None,
    new_parameters: dict[str, Any] | None,
) -> str:
    """Render the agreed plot + Decision/Reason/Next action report in 2–3 sentences."""
    plot_lines = [
        f"![result plot {index + 1}]({path})"
        for index, path in enumerate(analysis.get("plots", []))
    ]
    plot_block = "\n".join(plot_lines) if plot_lines else "_No result plot available._"
    sentence_one = f"Decision: {decision}. Reason: {reason.strip()}"
    sentence_two = (
        f"Next action: next node = {next_node or 'none'}; "
        f"new parameters = {new_parameters or {}}."
    )
    return f"{plot_block}\n\n{sentence_one}\n\n{sentence_two}"
