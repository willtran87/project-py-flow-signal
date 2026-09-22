# Objective-focused improvement backlog

Status: implementation and validation recorded in [operation outcomes and ownership](OUTCOME_ANALYSIS.md), 2026-09-22. Original requirements below are preserved; pending external evidence is not counted as completed.

| Item | Status |
| --- | --- |
| FS-101 / FS-102 | Implemented: scoped outcomes, explicit business contracts, deterministic signal/severity decisions |
| FS-103 / FS-104 | Implemented: bounded call-site contexts and declared deferred registrations |
| FS-105 / FS-106 | Implemented for the documented bounded retry/async subsets; complex control and external ownership stay uncertain |
| FS-107 | Tooling, frozen rubric, and 30 scopes across three pinned packages prepared; independent human labels/adjudication pending |
| FS-108 | Implemented: source-linked uncertainty and actions in the inspector and exported facts |
| FS-109 | Implemented: phase/peak-memory measurement, repeated cache equivalence, source-read reuse, retention; measured targets and limitations are published in the current guide |

Objective: statically trace possible execution paths through general Python codebases, identify failure-sensitive points, and recommend appropriate instrumentation with explicit evidence and uncertainty. The scanner must work without an LLM or importing/executing the scanned application. Runtime collection remains a separate, deliberate action.

The pre-implementation assessment was B- against that objective and C+ for demonstrated enterprise readiness. These are engineering judgments, not accuracy measurements. Evidence at that assessment included 14 correct and 11 missed call pairs in 12 fresh external cases, no independently adjudicated instrumentation labels, and a 1,000-file changed-source cached scan slower than a fresh scan. See [assessment evidence](WORKFLOW_ACCURACY.md); [current validation](OUTCOME_ANALYSIS.md) records this increment.

## Work item index

P1 items address correctness, recommendation quality, or evidence needed to trust those recommendations. P2 items improve breadth, review efficiency, and scale. Size is relative implementation complexity (M = contained subsystem change; L = cross-subsystem change), not a delivery estimate. The implementation status above supersedes the original planning state; FS-107's human adjudication requires reviewers and source access.

| ID | Priority | Work item | Size | Dependencies |
| --- | --- | --- | --- | --- |
| FS-101 | P1 | Represent operation outcomes and declared business context | L | None |
| FS-102 | P1 | Generate severity and signal choices from outcome evidence | L | FS-101 |
| FS-103 | P1 | Propagate callable arguments through ordinary Python helpers | L | None |
| FS-104 | P2 | Support explicit callback-registration contracts | M | FS-103 |
| FS-105 | P1 | Identify reporting ownership across retries and fallback | L | FS-101, FS-102 |
| FS-106 | P1 | Track bounded async task ownership and failure observation | L | FS-101, FS-102 |
| FS-107 | P1 | Independently evaluate recommendation accuracy | L | Start now; evaluate each analysis increment |
| FS-108 | P2 | Explain uncertainty at the affected path and recommendation | M | FS-101, FS-103; extend for FS-104/105/106 |
| FS-109 | P2 | Validate representative scale and reduce changed-scan overhead | L | None; final measurements after analysis changes |

## FS-101 — Operation outcomes and declared business context

**Problem:** `except: return fallback` establishes recovery-shaped control flow, but does not establish whether the operation succeeded, degraded, or failed. A network call alone does not establish business criticality.

**Deliverable:** Add a bounded outcome record to the analysis/report model, and optional validated operation contracts in configuration. Separate syntactic facts from user-declared meanings. Suggested outcome vocabulary: expected rejection, successful recovery, degraded recovery, failed operation, cancellation, and unknown. Keep syntactic return/raise/fallthrough facts independently available. Allow an operation owner and declared importance without deriving importance from the dependency type.

**Acceptance criteria:**

- Every classified outcome names its operation, source location/path, evidence basis, and unresolved assumptions.
- A constant return or swallowed exception alone remains semantically unknown; it is not automatically called successful recovery.
- Configured meanings are visibly labeled declarations, scoped to matching operations; conflicting or malformed declarations produce actionable diagnostics and do not silently win.
- Mixed returns/raises and bounded-path truncation retain multiple possible outcomes or unknown status.
- Existing configurations remain valid, and results are deterministic without importing the target.

**Validation:** At least 12 authored cases covering identical syntax with different declared business meanings, mixed branches, unknown context, nested operations, and conflicting contracts. Round-trip new records through JSON and show the evidence in HTML; preserve existing report consumers or document/version any incompatible change.

**Primary code:** `src/flowsignal/model.py`, `config.py`, `scanner.py`, `reporting_paths.py`, `rules.py`.

## FS-102 — Outcome-aware severity and signal selection

**Problem:** Conditional ERROR advice is helpful, but currently leaves much of the decision about recovery, expected failures, and logging noise to the reader.

**Deliverable:** A deterministic decision table consuming FS-101 evidence and recognized reporting ownership. Output a preferred instrumentation action when evidence supports it; otherwise retain explicit conditional alternatives. Include log, trace, metric, enrich-existing, and no-additional-log decisions. Keep logging severity, review priority, and confidence separate.

**Acceptance criteria:**

- A declared successful recovery does not produce an unconditional ERROR recommendation for the recovered attempt.
- A declared degraded outcome can recommend WARNING; an established failed required operation can recommend ERROR at its owner, subject to existing reporting evidence.
- Important declared successful lifecycle events may recommend INFO. Routine success does not automatically generate lifecycle logs.
- Expected rejection and normal cancellation are not automatically ERROR. Cancellation that violates a declared required outcome remains reviewable.
- Existing equivalent reporting can yield enrich-existing or no-additional-log advice, with the source evidence that supports it.
- Every decision records the outcome, triggering condition, reporting owner or ownership uncertainty, and reason for the signal/level choice.
- Unknown outcomes retain conditional advice rather than invented business semantics. Explicit declaration of outcome does not prove log delivery.

**Validation:** A table-driven matrix covering all six outcome classes, reported/unreported variants, and positive and negative cases for INFO/WARNING/ERROR. Add tests for metrics/traces/no-log choices and duplicate-log avoidance. Compare resulting suggestions with independent labels under FS-107, separately from development tests.

**Primary code:** `src/flowsignal/rules.py`, `model.py`, `config.py`, `sarif.py`, `templates/report.html`.

## FS-103 — Callable arguments through ordinary helpers

**Problem:** `invoke(callback)` followed by `callback()` can remain unresolved even when an indexed caller passes one known function. This breaks entrypoint-to-failure paths in ordinary Python workflows.

**Deliverable:** Bounded, call-site-aware propagation of known callable arguments through positional/keyword parameters, defaults, and simple helper forwarding. Reuse existing callable-value inference. Represent ambiguous candidates explicitly if the public model needs more than one target; never select an arbitrary candidate.

**Acceptance criteria:**

- Resolve a known function and a bound method passed through one or two indexed helper calls, including cross-module calls and keyword binding.
- Keep different call sites' callback bindings distinct; two callers using different callbacks must not become one definitive edge.
- Unknown external callers, conflicting rebinding, incompatible argument binding, and unsupported star expansion remain explicit uncertainty.
- Resolved candidates retain their binding chain as evidence. Candidate-only edges cannot alone establish universal reporting coverage.
- Recursive helper/callback cycles terminate within a configured work budget, with incomplete/uncertain status exposed appropriately.
- Scanning a fixture with import-time side effects never runs those effects.

**Validation:** At least 20 development cases with ten negative/ambiguity controls. If any current evaluation cases guide implementation, move those cases to development before tuning and add a fresh, pinned evaluation cohort. Retain all missing expected pairs in evaluation denominators; do not rewrite labels to match output.

**Primary code:** `src/flowsignal/receivers.py`, `scanner.py`, `model.py`, `uncertainty.py`; `scripts/benchmark_accuracy.py`.

## FS-104 — Explicit callback-registration contracts

**Problem:** An application can register a function with a dispatcher without directly calling it in source. Framework-independent scanning needs a way to describe that boundary.

**Deliverable:** Optional declarative contracts identifying a registration callable, the callback argument position/name, and deferred execution semantics. Begin with direct known callback values; avoid a framework runtime dependency.

**Acceptance criteria:**

- Configured `registry.register(handler)` and keyword equivalents produce labeled registration/possible-execution relationships.
- Registration is visibly distinct from an immediate call, and does not inherit the registration site's surrounding exception handler as execution coverage.
- An unrelated unconfigured `.register()` does not gain an edge merely because its method name matches.
- Shadowed names, unknown callbacks, conflicting contracts, and unsupported argument forms remain unresolved with reasons.
- Contracts work on plain Python fixtures without importing LangChain, LangGraph, or any dispatcher.

**Validation:** At least eight positive/negative contract cases; JSON, Mermaid, and HTML distinguish declared deferred edges. Add one multi-file registry workflow whose actual execution is checked only through a separately invoked authored runtime probe.

**Primary code:** `src/flowsignal/config.py`, `scanner.py`, `diagram.py`, `templates/report.html`.

## FS-105 — Retry and fallback reporting ownership

**Problem:** An attempt can fail while its operation eventually succeeds. Logging every attempt at ERROR is noisy; suppressing the final exhausted outcome can hide the real failure.

**Deliverable:** Bounded analysis of a documented subset of explicit retry loops, fallback branches, and final exits. Track attempt-level events separately from operation-level reporting owners. Custom retry wrappers can use declared contracts; unknown loops stay unknown.

**Acceptance criteria:**

- A failed attempt followed by successful recovery does not receive definitive final-failure advice.
- Exhaustion that ends a required operation identifies one candidate final reporting owner and its source path.
- Degraded fallback uses FS-101/102 outcome evidence rather than treating every fallback as either success or ERROR.
- Duplicate-log advice accounts for supported conditional branch reporting, not only unconditional ERROR statements.
- Ambiguous loop exits, exception replacement, and unknown wrapper semantics preserve uncertainty and cannot establish complete coverage.
- Existing reporting is recognized without automatically suppressing findings for distinct operation failures that happen to share an exception.

**Validation:** At least 12 scenarios: first-attempt success, retry success, exhaustion, break/continue, timeout-shaped exceptions, degraded fallback, final rethrow, and nested owners. Compare static advice with authored execution traces/log capture; only those deliberate validation probes execute code.

**Primary code:** `src/flowsignal/reporting_paths.py`, `scanner.py`, `rules.py`, `model.py`.

## FS-106 — Async task ownership and failure observation

**Problem:** Retaining a task handle does not establish that its failure is observed. Current discarded-handle detection covers only part of that problem.

**Deliverable:** Track a bounded subset of task creation, local handle aliases, awaiting, returning/transferring handles, and supported aggregate observation. Cover direct awaits, `TaskGroup`, and `gather` variants explicitly; retain escape uncertainty for complex containers or external ownership.

**Acceptance criteria:**

- Distinguish discarded, retained-but-unobserved, awaited, transferred, and uncertain ownership states in supported cases.
- Creation-time handlers are not credited with reporting exceptions raised later by task execution.
- `gather(return_exceptions=True)` alone does not establish that returned exceptions are inspected or reported.
- An observed child failure can identify the enclosing operation's reporting owner; mere `await` establishes observation/propagation, not logging delivery.
- Cancellation advice follows outcome evidence; cancellation is not automatically an error or automatically harmless.
- Deferred coroutine creation and execution remain distinct in exported diagrams and facts.

**Validation:** At least 12 async fixtures covering observed and unobserved failures, handle aliases, returned tasks, exception collection, task groups, cancellation, and unknown escapes. Use explicit deterministic async probes without network calls or timing-dependent sleeps.

**Primary code:** `src/flowsignal/scanner.py`, `rules.py`, `model.py`, `diagram.py`; async regression tests.

## FS-107 — Independent recommendation accuracy

**Problem:** Current tests validate implemented behavior, but there is no independent estimate of whether instrumentation recommendations are useful or complete.

**Deliverable:** Extend the existing source-only review packet/import workflow to capture outcome, instrumentation choice, severity, reporting owner, and acceptable alternatives. Assemble a documented sample of at least 30 workflow scenarios across at least three repositories, including plain services, batch/background work, and callback-heavy workflows. Use lawfully available sources with pinned revisions; application code remains unexecuted during scoring.

**Acceptance criteria:**

- Reviewers label complete scoped workflows, including expected instrumentation points and explicit no-additional-signal cases; they do not only assess scanner-selected findings.
- At least two reviewers independently label a shared subset of ten scenarios. Record disagreements and adjudication rather than averaging incompatible labels away.
- Record reviewer identity, revision/source hashes, scope, configuration, provenance, and whether a case was used for development. Reviewer declarations are not presented as authenticated identity.
- Report finding precision/recall by rule and workflow type, plus signal/level and owner agreement on applicable matched recommendations. Publish denominators, raw disagreements, abstentions, and uncertainty intervals where sample sizes permit.
- Unlabeled, stale, or incomplete cases remain unscored. Conditional alternatives receive a prespecified scoring policy; emitting every possible severity cannot count as perfect agreement.
- Freeze the evaluation rubric and cohort before measuring an implementation change. Tuning cases move to development and are replaced with fresh evaluation cases.
- Publish both raw results and remaining misses; no claim of enterprise-wide accuracy from this small cohort.

**Validation:** Importer tests for stale hashes, incomplete reviews, invalid alternatives, duplicate labels, and disagreement records. Reproducible scoring with no fixture execution. Human adjudication is a real external dependency; building packet tooling alone does not finish this item.

**Primary code/artifacts:** `scripts/benchmark_accuracy.py`, `benchmarks/manifest.json`, review packets, benchmark documentation.

## FS-108 — Actionable path uncertainty

**Problem:** A list of unresolved calls does not clearly tell a reviewer which conclusion depends on a gap or what evidence would resolve it.

**Deliverable:** Connect resolution/coverage limitations to affected operation paths and recommendations. Add an uncertainty view to the existing diagram and review queue, reusing source links and bounded context rather than introducing a separate dashboard.

**Acceptance criteria:**

- Selecting an affected recommendation shows the supported path, the first gap on that displayed path, and the relevant uncertainty reason; multiple possible paths retain their own gaps.
- Each supported reason offers a concrete action: configure a boundary/reporter/registration contract, declare an operation outcome, inspect a specific source site, or collect a selected runtime observation.
- Distinguish unsupported semantics, analysis-budget truncation, user declarations, and missing runtime observations.
- Runtime corroboration applies only to matching observed pairs and source hashes; it cannot silently resolve unobserved alternatives or prove reporting delivery.
- Empty caller context is not described as unreachable, and confidence labels are not presented as calibrated probabilities.
- JSON and SARIF retain the same uncertainty relationships as HTML; sensitive source omission remains effective.

**Validation:** Browser interaction and geometry checks at 2560x1600, 1440x1000, and 390x844, including keyboard navigation, long reasons, cyclic paths, partial scans, and no remote requests. Verify path/recommendation associations against report facts.

**Primary code:** `src/flowsignal/uncertainty.py`, `review_queue.py`, `diagram.py`, `templates/report.html`, `sarif.py`.

## FS-109 — Representative scale and changed-scan cost

**Problem:** Reusing parsed files does not currently make changed-source scans faster. Small generated modules also do not establish realistic enterprise memory or graph behavior.

**Deliverable:** Add reproducible per-stage timing and peak-memory measurement for at least three pinned representative repositories plus synthetic 1,000- and 10,000-file workloads. Profile discovery, parsing/cache decoding, resolution, coverage, serialization, and rendering before selecting optimizations. Add bounded cache retention. Evaluate dependency-directed invalidation only where its correctness can be demonstrated.

**Acceptance criteria:**

- Report at least five isolated repetitions per workload, median/range, environment, configuration, source counts, peak memory, scan completion, and findings. Fixture creation is excluded from scan timing.
- Compare fresh, cold, unchanged, leaf-edit, shared-helper-edit, configuration-change, and add/delete-file scenarios. Any capacity increase is explicit; default-budget incomplete results remain visible.
- Cached and fresh reports are semantically equal after removing only documented cache/timing/resource counters; findings, evidence, inventories, uncertainty, and scope must agree.
- Exercise dense/cyclic graphs and long files as well as many small files. Budget/worker failures preserve prior output and never appear as clean scans.
- Target median unchanged reuse at or below 70% of fresh time and changed scans at or below 110% of fresh time on the selected 1,000-file workload. These are proposed engineering targets, not current claims. If a target is missed, publish the result and keep reuse opt-in.
- Cache retention bounds disk growth and never follows links or deletes outside the configured cache directory. Concurrent readers/writers retain atomic publication and safe fallback.
- Do not claim selective incremental analysis unless tests prove transitive dependency, caller-coverage, entrypoint, and configuration invalidation.

**Validation:** Extend `scripts/benchmark_incremental.py`; compare installed-wheel and source-checkout behavior, collect results on Windows and Linux, and retain a smaller semantic-equivalence workload in CI. Avoid fragile wall-clock pass/fail assertions on shared CI hosts.

**Primary code:** `src/flowsignal/cache.py`, `scanner.py`, `receivers.py`, `rules.py`; benchmark scripts and CI.

## Execution order and completion policy

1. Start FS-107 corpus/reviewer preparation and FS-109 measurement setup. These provide evidence before further tuning.
2. Implement FS-101 then FS-102. FS-103 can proceed independently of those changes.
3. Implement FS-105 and FS-106 using the shared outcome model; add FS-104 after callable propagation.
4. Complete FS-108 against the new evidence records, finish measured FS-109 optimizations, and evaluate the resulting version under FS-107.

For every item: update user-facing documentation and limitations, preserve deterministic offline scanning, add meaningful positive/negative regressions, run the affected integration/export checks, and record results. Do not mark a correctness item done because a UI control or data field exists without analysis behavior behind it. Human-review and cross-platform evidence stay pending until actually obtained.

LLM enrichment, automatic source edits, broad framework-specific integrations, and a hosted dashboard are outside this backlog. The work is focused on the existing product objective.
