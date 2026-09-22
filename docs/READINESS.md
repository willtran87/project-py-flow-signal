# Enterprise readiness assessment

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

Local measurements used Windows 10 and Python 3.14.5. Times are single-run observations of the scanner, excluding fixture generation, JSON serialization, and diagram rendering; they are not performance guarantees or memory measurements.

| Workload | Source files | Symbols | Calls | Scan time | Outcome |
| --- | ---: | ---: | ---: | ---: | --- |
| Generated framework-neutral workflows, six calls deep | 1,000 | 8,000 | 7,500 | 3.727 s | Exactly 500 expected FS005 findings, zero unresolved calls or diagnostics |
| Local `project-python-dfmea/src` | 163 | 2,617 | 51,295 | 11.801 s | Completed within default budgets; 844 review candidates |

The generated fixture alternates operations with and without a reporting owner. Its exact expected result validates these specific structures; it does not establish general precision or recall.

The DFMEA scan read 6,624,640 source bytes, visited 746,139 AST nodes, and used 12,725 graph-work units. It retained **18,642 unresolved calls** and **2,159 diagnostics** (1,934 deferred generator expressions, 170 deferred lambdas, and 55 context-manager propagation uncertainties). There were 286 calls inside contexts with uncertain suppression behavior. Those limitations remain even though the scan status is `complete`. Its 844 findings have not been independently labeled as correct or incorrect. Measurements were taken while the two benchmark processes ran concurrently, so small timing differences from earlier runs should not be interpreted as a performance regression or improvement.

The local suite contains 98 tests, covering the original scanner and diagrams plus the hardening cases. Windows tests skip actual symbolic-link creation when the account lacks that capability; simulated replacement and containment checks still run. Local verification uses Python 3.11 and 3.14. Browser verification checks the interactive sample at 2560×1600, 1440×1000, and 390×844, including keyboard navigation, export, overflow, node overlap, and absence of network requests or JavaScript errors.

The repository includes a Windows/Linux CI matrix for Python 3.11 and 3.14. Adding the workflow does not establish that remote CI has run. Reproduce scan measurements with `scripts/benchmark_scan.py`; retain its JSON output with the revision being evaluated.

## Remaining material gaps

1. **General Python coverage is approximate.** Dynamic dispatch, inheritance, mutation of globals/nonlocals, reflection, decorators, dependency injection, callbacks, and declarative workflow routing can remain unresolved or be approximated. Lambdas and deferred generator execution are explicitly limited. This is a call/exception relationship graph, not a complete statement-level control-flow graph.
2. **Instrumentation is evidence, not delivery.** Runtime filters, exporter setup, context-manager suppression, exception types from callees, and actual operational outcomes are not fully modeled. Log severity remains conditional on whether the operation recovered, degraded, or failed. Business criticality needs human context.
3. **Accuracy has not been calibrated on an independent labeled corpus.** The real-codebase scan demonstrates practical execution, not a measured false-positive or false-negative rate. Review representative findings and known omissions before defining CI thresholds.
4. **Budgets are not hard process isolation.** Python parsing occurs before AST checks. Directory discovery, serialization, and HTML projection have no hard time/RSS limit. The scanner retains its model in memory. Identity checks detect common file changes but do not provide an atomic snapshot of a concurrently modified repository. Use a stable checkout and a separately constrained worker when stronger limits are required.
5. **Long-term review and integration remain basic.** Finding IDs depend on source locations. There is no persistent baseline, suppression lifecycle, cross-version reconciliation, SARIF export, or runtime trace import. Source omission is not comprehensive secret detection.

## Recommended adoption

The [self-dogfood review](DOGFOOD.md) adds direct behavioral evidence: CLI/report checks, five injected failure scenarios, three deliberately executed recovery probes, and a comparison with observed internal calls. It also records eight missing relationships between indexed symbols and the need to distinguish structured diagnostics and stderr from absent reporting. These findings reinforce the supervised-pilot assessment.

Start with a stable checkout and an advisory scan. Configure import roots for the repository layout, known entrypoints, and application boundary/logging wrappers where the default recognition cannot resolve them. Review scan status, diagnostics, and unresolved calls alongside the findings; inspect the suggested owner of each failure signal before adding logs. Keep INFO/WARNING/ERROR decisions tied to actual operational outcomes.

Promote selected rules to a release gate only after representative labeled reviews, runtime comparisons for important paths, and measured resource use on the intended repository establish acceptable behavior. These are validation tasks still to do, not capabilities implied by the current test count.
