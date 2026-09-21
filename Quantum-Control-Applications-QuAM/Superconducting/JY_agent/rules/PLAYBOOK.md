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
   links to Home, Approval, and Experiment results; every run remains in the
   numbered dropdown until shutdown. When ending a turn in measurement mode,
   copy `operator_handoff.chat` as a three-item markdown list, never as one
   paragraph, so the operator always sees what to do next, which exact phrase
   to send afterwards, and the shutdown hint. Successful
   entry shows only `browser_url`. After the operator sends `已核准`, the first
   line of the next reply is exactly `已核准`. A live turn may execute
   `結束量測` directly; a paused or disconnected turn needs Dashboard Home
   shutdown first, then `結束量測` to verify.
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

`02x → 02c → 02a → 03a → 04 → 05 → 07b → 06 → 06b → 10a → 05st → 06st_t2star → 06st_t2e`

The operator places already-correct resonator frequencies in `state.json`
before entry. Resonator spectroscopy (`02x`, `02c`, `02a`) only fine-tunes
those values; it is not a wide search. Keep `frequency_span_in_mhz` at most
60 MHz. The protected-node fitter will treat the readout
`upconverter_frequency` (IF = 0, equivalently detuning `−IF` from the stored
resonator RF) as a false resonance if that frequency falls inside the sweep,
so never widen a window until it includes the LO.

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
  stay inside the ±400 MHz qubit IF range. For `02x`, `02c`, and `02a`,
  `frequency_span_in_mhz` must stay at most 60 MHz and the IF window must not
  include the readout `upconverter_frequency`.
- If x180 is missing or zero before 03a/04/05, call `jy_request_bootstrap`.
  Bootstrap x180 is `0.5 × max_x180_wf_amplitude`; a missing/zero x90 is set
  to `x180 / 2`. Never overwrite an existing nonzero x180 or x90.

### Multiplex workflows

Phrase-only entry (`進入 JY 量測模式` or `進入 JY 自動量測模式` with no extra
fields) creates a new workflow from `state.json` `active_qubit_names` and
defaults `multiplexed=true`. The server then forces `multiplexed=true` on every
run proposal and refuses attempts to disable it. Set the intended qubits in
`active_qubit_names` before entering; do not ask the operator to type `target`
or `multiplex` in chat. Explicit `targets` or `multiplexed=false` remain
optional overrides for tests and special cases.

For the q3–q8 profile, `active_qubit_names` should be
`["q3", "q4", "q5", "q6", "q7", "q8"]`.

Before creating the workflow or a run proposal, server policy validates target
count, resonator input/output wiring, same-line IF spacing, and aggregate readout
amplitude. Multiplexing applies to the qubits within one node; calibration nodes
still execute one at a time with a separate approval decision and analysis for
each, all displayed on the same session Dashboard.

Minimize the number of hardware runs. The first attempt of every node must
measure all currently-active workflow targets together with one shared parameter
set. Do not start a node by measuring one qubit at a time. If only some qubits
fail, retry those unresolved qubits together with the same modified parameters
and omit already-resolved targets. Split into separate multiplex subgroups only
when those qubits scientifically cannot share the sweep (different frequency or
power window, incompatible statistics wait time, or a 03a stage/LO constraint).
A singleton retry is a last resort after a shared-parameter multiplex retry, not
the default first action. Scheduling already-resolved targets again is a
recoverable planning error: the server rejects the request without hard-stopping
an active autonomy lease.

## Node decisions

### 02x — bare resonator spectroscopy

`02x` only fine-tunes the operator-supplied resonator RF, typically by a few
MHz. Start from `state.json` and the 20 MHz default; do not treat 02x as a
search over tens of MHz. Advance only when every target has a visible resonator
feature, robust SNR of at least 3, and the feature is not at a sweep edge and is
not the readout `upconverter_frequency`. If a feature is clipped, recenter the
stored RF or take a small span increase still at most 60 MHz; never widen until
the LO/upconverter lies inside the window. Increase averages or adjust the step
when the trace is noisy. The protected node records
`extras/bare_resonator_freq`; if that key is absent, initialize it to the
qubit's current resonator RF frequency through a separately approved
state-commit proposal before running 02x. The first 02x run must multiplex every
active target. Passing qubits accumulate; retry only unresolved targets that can
share the same span/averages change. After every active target is resolved,
advance to `02c`.

### 02a — dressed resonator spectroscopy

`02a` runs after `02c`. It is only a simple dressed-frequency scan around the
already tracked resonator, not a power-dependent or wide search. Keep the span
at the 20 MHz default unless a few-MHz recenter is required, and never exceed
60 MHz. Advance when the dressed resonance is resolved for every target, the
feature is not the upconverter, robust SNR is at least 3, and the proposed
frequency change is finite and within IF limits. If a feature is at an edge,
recenter rather than opening a window that includes IF = 0. In bounded autonomy,
the first 02a run must multiplex every active target with the same sweep. Passing
qubits accumulate across runs; retry only unresolved targets that can share the
same span/averages change, and never remeasure an already-resolved target.
After a
passing 02a, advance to `03a`.

### 02c — resonator spectroscopy versus amplitude

`02c` runs immediately after `02x` and before the simple dressed scan `02a`.
Validate the tracked resonator frequency as power increases from low power
(more-negative dBm) to high power. The low power end shows the
`dressed frequency` and the high power end shows the `bare frequency`.

A run passes when those two frequencies can be told apart: they are separated by
at least the configured frequency distance, each is observed on at least
`min_classified_plateau_points` consecutive classifiable power points, and
neither sits within 5% of a frequency-sweep edge. The power segment in between,
where a point is neither clearly dressed nor clearly bare, is discarded rather
than judged. Plateau stability, plateau width, depletion-point count, and
transition monotonicity are reported as advisory notes and no longer reject a
run on their own; re-enable one through its `require_*` rule in `policies.yaml`
if a target needs the stricter gate.

The target readout power is the highest power that is still classified as
dressed, taken from the usable low-power part of the trace. It is deliberately
not pushed to the last point before punch-out. JY's deterministic analysis is
authoritative for the proposed frequency and power; the protected node's
derivative-threshold fit is advisory and must be ignored when it is empty or
disagrees with the dressed boundary.

02c fails only when dressed and bare still cannot be told apart after the
parameters have been driven to their policy limits — the frequency window up to
60 MHz (excluding the readout `upconverter_frequency`) and `num_averages` at
100. Missing, edge-clipped, or inseparable frequencies below those limits mean
widen the window or add averages and repeat. At the limits, treat the target as
manual review or scientifically unmeasurable instead of spending more attempts.
Never schedule `num_power_points` above 30 or `num_averages` above 100.

When an unresolved target's dressed/bare plateau transition is clipped by the
frequency-sweep boundary, spans most of the current sweep, or cannot be fully
contained in the current window, widen `frequency_span_in_mhz` before treating
the target as scientifically unusable or spending repeated attempts at the same
span, but stay at or below 60 MHz and never include the readout
`upconverter_frequency`. Keep qubits together when they can share the same
frequency and power window. Split only the unresolved qubits that actually need
a different window, preserve `multiplexed: true`, and reapply the normal
separability, edge-distance, and state-commit evidence gates to the wider scan.
Do not repeat an already resolved target merely because another target in the
same numeric range needs a wider frequency window.

The first 02c run must multiplex every active workflow target with the same
parameters. An 02c retry may then measure the unresolved subset while preserving
`multiplexed: true`. Group every qubit that needs the same frequency or power
change into one retry; do not walk the chip one isolated qubit at a time.
Do not advance to 02a until every original target has either a valid dressed
point or satisfies the strict bare-only absence rule.

A qubit may be marked `bare_only_absent` and skipped in every later node only
when all safeguards pass: the full power trace is a straight line matching the
previously measured bare frequency, minimum power reaches at least -50 dBm, the
configured frequency span is at most 10 MHz with step at most 0.1 MHz, at least
30 power points are present, the bare resonance remains resolved and away from
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
is at least 5% away from either sweep edge, and robust SNR is at least 6. The
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
SNR is below 6, keep the same LO window and increase `num_averages` through
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

After each target has a credible candidate, lower the drive power and then run a
centered refinement/fine scan. Candidate targets may use an intermediate
refinement stage (for example amplitude factor 0.075) when a direct drop from
the coarse factor to 0.05 makes a previously reproducible peak disappear.
Refinement scans never produce a state patch and must still be followed by the
final low-power scan.

Do not chase ever-narrower frequency windows. A final 03a scan does not need a
40–50 MHz span; about 200 MHz is already fine enough once the LO/IF is centered
on a credible candidate. Prefer keeping the span near that scale and fixing
linewidth or morphology with drive amplitude instead. A final 03a result
requires a span no greater than 200 MHz, an operation amplitude factor no
greater than 0.05, a stable finite fit, robust SNR of at least 6, and a feature
at least 5% away from either sweep edge. Only this fine-scan evidence may
update idle frequency or resolve the target.

The final feature FWHM must also lie between 0.5 and 16 MHz. A still-broader
feature is power-broadened and must not resolve the target even when fit and SNR
pass; reduce `operation_amplitude_factor` and repeat at about the same ~200 MHz
window. If many peaks appear in one window (noisy multiplet or power-split
structure), lower the amplitude first rather than shrinking the span; weaker
drive usually collapses the clutter onto the fundamental. If the feature becomes
narrower than 0.5 MHz, slightly raise amplitude or increase resolution before
accepting it. Only commit `idle_freq` after the measured FWHM falls inside this
interval.

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
rules. After the all-target coarse scan, retry unresolved qubits that can share
the same span, amplitude, and averages together; isolate a qubit only when its
LO/stage constraint differs. Do not advance to 04 until every active target has
final fine-scan evidence.

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
targets while preserving `multiplexed: true`. The first 04 run must multiplex
every active target with the same sweep. Passing qubits accumulate across
runs when the Pi amplitude is finite and nonzero, robust SNR is at least 14, and
the feature is at least 5% away from either sweep edge. Retry unresolved qubits
that need the same averaging or amplitude-range change together. Do not advance
to 05 until every active target is resolved. This SNR
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
`multiplexed: true`. The first 05 run must multiplex every active target with
the same wait window and averages. If a later multiplexed protected fit aborts
the snapshot, retry the remaining unresolved qubits together before isolating a
single qubit. Passing T1 evidence accumulates by qubit; retry only unresolved
targets that can share the wait range. Because this workflow includes 100-run
statistics,
final 05 evidence must cover at least 3.5 fitted lifetimes so that the trace has
reached its equilibrium tail. Use the emitted
`recommended_statistics_max_wait_time_in_ns` (4.5x the fitted lifetime, rounded
to a 4 ns boundary) when forming compatible 05st target subgroups. It must also
have robust SNR of at least 25, anchored to the accepted q3/q4/q5 reference run
(minimum 25.06). If only some traces are below that floor, call
`jy_recommend_0405_snr_retry` and repeat only those targets through 2000, 4000,
then 8000 averages. Reaching the averaging ceiling without the SNR floor leaves
the target unresolved; it does not convert a noisy fit into a completed result.

### 07d and 07b run once on defaults

Operator instruction, 2026-09-21. 07d Readout Frequency/Duration/Power
Optimization is part of the sequence and runs immediately before 07b, so the
readout it optimises is the one 07b then characterises. Both nodes carry
`single_pass: true` in `rules/policies.yaml`.

A single-pass node runs exactly once, full width, on the node defaults, and the
workflow advances whatever the analysis says. Do not retry it, do not adjust its
parameters, and do not chase individual targets through it — a second run is
refused before the worker reaches hardware. Evidence gating is unchanged:
a target whose analysis passes still commits its state patch, because that is
the node's output, and a target that does not pass simply carries no update and
does not hold the workflow up.

This replaces the tail-power ladder below as the normal path for 07b. Keep the
ladder documented: it is still the right tool if an operator deliberately
reopens 07b, and it records what the morphology rule is protecting against.

07b has one exception to the single run. Any qubit whose thermal result is
**above** `analysis."07b".active_reset_trigger_fidelity` (0.80) earns exactly
one more 07b run, with `reset_type_thermal_or_active: "active"`. Run those
qubits together; that repeat is their accepted 07b result and it qualifies
them to use active reset in later nodes, while every other qubit stays
thermal. A second thermal run is still refused, and so is a second active run.
`jy_get_next_action` proposes the repeat itself, with the qualifying target
list, before it reports `advance`.

Because 07b no longer gates on morphology, morphology no longer gates
active-reset qualification either: `active_reset_requires_morphology` is
false, so an active run qualifies on fidelity alone. The floor and the trigger
are the same number, 0.80, deliberately - one threshold decides both. A
thermal run never qualifies a qubit for active reset, whatever its fidelity.

The statistics rule is unchanged: before any 05st or 06st run uses active
reset, 05, 06 and 06b must be repeated and accepted with active reset for
those qubits.

### 07b — IQ blobs

Require a saved discriminator plot and a finite per-qubit readout fidelity of at
least `0.70`, but fidelity alone is not sufficient. Each prepared-state distribution
must look like a compact, approximately round IQ cloud. Some overlap between the
two round clouds is acceptable; a long line/tail, crescent, or strongly elongated
cloud is a power-saturation signature and must fail even when fitted fidelity is
above 0.5. JY enforces covariance-axis and radial-tail ratios from `ds.h5`, and
suppresses the discriminator state patch of every qubit whose morphology fails.

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
to fractions (`0.886`) before acceptance or state commit. Retry unresolved
qubits that can share the same readout-amplitude recovery together; do not
walk failed 07b targets one isolated qubit at a time.

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
mandatory because both values feed downstream 100-run statistics. The first 06
and 06b runs must multiplex every active target with the same wait window;
retry unresolved qubits together when they can share that window. Use the
emitted 4.5x recommended statistics wait time when forming compatible target
subgroups. The Ramsey frequency correction and T2 state values remain separately
committed.

### 10a — single-qubit randomized benchmarking

Require a successful fit and finite EPC/EPG values in [0, 1] for every resolved
qubit. The first 10a run must multiplex every active target with the same depth
settings. The depth must be divisible by `delta_clifford`, and later retries may
use a shared-parameter unresolved subgroup.

### 05st / 06st_t2star / 06st_t2e — 100-run statistics

Each statistics node requires exactly `histo_num=100`. It may start only after the
05, 06, and 06b results are all resolved with the same reset type. Each baseline
fit must cover at least 3.5 lifetimes. For the statistic being measured, the
requested `max_wait_time_in_ns` must lie between 3.5 and 5.5 times its accepted
T1/T2 value. This ensures that the baseline has reached equilibrium and the
statistics range is approximately four lifetimes; prefer 4.5 times the baseline.
Start from the largest compatible multiplex group that can share one
`max_wait_time_in_ns`; do not begin with isolated one-qubit statistics runs.

Once those prerequisites pass, run each 05st/06st node once. JY does not refit all
100 raw iterations, does not impose a 100/100 secondary fitting gate, and does not
reject the entire statistics run because one independently re-analysed iteration
would fail. The protected node's summaries and plots remain informational; normal
worker completion, saved plot, exactly `histo_num=100`, and the already-validated
baseline prerequisites are sufficient to complete that statistics node. Any state
candidate produced by the registered statistics node remains subject to the usual
state-patch policy and commit authorization, but no extra statistics-specific
fitting check is added.

## A `needs_review` run still commits the qubits that passed

Operator instruction, 2026-09-20. `analysis_status` is a single run-level
verdict, but every acceptance check is per qubit. A run that is `needs_review`
because one target failed must still commit the targets that passed: the node
refuses a further run once every target is resolved, so suppressing the whole
patch lost calibrated values for good. This happened three times in workflow
`0892399eba47422b96c9203e8ead1523` — 02c committed none of nine passing
targets, 03a none of seven, and 04's `q1`/`q2` were never recovered.

The analyzer therefore reports, alongside `failure_reasons`:

- `failure_reasons_by_target` — each failure attributed to the qubit it names.
- `run_level_failure_reasons` — failures that belong to the run as a whole
  (no saved plot, unreadable dataset, invalid recorded state updates). Any of
  these still suppresses every target.
- `passing_targets` — the qubits with no failure of their own in a run that has
  no run-level failure. `candidate_state_patch` is filtered to these qubits.
- `suppressed_patch_targets` — the qubits whose changes were dropped, and why.

`jy_autonomy_commit_state` accepts a `needs_review` run only for
`passing_targets`, and refuses any other target without halting the lease. Read
`passing_targets` before proposing the commit, and say in the Reason which
targets it covers and which are still unresolved. Only passing evidence is ever
committed; this relaxes nothing else.

## Status meanings

- `run.status`: worker lifecycle (`starting`, `running`, `stopping`, `completed`,
  `failed`, `stopped`, `cancelled_by_shutdown`, `force_stopped`).
- `analysis_status`: deterministic evidence result (`pass`, `needs_review`,
  `failed`). It is run-level; `passing_targets` carries the per-qubit verdict.
- `workflow.status`: bring-up progress (`active`, `completed`, `stopped`,
  `recovery_required`).
- `proposal.status`: approval lifecycle (`pending`, `approved`, `expired`,
  `consumed`).

Do not substitute one status for another.

## Instrument-connectivity failures never engage the hardware lock

Operator instruction, 2026-09-19. A recognized instrument-connectivity failure
(`failure_category: instrument_unreachable`) pauses the run and scheduling, but
it must never retain the hardware lock and must never put JY into recovery-only
quarantine — not even when the worker could not confirm hardware cleanup
because the instrument was already unreachable. The worker still makes its
best-effort `_stop_active_node_with_evidence()` attempt and still refuses to
release the lock if `state.json` changed during the failed run; only the
unverifiable-cleanup condition is waived.

The operator's reasoning: when the cause is simply that the hardware is
unreachable, the lock adds an extra manual unlock step at the local console
without protecting anything — no job can be running on an instrument that
cannot be reached. Recovery must stay a plain `恢復量測` /
`jy_resume_measurement_mode` on the same paused lease.

This does not relax the other lock rules. A retained lock from any other cause
is still preserved for local recovery, is never deleted automatically, and
still requires the operator console.

## A run crash is classified: known ones continue, unknown ones pause

Operator instruction, 2026-09-20, replacing the 2026-09-19 rule that every
worker exception stopped for the operator. In automatic mode the lease must
survive everything that can be corrected, so a crash is now classified:

- **Registered node-parameter rejections continue.** A crash whose signature is
  listed in `autonomy.recoverable_worker_exceptions` is a parameter the
  protected node itself rejects: deterministic, reproducible, and fixed by a
  different value. The lease stays active, the server records
  `autonomy_recoverable_worker_exception` with the registered remedy, and you
  choose a corrected parameter. Never resend the value that crashed.
- **Everything else pauses.** An unrecognized worker exception pauses the lease
  and scheduling, exactly like an instrument outage. Report to the operator
  what crashed and why, and resume the same lease with `恢復量測` once they
  decide. No new approval is needed, and the lease is not lost.

Both currently registered signatures are also prevented before they run:
fractional `min_power_dbm`/`max_power_dbm` in 02c is refused because the node
types them as `int`, and `arbitrary_qubit_frequency_in_ghz` in 03a is refused
while `freq_vs_flux_01_quad_term` is zero. Both were crashes in workflow
`0892399eba47422b96c9203e8ead1523` and its predecessor.

Unchanged: record the failure as a labeled lesson in
`rules/experiences/<node>.md`, state plainly that no snapshot or plot exists,
and never retry the exact parameter that crashed. Add a new entry to
`recoverable_worker_exceptions` only together with its lesson.

## A refusal before the hardware never ends the lease

Operator instruction, 2026-09-20. A request that is rejected before the worker
reaches the instrument -- an out-of-policy parameter, an out-of-scope target
set, a state patch that fails validation, a premature `jy_analyze_run` -- does
not end the authorization. Nothing ran, so refusing the action is the whole
protection; ending the lease on top of that only costs a fresh human approval
and stops a bring-up that could have continued. Every refusal is recorded as
`autonomy_action_refused` and stays visible on the Dashboard.

Correct the request and continue. The conditions that still halt the lease are
only the ones where JY can no longer account for the hardware or the state
file: `snapshot_missing`, `state_hash_conflict`, and `hardware_lock_anomaly`.

The earlier trap where a refused `jy_autonomy_start_run` cost the lease is
gone, but still check `current_node` before requesting the first run of a node:
a refused request is a wasted round trip even when it is free.

## Ask the server what to do next

`jy_get_next_action(workflow_id)` returns the deterministic plan: the current
node, the active/resolved/incomplete/unresolved target sets, attempts used and
left per target, any required deterministic setup tool, and one of `wait`,
`analyze`, `record_decision`, `setup`, `run`, `advance`, or `blocked`. For a
`run` it gives the qubits and only the parameters that differ from the node
defaults, plus the rule it applied.

It is an aggregator over the same state the guards enforce, not a second
opinion. When `notes` says no registered deterministic rule covers the
situation, that is the honest answer: read the node's section above and
`rules/experiences/<node>.md` and choose the parameters yourself. Use
`jy_get_decision_experience` for what was tried before; each entry carries an
`evidence` digest of the gated numbers, and `include_analysis=True` only when
the digest is genuinely not enough.

## 04 never multiplexes more than five targets

Operator instruction, 2026-09-21. Power Rabi caps its multiplex group at five
qubits, whatever the active set is, via `nodes."04".max_multiplex_targets: 5`
in `rules/policies.yaml`. This is the one node with a cap; every other node
still multiplexes all active targets and the rule below applies to them
unchanged.

For a capped node the cap replaces the shared-first-batch requirement: the
first 04 run is the first group of five, not every active target, and the node
is completed by repeating it for the remaining groups. A request above the cap
is refused before the worker reaches hardware, which is a correctable planning
error and does not halt the lease.

Evidence, `as-qpu-10qV2_agent`: a ten-qubit 04 program has never once been
accepted by this QOP. Workflow `2dba0368b00c4b06aeed1a1ceabbb3d9` spent seven
runs walking 10 -> 5 -> 3 -> 2 -> 1 twice, either side of a genuine QOP outage
and restart, without a single successful submission. Starting at five skips the
first rung of that ladder every time the node begins.

## Halve the multiplex group when a run times out on submission

Operator instruction, 2026-09-20, replacing the earlier "three or four qubits"
wording. Except at a capped node, every node starts the same way: the first run
of a new node always multiplexes ALL active targets with one shared parameter
set.

If that run dies with `QMTimeoutError: A timeout of 100 seconds was reached`
raised from `qm.execute()` / `add_to_queue`, assume the program carries too
many qubits and halve the group. Measure the targets in two halves; if a half
still times out, halve again, and keep halving.

If a run carrying a SINGLE qubit still times out on submission, the cause is no
longer program size. Treat it as a genuine instrument-side disconnection, stop
scheduling, and hand it to the operator.

So the ladder for ten active targets is 10 -> 5 -> 3/2 -> 1, and only a
one-qubit timeout means the instrument itself is at fault. Keep one shared
parameter set across the halves so their evidence stays comparable, and never
narrow the sweep instead of the group: coarsening the swept axis does not fix a
submission timeout.

`service.py` waives the shared-first-batch rule once a full-width attempt has
failed with `program_submission_timeout` or, for runs recorded before that
category existed, `instrument_unreachable`. That waiver is what lets the halves
be scheduled.

### Which timeout is this? Three causes, one of them halves

A `QMTimeoutError` alone does not say whether the program was too large, the
QOP is wedged, or the network is down, so the worker decides with evidence
rather than with the exception type. Do not re-derive this by hand: read
`failure_category` and `instrument_probe` in the run's analysis.

1. **Program too large** — `failure_category: program_submission_timeout`.
   Requires all of: the deadline expired in a `qm` package frame that submits a
   program (`execute` / `add_to_queue` / `_add_program` / `compile`), the gRPC
   status was `DEADLINE_EXCEEDED` rather than a transport failure, and a live
   probe right after the failure got the QOP to answer a health check. Only
   this case halves, and only while more than one target was attempted. The
   lease stays active and no operator is needed.
2. **Instrument wedged or down** — `failure_category: instrument_unreachable`
   with `instrument_probe.cause: "instrument"`. The socket was refused, or it
   opened but the QOP never completed a health check. Scheduling pauses and the
   operator restarts or inspects the QOP.
3. **Network fault** — `failure_category: instrument_unreachable` with
   `instrument_probe.cause: "network"`. Name resolution failed, the host was
   unroutable, or nothing answered at the socket level. Scheduling pauses and
   the operator checks the lab network.

A single-target submission timeout whose probe still succeeds is out of halving
room: the worker pauses it with `measurement_paused: true` and the operator
message names the QOP job queue, not the network. Never halve in cases 2 and 3
— there is nothing to shrink.

Evidence behind the rule, node 04 on `as-qpu-10qV2_agent`: ten multiplexed
targets failed at 171, 86 and 18 amplitude points (runs `cb707ffb…`,
`9a09832b…`, `db832677…`), all with the identical 100 s message, so the
eighteen-point program -- a tenth the size of the first -- bought nothing. The
first four-qubit subgroup then submitted and completed in 58 seconds.

The 100 s itself belongs to the `QuantumMachinesManager` created inside
`machine.connect()` ([quam_root.py:215]); `qm_session(timeout=...)` and the
node's `timeout` parameter do not affect it.

## Record `advance`, not `manual_review`, on the run that finishes a node

Lesson, 2026-09-20, workflow `0892399eba47422b96c9203e8ead1523` at node 04.

A run carries exactly one decision, and `current_node` only moves when a
decision names the next node. If the last run of a node is given
`manual_review` with `next_node` still pointing at the current node, and every
other run of that node already has a decision, the workflow cannot leave the
node: `jy_autonomy_start_run` refuses a fresh run with "All active <node>
targets are already resolved or incomplete", and `jy_record_decision` refuses a
second decision on the same run.

So when a target hits its scientific boundary and that closes the node, the
order is:

1. `jy_mark_scientifically_unmeasurable` for that target, then
2. `jy_record_decision` with `advance` and the real `next_node`, whose Reason
   states the target is incomplete at the boundary.

Never `manual_review` on the run that completes a node.

If the dead-end has already happened, an `advance` decision can be recorded
against an earlier run of the same node that never received one -- a failed run
qualifies, since `jy_record_decision` accepts `analysis_status: failed`. State
plainly in the Reason that the decision is node-level and that this particular
run produced no snapshot.

Related trap: a refused `jy_autonomy_start_run` is itself a lease stop
condition ("Authorized run was refused or failed to launch: ..."). Check
`current_node` before requesting the first run of a new node; a request sent
too early costs the lease and a fresh operator approval.
