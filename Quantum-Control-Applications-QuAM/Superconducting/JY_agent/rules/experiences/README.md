# Calibration experience log

This directory preserves reproducible experience for every bring-up node:
`02x`, `02a`, `02c`, `03a`, `04`, `05`, `07b`, `06`, `06b`, `10a`,
`05st`, `06st_t2star`, and `06st_t2e`.

Before proposing a run, read the relevant `<node>.md` file when it exists.
Accepted settings are evidence-backed starting points, not universal defaults;
the current qubit must independently pass every policy and node acceptance
check. Prefer experience from the same device/project and similar wiring or
qubit type. Never copy a learned frequency, amplitude, power, or lifetime into
active state without the normal run analysis and separately approved commit.

Append an `Accepted setting` only after a saved snapshot passes the complete
node-specific checks. Include:

- Taipei timestamp, device/project, workflow ID, node, qubit subgroup, run ID,
  and snapshot ID/path.
- Complete run parameters needed to reproduce the result.
- Accepted fitted or manually selected values and the decisive evidence, such
  as SNR, edge fraction, FWHM, plateau/depletion evidence, Rabi fit, or T1 fit.
- Decision, reason, result-plot path, and any separately approved state commit.
- Applicability limits and what must still be revalidated on another qubit.

A useful failed attempt or recovery technique may be appended as a `Lesson`,
with the same run and snapshot references plus the observed failure and recovery
conditions. A lesson must never be presented as an accepted setting. Do not log
speculative conclusions or a run that lacks enough evidence to support them.

In addition to curated Markdown accepted settings, every run decision is
automatically retained in SQLite with its full parameters, elapsed time, snapshot,
analysis, Decision, Reason, and Next action. Agents should query this complete
decision history with `jy_get_decision_experience`; the Markdown files remain the
smaller human-curated subset of accepted settings and lessons.
