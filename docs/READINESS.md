# Enterprise readiness assessment

Current update: [accuracy and review](ACCURACY_AND_REVIEW.md) adds all six P1/P2 items. The small independent call benchmark reports 3 correct pairs and 6 misses; it supports regression checks, not an enterprise accuracy claim. All four previously indexed self-dogfood omissions are now required passing checks.

Earlier update: [supervision and uncertainty](SUPERVISION_AND_UNCERTAINTY.md) adds opt-in Windows/Linux worker limits for scan/render work and actionable unresolved-call context. Earlier workload measurements below remain historical.

The newer [typed-flow and coverage increment](TYPED_FLOW_AND_COVERAGE.md) adds receiver/helper-return inference, source-linked coverage explanations and updated validation. Measurements below remain historical evidence for their stated revision.

The latest [review-workflow increment](REVIEW_WORKFLOW.md) adds explicit reporting-owner contracts, supported property-getter edges, structural baselines and reasoned dismissals. Both original property omissions are now regression checks. The workload and test counts below are historical evidence for earlier revisions; current machine-readable validation should accompany deployment decisions.

FlowSignal is suitable for a **supervised pilot and advisory instrumentation review**. It is not yet a validated whole-program analyzer or a release gate. Its general Python core requires no LangChain, LangGraph, or other workflow framework, and never imports or executes the scanned application. Configuration extends recognition of application APIs and logging wrappers.

## Soft spots addressed in this hardening pass

| Problem | Change and practical effect |
| --- | --- |
| Closure, class, and comprehension shadowing could attribute a call to the wrong imported API. | Lexical bindings preserve enclosing function scope, distinguish duplicate enclosing definitions, and isolate comprehension and class names. Negative controls cover false boundary attribution. |
| Client helpers such as request preparation could be classified as network activity. | Known client method lists distinguish request execution from preparation. Unknown APIs remain unresolved or need explicit configuration. |
| A trace scope could hide a finding even when exception recording was disabled or the exception was swallowed. | Trace presence and potential failure recording are separate facts. Disabled or unknown recording options, deferred execution, and unreported consumption do not establish failure coverage. Exporter delivery is still unverified. |
| Arbitrary `something.ERROR` values could be interpreted as standard log levels. | Generic `log()` calls require recognized constants or numeric levels, including keyword arguments; unknown levels emit a diagnostic. |
| Large inputs and dense caller graphs lacked aggregate work limits. | Source-byte, AST-node/depth, and graph-work budgets plus cached caller analysis bound more of the scan. A dense cyclic fixture verifies that work exhaustion retains findings and marks the scan incomplete. |
| A file could be replaced between discovery and reading. | Bounded regular-file reads check identity before, during, and after reading, reject final-path links/reparse points, and check containment. SHA-256 hashes identify the bytes used for analyzed files. This is not an OS sandbox. |
| Monorepo source layouts could produce incorrect import names. | Explicit relative import roots support multiple source trees; duplicate module names produce ambiguity diagnostics instead of arbitrary resolution. |
| Partial scans could look clean after filtering or in a diagram. | Centralized incomplete status, CLI exit 2, and visible report notices keep input/analysis failures separate from finding thresholds. |
| Exported evidence always carried source snippets. | Optional source omission removes excerpts and literal-bearing call expressions. Names, paths, settings, and diagnostic metadata remain. |
| A context manager could swallow an exception before a credited outer logger or span received it. | Unknown context managers now block outer coverage credit while preserving instrumentation inside them. Multiple with-items follow nesting order, including context-expression evaluation. Calls expose uncertainty and blocked-handler metadata. |
| Pattern captures could be mistaken for imported names, and eager lambda defaults were skipped. | Capture bindings invalidate shadowed names. Lambda defaults are scanned at creation while the body remains explicitly deferred. |
| Repositories with many non-Python files could exhaust discovery resources before the Python-file budget applied. | A streaming directory-entry budget counts all encountered entries before sorting, preserves exclusions, and marks truncated discovery incomplete. |
| Report paths could overwrite configuration or replace links. | Source/configuration destinations, common manifests, the active configuration, and link/directory destinations are refused. Atomic-write failure tests preserve an existing report and clean up temporary files. |
| A low unresolved count could obscure the kinds of assumptions used in resolution. | Summary resolution categories distinguish internal relationships from import/annotation attribution; diagnostic categories and uncertain-propagation counts are exposed separately. |

Context-manager coverage follows the possibility of exception suppression described in the [Python language reference](https://docs.python.org/3/reference/compound_stmts.html#the-with-statement). Treating unknown managers conservatively can increase review candidates even when their runtime behavior does not suppress failures.

## Validation evidence

The workload measurements below describe the initial hardening revision. The subsequent [implementation research](RELATED_WORK.md) adds initializer edges, nullable receiver resolution, and `sys.exc_info()` recognition, with current self-scan evidence in [DOGFOOD.md](DOGFOOD.md).

Local measurements used Windows 10 and Python 3.14.5. Times are single-run observations of the scanner, excluding fixture generation, JSON serialization, and diagram rendering; they are not performance guarantees or memory measurements.

| Workload | Source files | Symbols | Calls | Scan time | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| Generated framework-neutral workflows, six calls deep | 1,000 | 8,000 | 7,500 | 3.727 s | Exactly 500 expected FS005 findings, zero unresolved calls or diagnostics |
| Local `project-python-dfmea/src` | 163 | 2,617 | 51,295 | 11.801 s | Completed within default budgets; 844 review candidates |

The generated fixture alternates operations with and without a reporting owner. Its exact expected result validates these specific structures; it does not establish general precision or recall.

The DFMEA scan read 6,624,640 source bytes, visited 746,139 AST nodes, and used 12,725 graph-work units. It retained **18,642 unresolved calls** and **2,159 diagnostics** (1,934 deferred generator expressions, 170 deferred lambdas, and 55 context-manager propagation uncertainties). There were 286 calls inside contexts with uncertain suppression behavior. Those limitations remain even though the scan status is `complete`. Its 844 findings have not been independently labeled as correct or incorrect. Measurements were taken while the two benchmark processes ran concurrently, so small timing differences from earlier runs should not be interpreted as a performance regression or improvement.

The local suite now contains 112 tests, covering the original scanner and diagrams, the hardening cases, and research-informed resolution changes. Windows tests skip actual symbolic-link creation when the account lacks that capability; simulated replacement and containment checks still run. Local verification uses Python 3.11 and 3.14. Browser verification checks the interactive sample at 2560×1600, 1440×1000, and 390×844, including keyboard navigation, export, overflow, node overlap, and absence of network requests or JavaScript errors.

The repository includes a Windows/Linux CI matrix for Python 3.11 and 3.14. [The initial published revision passed that matrix](https://github.com/willtran87/project-py-flow-signal/actions/runs/35679950366); that run predates the research-informed changes. Reproduce scan measurements with `scripts/benchmark_scan.py`; retain its JSON output with the revision being evaluated.

## Remaining material gaps

1. **General Python coverage is approximate.** Dynamic dispatch, inheritance, mutation of globals/nonlocals, reflection, decorators, dependency injection, callbacks, and declarative workflow routing can remain unresolved or be approximated. Lambdas and deferred generator execution are explicitly limited. This is a call/exception relationship graph, not a complete statement-level control-flow graph.
2. **Instrumentation is evidence, not delivery.** Runtime filters, exporter setup, context-manager suppression, exception types from callees, and actual operational outcomes are not fully modeled. Log severity remains conditional on whether the operation recovered, degraded, or failed. Business criticality needs human context.
3. **Enterprise accuracy remains uncalibrated.** A pinned independent PyCG sample now measures call-pair precision/recall and explicitly reports six misses. Independent instrumentation/owner labels and representative enterprise calibration are still missing. Review representative findings and known omissions before defining CI thresholds.
4. **Resource supervision has explicit scope.** Ordinary scans retain analysis budgets, with parsing before AST checks. Opt-in supervised CLI scans now enforce worker allocation and wall-time limits through rendering on Windows/Linux. Interpreter bootstrap memory, parent-side publication, disk capacity, and filesystem snapshots remain outside those limits. The scanner still retains its model in memory; use a stable checkout and review the documented worker boundaries.
5. **Long-term review and integration remain limited.** Structural baselines and reasoned dismissals now support repeated reviews while location-based report IDs remain. Owner/expiry/history, SARIF export, and runtime call-pair import are now implemented. Review authentication, tamper-evident history, cross-version reconciliation, and trace-provider adapters remain limited. Source omission is not comprehensive secret detection.

## Recommended adoption

The [self-dogfood review](DOGFOOD.md) adds direct behavioral evidence: CLI/report checks, five injected failure scenarios, three deliberately executed recovery probes, and a comparison with observed internal calls. Subsequent increments close the original property omissions and all four later indexed omissions. The harness now requires 26 relationships; this remains only a sampled execution. Structured diagnostics and stderr are still not distinguished from absent reporting. These findings reinforce the supervised-pilot assessment.

Start with a stable checkout and an advisory scan. Configure import roots for the repository layout, known entrypoints, and application boundary/logging wrappers where the default recognition cannot resolve them. Review scan status, diagnostics, and unresolved calls alongside the findings; inspect the suggested owner of each failure signal before adding logs. Keep INFO/WARNING/ERROR decisions tied to actual operational outcomes.

Promote selected rules to a release gate only after representative labeled reviews, runtime comparisons for important paths, and measured resource use on the intended repository establish acceptable behavior. These are validation tasks still to do, not capabilities implied by the current test count.
