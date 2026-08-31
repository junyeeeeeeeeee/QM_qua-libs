# JY superconducting-qubit bring-up playbook

## Non-negotiable rules

1. Do not start or resume a workflow until the user says exactly either
   `進入 JY 量測模式` (conversational) or `進入 JY 自動量測模式` (bounded
   automation). Configured exit/stop wording such as `退出 JY 量測模式`,
   `結束量測`, or `停止量測` always means full shutdown: persist the request,
   cooperatively stop/poll any active run (force only the verified worker after
   its grace period), stop or quarantine the workflow, and run `stop_server.ps1`
   to close MCP, Approval HTTP, and the public tunnel. A retained lock is kept
   for local recovery but does not keep those services alive. Outside measurement mode, do not call
   experiment or state-changing JY tools.
2. Treat `calibration_graph/` and `Script/` as read-only. Call only the six
   allowlisted calibration files through JY tools.
3. Read the active workflow and deterministic analysis before choosing an action.
   A node's own `successful` outcome is advisory, not proof.
4. In conversational mode call `jy_enter_measurement_mode`, preserve its session
   `browser_url`, then create exactly one `jy_request_conversational_run` proposal
   at a time. Every run/state approval appears on the Dashboard Approval page;
   results and controls remain on the Results page. The node may be
   any policy-registered experiment. In automatic mode call
   `jy_enter_autonomy_mode`, give the human its session `browser_url`, wait with
   `jy_wait_for_autonomy_status`, and use only `jy_autonomy_*` tools after the one
   lease activation. When the host supports a durable goal, keep one automatic
   session goal alive until completion, operator stop, lease expiry, or hard stop.
   A URL may be local or a
   token-protected public HTTPS URL; never replace it with a guessed URL.
   Preserve the same top-level `/` entry for the workflow/service lifetime. It
   links to Home, Approval, and Results & controls; every run remains in the
   numbered dropdown until shutdown.
5. Never write `state.json` directly. Derive a JSON patch, explain each change,
   and apply it only through the lease's passing-evidence state-commit tool.
6. After every terminal run and deterministic analysis, directly embed every
   returned result plot in the conversation before continuing. A local file path
   or link alone does not satisfy this requirement.
7. Follow the plots with `Decision`, `Reason`, and `Next action` (`next node`,
   `new parameters`) in only two or three short sentences. The existing control
   page appends them automatically; do not create another approval page.
8. If a failed or stopped run produced no snapshot or plot, explicitly report
   that no result plot exists and state the failure reason before proposing any
   recovery action.
9. Before proposing a node, read its accepted and lesson entries under
   `rules/experiences/`. After accepting a run, append the complete reproducible
   setting and evidence to `rules/experiences/<node>.md`. Experience selects a useful
   starting point; it never bypasses the current node's acceptance checks.

## Workflow

The default sequence is:

`02x → 02a → 02c → 03a → 04 → 05 → 07b → 06 → 06b → 10a → 05st → 06st_t2star → 06st_t2e`

`06b` is mandatory and runs immediately after `06` in the default workflow.

The sequence is dynamic rather than a pre-approved batch. After every run:

1. Wait on bounded status/events until the worker reaches a terminal state.
2. Call `jy_analyze_run`.
3. Inspect the result plots, fit evidence, edge distance, robust SNR, candidate
   state patch, and policy warnings.
4. Directly embed every returned plot in the conversation.
5. Choose `advance`, `repeat`, `manual_review`, or `stop`, then report the
   `Decision` and `Reason`.
6. Record the decision and next parameters. For an accepted run, record the
   successful settings and evidence using `rules/experiences/README.md`; record a
   useful failed/recovery observation only as a labeled lesson. Commit any state
   patch only through the active lease and identical passing evidence.
7. Continue within scope after the result report. Explicitly say when the
   workflow is complete, stopped, expired, or halted.

Every run retains its complete parameters, start/finish timestamps, elapsed time,
snapshot, analysis, Decision, Reason, and Next action in the SQLite audit database.
Use `jy_get_run_telemetry` for duration forecasts and
`jy_get_decision_experience` before choosing parameters for a similar run. The
approved autonomy URL becomes an event-driven, append-only dashboard. It reloads
only for a newly analyzed result/decision or a manual/automatic control-state
change, and it keeps every run, plot, parameter set, elapsed time, Decision,
Reason, and Next action from that workflow/service lifetime. A plot can appear only after the worker
has produced a snapshot; intra-acquisition partial plots remain a future feature.

Manual retry proposals have no lifetime total limit. A single run approval may
cover at most the policy-configured number of unattended retries and expires.

In bounded autonomy, every `(node, qubit)` has its own 20-attempt counter. If a
qubit reaches 20 attempts without usable evidence, report it as incomplete for
that node and never request attempt 21 under the same lease. Quota exhaustion is
target-local: finish the remaining qubits, record `advance` when every active
target is either resolved or incomplete at quota, and carry only resolved qubits
into the next node. Do not treat this as a lease-wide halt. The configured hard
stops for missing snapshots, worker failures, state-hash conflicts, hardware-lock
anomalies, and parameter-policy violations still halt the entire lease.
While the lease remains active, continue scheduling safe in-scope work until the
workflow finishes. Do not stop merely because a run is `needs_review`: retry an
unresolved subgroup, increase evidence quality within policy, or advance the
already resolved qubits after the other targets become scientifically unusable,
reach their local quota, or run out of lease time.
If every safe authorized adjustment for a qubit has been exhausted, leave it
incomplete, continue the remaining resolvable qubits, and give the user a compact
summary of attempted parameters and observed evidence. When the user explains a
new strategy, add that instruction to the relevant experience/rule file before
using it in later runs.
Before a new autonomy lease is created, the server revalidates previously
advanced 04/05 evidence against the current SNR floors. If an older advancement
no longer qualifies, the workflow deterministically reopens the earliest affected
node and records `workflow_evidence_revalidation`; the new lease must start there.

`暫停 JY 自動量測` pauses only the lease. Dialogue exit/stop/shutdown wording
permanently stops the workflow and closes MCP, Approval HTTP, and the public
tunnel after the active run is safely terminal. If cleanup cannot be verified,
the lock survives in recovery quarantine and the next Ensure is local-only.

## Parameter handling

- Always pass explicit `qubits`; multi-qubit runs are allowed.
- `02x` and `02a` retain their hardcoded minimum-flux behavior.
- `02c`, `03a`, `04`, and `05` must use `joint`.
- Stay within the server-returned policy limits. For 03a, the full sweep must
  stay inside the ±400 MHz qubit IF range.
- If x180 is missing or zero before 03a/04/05, call `jy_request_bootstrap`.
  Bootstrap x180 is `0.5 × max_x180_wf_amplitude`; a missing/zero x90 is set
  to `x180 / 2`. Never overwrite an existing nonzero x180 or x90.

### Multiplex workflows

To measure a fixed target set concurrently inside every node, start the workflow
with `initial_parameters: {"multiplexed": true}`. The server then forces
`multiplexed=true` on every run proposal and refuses attempts to disable it.
For the q3–q8 profile, use the exact targets
`["q3", "q4", "q5", "q6", "q7", "q8"]`.

Before creating the workflow or a run proposal, server policy validates target
count, resonator input/output wiring, same-line IF spacing, and aggregate readout
amplitude. Multiplexing applies to the qubits within one node; calibration nodes
still execute one at a time with a separate approval decision and analysis for
each, all displayed on the same session Dashboard.

## Node decisions

### 02x — bare resonator spectroscopy

Advance only when every target has a visible resonator feature, adequate robust
SNR, and the feature is not at a sweep edge. Repeat with a wider span if the
feature is clipped; increase averages or adjust the step when the trace is noisy.
The protected node records `extras/bare_resonator_freq`; if that key is absent,
initialize it to the qubit's current resonator RF frequency through a separately
approved state-commit proposal before running 02x.

### 02a — dressed resonator spectroscopy

Advance when the dressed resonance is resolved for every target and the proposed
frequency change is finite and within IF limits. Repeat with a recentered or wider
sweep if the feature is at an edge. In bounded autonomy, 02a remains a fixed
multiplex batch: a retry motivated by one target must still run every workflow
target. The service normalizes a target-local `qubits` request to the full
authorized workflow target set and records the adjustment. If the lease itself
covers only a subgroup, create a new bounded lease covering all workflow targets;
the rejected request must not touch hardware or halt the existing lease.

### 02c — resonator spectroscopy versus amplitude

Validate the tracked resonator frequency as power increases from low power
(more-negative dBm) to high power. The low-power tail must form a stable
`dressed frequency` plateau, and the high-power tail must form a separate stable
`bare frequency` plateau. The intermediate `depletion region` must contain
frequencies between those plateaus and move predominantly from dressed toward
bare as power increases (equivalently, toward dressed as power decreases).

The target readout power is the highest power that still belongs to the dressed
plateau. JY's deterministic plateau analysis is authoritative for the proposed
frequency and power; the protected node's derivative-threshold fit is advisory
and must be ignored when it is empty or disagrees with the dressed boundary.
Missing or edge-clipped plateaus, too few depletion points, or a non-monotonic
transition still require repeat or manual review.

An 02c retry may measure any non-empty subset of the workflow targets while
preserving `multiplexed: true`. Use separate subgroups when qubits need different
frequency or power ranges (for example q3-q5 and q1-q2), and do not advance to
03a until every original target has either a valid dressed point or satisfies
the strict bare-only absence rule.

A qubit may be marked `bare_only_absent` and skipped in every later node only
when all safeguards pass: the full power trace is a straight line matching the
previously measured bare frequency, minimum power reaches at least -50 dBm, the
configured frequency span is at most 10 MHz with step at most 0.1 MHz, at least
40 power points are present, the bare resonance remains resolved and away from
sweep edges, and either readout integration is at least 1000 ns or
`num_averages` is at least 200. Every state update still requires a separate
human-approved state-commit proposal.

### 03a — qubit spectroscopy

Require `fit_successful=true`, a finite drive frequency, adequate SNR, and a peak
away from sweep edges. If no peak is found, first adjust drive amplitude/averages,
then span and step while preserving the ±400 MHz IF limit. Do not accept a
clamped or non-finite x180 proposal.

Before the first 03a run, call `jy_request_initial_03a_zero_if` and
apply its separately human-approved state commit. It must preserve each active
target's current RF frequency by moving that target's private LO to the RF and
setting every XY IF to zero. The first coarse run must then measure all active
workflow targets together with `multiplexed: true`, an 800 MHz full span, the
configured stronger but policy-safe saturation factor, and enough coarse points
to avoid stepping over a resonance.

Never accept the protected node's fit or success outcome by itself. A coarse peak
is a credible candidate only when the fit is finite and successful, the feature
is at least 5% away from either sweep edge, and robust SNR is at least 10. The
signal prominence must visibly exceed the surrounding noise; multiple noisy
local extrema are not a candidate even when the node reports a fit. High-power
coarse runs must not produce a state patch or count as final 03a evidence.

At higher drive power, explicitly check for a paired-transition signature before
accepting the fitted frequency. A narrow lower-frequency peak can be the
two-photon `|0> -> |2>` transition divided by two, and may be stronger than the
fundamental. When another credible, distinctly broader peak appears roughly
`|anharmonicity| / 2` above it (commonly about 100 MHz; the configured detection
window is 50-150 MHz), treat the lower narrow peak as the two-photon candidate
and select the higher-frequency peak as the `|0> -> |1>` candidate. Record both
peaks, their separation, widths, and SNR. The protected node's strongest-peak
fit must be overridden when it selected the lower member of such a pair. This
pair rule only chooses a coarse candidate; the higher-frequency result must
still pass a separately centered low-power fine scan.

For an unresolved target whose strongest feature is inside the sweep but robust
SNR is below 10, keep the same LO window and increase `num_averages` through
500, 1000, and at most 2000 as needed. Do not move its LO before this noise
confirmation reaches 2000 averages. If no credible peak remains at 2000
averages, it may move to another LO window. An edge-limited feature may shift
immediately because the current window does not cover it adequately.
When a high-SNR coarse candidate fails reproducibility at the same power and
2000 averages, record `manual_review` to explicitly reject that candidate.
The rejected target is no longer treated as found, becomes eligible for an LO
window shift, and may regain candidate status only from a later credible run.

Use `jy_request_03a_window_shift` only for targets that satisfy the preceding
shift rule. Move that unresolved subgroup to explicitly chosen 100 MHz-grid LO
centers with IF=0, then repeat the 800 MHz coarse search. Never shift an already
found target or a shared XY output automatically.

After each target has a credible candidate, lower the drive power and iteratively
reduce span and step size. Candidate targets may use an intermediate refinement
stage (for example amplitude factor 0.075) when a direct drop from the coarse
factor to 0.05 makes a previously reproducible peak disappear. Refinement scans
never produce a state patch and must still be followed by the final low-power
scan. A final 03a result requires a span no greater than
100 MHz, an operation amplitude factor no greater than 0.05, a stable finite
fit, robust SNR of at least 10, and a feature at least 5% away from either sweep
edge. Only this fine-scan evidence may update idle frequency or resolve the
target.
The final feature FWHM must also lie between 0.5 and 10 MHz. A broader feature is
still power-broadened and must not resolve the target even when fit and SNR pass;
reduce `operation_amplitude_factor` and repeat, tightening span and step as
needed. If the feature becomes narrower than 0.5 MHz, slightly raise amplitude or
increase resolution before accepting it. Only commit `idle_freq` after the
measured FWHM falls inside this interval.

Before a candidate's refinement or fine scan, call
`jy_request_03a_candidate_center` when its stored RF center does not match the
credible coarse candidate. The server must derive the RF from the latest
non-rejected credible candidate, place the private XY LO on the nearest 100 MHz
grid, retain the residual IF, and validate the resulting state patch. Under an
active bounded-autonomy lease this deterministic setup may be delegated and
applied with `jy_autonomy_apply_setup_state`; it must never center a resolved,
non-candidate, shared-output, or out-of-policy target.

When `arbitrary_qubit_frequency_in_ghz` centers a fine or refinement scan, derive
RF as that explicit center plus the measured feature offset. Do not combine the
offset with the stored LO, and reject any protected-node frequency or state patch
that falls outside the actual requested sweep. A 03a scan that fails SNR, edge,
or fit checks must expose no candidate state patch.

An 03a run may measure any non-empty subset of the active workflow targets while
preserving `multiplexed: true`. Passing qubits accumulate only under these
rules. Retry only unresolved qubits, and do not advance to 04 until every active
target has final fine-scan evidence.

### 04 — power Rabi

Require a finite, nonzero fitted Pi amplitude and an oscillation that is not
edge-limited. Reduce `max_amp_factor` when the policy reports a waveform-limit
violation. Synchronizing x90 from an accepted x180 result is allowed, but its
state patch still requires human approval.

For 04 edge evidence, use the fitted `Pi_amplitude` relative to the recorded
amplitude-sweep minimum and maximum. A generic raw maximum/minimum can select a
small noisy local extremum even when the fitted Rabi extremum is visibly in the
interior; preserve that raw coordinate only as diagnostic evidence and do not
let it override a finite interior Pi fit. A fitted Pi amplitude within 5% of the
actual sweep boundary is still edge-limited and requires a repeat.

An 04 run may likewise measure any non-empty subset of the active workflow
targets while preserving `multiplexed: true`. Passing qubits accumulate across
runs when the Pi amplitude is finite and nonzero, robust SNR is at least 14, and
the feature is at least 5% away from either sweep edge. Retry only unresolved
qubits, and do not advance to 05 until every active target is resolved. This SNR
floor is anchored to the accepted q3/q4/q5 reference run (minimum 14.86). If a
trace is below the floor, call `jy_recommend_0405_snr_retry` and increase only
that subgroup through 500, 1000, 2000, then 4000 averages.

### 05 — T1

Require a finite positive exponential-decay lifetime, relative uncertainty below
0.25, R-squared of at least 0.90, at least two samples per lifetime, and a wait-time
span covering at least 3.5 fitted lifetimes. A clipped or under-covered decay
requires a repeat with a longer range or adjusted step; every T1 state update
still requires human approval.

An 05 run may measure any non-empty subset of the active targets while preserving
`multiplexed: true`. Prefer isolated per-qubit runs after a multiplexed protected
fit raises before saving a snapshot, because one invalid vectorized fit must not
erase usable evidence for other qubits. Passing T1 evidence accumulates by qubit;
retry only unresolved targets. Because this workflow includes 100-run statistics,
final 05 evidence must cover at least 3.5 fitted lifetimes so that the trace has
reached its equilibrium tail. Use the emitted
`recommended_statistics_max_wait_time_in_ns` (4.5x the fitted lifetime, rounded
to a 4 ns boundary) when forming compatible 05st target subgroups. It must also
have robust SNR of at least 25, anchored to the accepted q3/q4/q5 reference run
(minimum 25.06). If only some traces are below that floor, call
`jy_recommend_0405_snr_retry` and repeat only those targets through 2000, 4000,
then 8000 averages. Reaching the averaging ceiling without the SNR floor leaves
the target unresolved; it does not convert a noisy fit into a completed result.

### 07b — IQ blobs

Require a saved discriminator plot and a finite per-qubit readout fidelity of at
least `0.70`, but fidelity alone is not sufficient. Each prepared-state distribution
must look like a compact, approximately round IQ cloud. Some overlap between the
two round clouds is acceptable; a long line/tail, crescent, or strongly elongated
cloud is a power-saturation signature and must fail even when fitted fidelity is
above 0.5. JY enforces covariance-axis and radial-tail ratios from `ds.h5`, and
suppresses every discriminator state patch when morphology fails.

The 99th-percentile radius must remain within `4x` the median radius for each
prepared-state cloud. This intentionally rejects sparse or curved tails that can
look visually dominant while a covariance-axis test still appears acceptable.

For morphology failure, use `jy_request_07b_tail_power_reduction` to reduce each
affected target's readout amplitude by the policy-fixed factor `sqrt(1/2)` (about
-3 dB power) and repeat 07b. This deterministic recovery requires a saved failed
07b snapshot, may be applied through an active bounded-autonomy lease, and stops
at the configured minimum amplitude. Repeat one fixed step at a time until the
plot contains two compact clouds; do not compensate a long tail with fidelity or
threshold fitting alone.

If an isolated target still fails fidelity or compact-cloud morphology at the
configured minimum amplitude, record `manual_review` on that evidence run and
call `jy_mark_scientifically_unmeasurable` for only the failed target. This marks
the target incomplete at 07b without spending the rest of its attempt quota and
allows already resolved targets to continue downstream. Never use this boundary
for a passing target, a batch target without target-specific evidence, or while
another policy-authorized recovery remains.

State updates for integration angle, thresholds, confusion matrix, and
readout fidelity remain evidence-gated commits. Before the first 07b run for a
target subgroup, call `jy_request_07b_prerequisites` and apply its separately
authorized deterministic setup if any `extras/readout_fidelity` mapping key is
missing. The setup value is a non-passing zero sentinel used only so Qualibrate's
state recorder can observe an old value; 07b must replace it with measured evidence.
Normalize library fidelity values expressed as percentages (for example `88.6`)
to fractions (`0.886`) before acceptance or state commit. Retry only unresolved
qubits.

### Reset selection — thermal by default, active only after 07b evidence

Every registered experiment defaults to thermal reset. An operator or agent may
deliberately run 07b with active reset to qualify a per-qubit switch. Active reset
is unlocked for a qubit only when that active-reset 07b run has two compact clouds,
passes every normal morphology rule, and retains normalized readout fidelity of at
least `0.85` (higher is preferred). A thermal 07b result, a fidelity-only result,
or an active result below `0.85` does not unlock active reset.

After qualification, later experiments may explicitly request active reset; the
switch is optional and is never inferred from an old default. Before any 05st or
06st run uses active reset, repeat and accept all three baseline nodes—05, 06, and
06b—with active reset for the same qubit(s). These conditional verification runs
may be performed without discarding the workflow's current downstream node. A
statistics request must use the same reset type as its accepted 05/06/06b
baselines.

### 06 / 06b — Ramsey T2* and echo T2

Run Ramsey followed by T2 echo. Require a finite positive lifetime, relative
uncertainty below 0.25, at least two samples per lifetime, and at least 3.5
lifetimes of coverage before resolving a qubit. T2 echo additionally requires
R-squared of at least 0.90 and a non-boundary fit. This longer coverage is
mandatory because both values feed downstream 100-run statistics. Use the
emitted 4.5x recommended statistics wait time when forming compatible target
subgroups. The Ramsey frequency correction and T2 state values remain separately
committed.

### 10a — single-qubit randomized benchmarking

Require a successful fit and finite EPC/EPG values in [0, 1] for every resolved
qubit. The depth must be divisible by `delta_clifford`, and retries may use a
target subgroup.

### 05st / 06st_t2star / 06st_t2e — 100-run statistics

Each statistics node requires exactly `histo_num=100`. It may start only after the
05, 06, and 06b results are all resolved with the same reset type. Each baseline
fit must cover at least 3.5 lifetimes. For the statistic being measured, the
requested `max_wait_time_in_ns` must lie between 3.5 and 5.5 times its accepted
T1/T2 value. This ensures that the baseline has reached equilibrium and the
statistics range is approximately four lifetimes; prefer 4.5 times the baseline.

Once those prerequisites pass, run each 05st/06st node once. JY does not refit all
100 raw iterations, does not impose a 100/100 secondary fitting gate, and does not
reject the entire statistics run because one independently re-analysed iteration
would fail. The protected node's summaries and plots remain informational; normal
worker completion, saved plot, exactly `histo_num=100`, and the already-validated
baseline prerequisites are sufficient to complete that statistics node. Any state
candidate produced by the registered statistics node remains subject to the usual
state-patch policy and commit authorization, but no extra statistics-specific
fitting check is added.

## Status meanings

- `run.status`: worker lifecycle (`starting`, `running`, `stopping`, `completed`,
  `failed`, `stopped`, `cancelled_by_shutdown`, `force_stopped`).
- `analysis_status`: deterministic evidence result (`pass`, `needs_review`,
  `failed`).
- `workflow.status`: bring-up progress (`active`, `completed`, `stopped`,
  `recovery_required`).
- `proposal.status`: approval lifecycle (`pending`, `approved`, `expired`,
  `consumed`).

Do not substitute one status for another.
