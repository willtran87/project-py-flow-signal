# FlowSignal

**A deterministic Python execution-flow and observability analyzer.**

FlowSignal scans Python source without importing or executing the target project. It maps resolvable calls, identifies failure-sensitive boundaries, inspects exception handling and logging, and proposes instrumentation with source evidence and explicit conditions.

The core needs no LLM, API key, network access, or third-party runtime dependencies. This is an initial implementation for reviewing possible execution paths. It does not prove runtime behavior, business criticality, or complete instrumentation coverage.

## Run it

Requires Python 3.11 or newer. From this repository:

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
.venv\Scripts\flowsignal scan C:\path\to\python-project
```

On macOS/Linux, the executable is `.venv/bin/flowsignal`.

For a source checkout without installation:

```powershell
$env:PYTHONPATH = "src"
python -m flowsignal scan examples/order_service.py --entrypoint order_service.checkout
```

Useful commands:

```text
flowsignal scan ./my-project --format json --output report.json
flowsignal scan ./my-project --format html --output flow-report.html
flowsignal scan ./my-project --timeout-seconds 300 --memory-mb 1024 --format html --output flow-report.html
flowsignal scan ./my-project --format mermaid --output flow-diagram.mmd
flowsignal scan ./my-project --entrypoint "app.api.checkout"
flowsignal scan ./my-project --exclude tests --exclude vendor
flowsignal scan ./my-project --min-confidence medium --fail-on high
flowsignal scan ./my-project --entrypoint "app.worker.run" --lifecycle-info
flowsignal rules
```

`--entrypoint` marks operation owners for call-path presentation and recommendations; it does not restrict which files are scanned. Use a directory or file target and exclusions to control scan scope. A symbol can be selected by qualified name (`shop.api.checkout`) or relative file and symbol (`src/shop/api.py:checkout`); both accept shell-style globs. Quote globs in your shell.

Exit codes: **0** means the scan completed without meeting the selected failure threshold, **1** means a reported finding met `--fail-on`, and **2** means an invalid request or incomplete scan (for example, a syntax error, unreadable source, unmatched configured entrypoint, or source limit). Findings are still published for readable files during an incomplete scan. The default is `--fail-on none`. An empty findings list is not proof of adequate observability.

## What is implemented

- Python AST collection, import aliases, package-relative imports, `src/` layouts, nested functions, and conservative call resolution through local classes and simple or nullable type annotations.
- Inferred edges from known class constructions to their explicit initializers, labeled as possible initializers in diagrams. Conflicting receiver types, decorated/customized construction, and inherited initializer lookup remain conservative.
- Inferred receiver fields and local helper returns, including annotated parameters stored on `self`, compatible constructor assignments, method returns, and awaited async factories. Conflicting observed types remain unresolved.
- Source-linked coverage decisions for every recognized boundary, including credited reporting owners and reasons coverage cannot be established. The decision is shared with FS005; it is not proof of runtime delivery.
- Source-linked calls, handlers, logs, entrypoint-to-callee paths, and unresolved or ambiguous references.
- Lexical exception scopes, bounded upstream reporting checks, explicit handler outcomes, simple constant branch pruning, and terminal-statement pruning.
- Separate awaited and deferred coroutine calls, plus discarded `asyncio.create_task`/`ensure_future` handles.
- Common network, persistence, serialization, filesystem, and subprocess boundaries, with configurable patterns for other libraries and application APIs.
- Recognition of standard logging, structlog, loguru, common logger factory assignments, configured logging wrappers, and basic OpenTelemetry span context managers.
- Traceback recognition for `logger.exception()`, constant truthy `exc_info`, and import-resolved `sys.exc_info()` including aliases.
- Text and versioned JSON reports. Findings have stable IDs for unchanged source locations, code evidence, rule IDs, review priority, confidence, conditional recommendations, and unresolved assumptions.
- Offline HTML execution diagrams and Mermaid exports, showing recognized instrumentation alongside recommendations at functions, operation boundaries, and exception handlers.
- Supported property reads appear as inferred getter edges, including receivers obtained from annotated dictionary fields and `.values()` iteration.
- Explicit contracts for diagnostic, stderr, and error-return reporting, with the reporting owner shown separately from logs.
- Structural finding baselines, new/unchanged/resolved comparisons, and dismissals with owner, expiry, renewal signals, and retained history.
- Reporting-owner path views that highlight the inspected caller chain, credited owners, and barriers.
- SARIF 2.1.0 export, with review priority separate from logging severity.
- External runtime call-pair import with source-hash checks and an optional observation overlay; static findings remain unchanged.
- A pinned development/evaluation accuracy corpus and CI regression gate. See [accuracy and review](docs/ACCURACY_AND_REVIEW.md) for all six P1/P2 enhancements and measured limits.
- Opt-in supervised CLI scans with enforced worker memory and wall-time limits, including report rendering; timeout/crash failures preserve existing report and baseline destinations.
- Unresolved-call reasons, suggested next checks, and bounded potential-entrypoint context, with reason/entrypoint filters in the HTML report.

## Supervised scans and unresolved calls

Either `--timeout-seconds` or `--memory-mb` enables a separate worker. The omitted companion limit defaults to 300 seconds or 1024 MiB. Windows uses a Job Object committed-memory limit; Linux uses an address-space limit. These are allocation limits, not interchangeable RSS measurements. If enforcement cannot be installed, the scan fails rather than silently dropping the limit. Other platforms can use ordinary scans without these flags.

Worker startup, source analysis, baseline preparation and report rendering are supervised. On timeout, allocation failure or worker crash, the CLI returns 2, emits a small JSON failure record on stderr, and preserves existing output destinations. Completed artifacts are copied from staging in bounded chunks and atomically published; this final parent-side I/O is outside the worker deadline. The bootstrap's interpreter startup occurs before its memory limit is installed. This is resource supervision, not a security sandbox or an atomic filesystem snapshot. Ordinary scans and the `scan()` Python API retain their existing analysis budgets and do not automatically launch workers.

Every explicitly unresolved or ambiguous call now has a `resolution_gaps` record with a reason, source location, next action, potential entrypoints through known edges, and a `context_truncated` flag. Unknown local-class members are no longer labeled as external imports simply because the receiver has an annotation. This can increase the unresolved count while making the report more honest.

Expand **Unresolved call review** in HTML to filter by reason or potential entrypoint and navigate to the owning function. JSON/text retain every record; HTML shows up to 100 matching records at a time and 12 examples per symbol. No entrypoint found does not mean unreachable. `max_resolution_steps` defaults to 100,000; exhausting it marks context partial and the scan incomplete. See [supervision and uncertainty](docs/SUPERVISION_AND_UNCERTAINTY.md) for failure semantics, limits, and reproduction steps.

## Repeatable review workflow

```text
flowsignal scan ./my-project --save-baseline baseline.json
flowsignal scan ./my-project --baseline baseline.json --format html --output review.html
flowsignal review dismiss baseline.json FINDING_FINGERPRINT --owner workflow-platform --reason "Reviewed: the caller owns this outcome"
flowsignal scan ./my-project --baseline baseline.json --fail-on medium --fail-on-new
flowsignal review restore baseline.json FINDING_FINGERPRINT
```

Use the finding's `fingerprint` from JSON, text, or the HTML evidence panel. Dismissed findings retain their reason and remain visible; they are excluded from failure thresholds. Baselines tolerate comments, formatting, and line shifts in ordinary symbols. Incomplete comparisons mark absent findings **unverified**, and incomplete scans cannot replace a baseline. Recognition settings and scan scope must match.

See the [review workflow guide](docs/REVIEW_WORKFLOW.md) for reporter contracts, supported property inference, baseline limitations, and a reproducible demonstration. These features support advisory reviews; they do not establish enterprise-wide accuracy or complete execution coverage.

## Rules

The rule set is intentionally a starting point:

See [implementation research and improvement priorities](docs/RELATED_WORK.md) for the reviewed upstream projects, pinned sources, adopted ideas, validation, and remaining gaps.

| Rule | Review candidate |
| --- | --- |
| FS001 | A handler may consume an exception without a recognized reporting signal |
| FS002 | A `finally` exit can suppress an active exception |
| FS003 | A recognized operation entrypoint rethrows after logging below ERROR |
| FS004 | Inner and outer handlers may log the same propagated failure |
| FS005 | A known failure-sensitive boundary has no recognized covering instrumentation |
| FS006 | An existing exception-handler log may need traceback context |
| FS007 | A background task handle is discarded |
| FS008 | An explicitly configured operation may benefit from an INFO lifecycle event; opt-in only |

Narrow exception handlers can represent ordinary control flow or structured error returns. FS001 candidates for these handlers have low confidence and priority. `--min-confidence medium` hides those candidates without changing the underlying source facts.

## Severity follows the outcome

**Review priority, finding confidence, and recommended log level are different fields.** A high-priority finding is not an instruction to emit an ERROR log on every execution.

| Recommendation | Condition |
| --- | --- |
| INFO | An important expected lifecycle event or successful business operation |
| WARNING | Unexpected degradation when useful processing can continue |
| ERROR | Required processing fails at the boundary responsible for reporting it |
| Trace / metric | Latency, operation relationships, or frequent outcomes need visibility |
| Enrich an existing log | The event already exists but useful exception context is absent |
| No additional log | An existing owner already reports the same failure |

For example, a timeout followed by an acceptable cache hit may need no log. An unexpected degraded cache fallback may warrant WARNING. A failed request after fallback and retry exhaustion may warrant one ERROR at the request owner. Static syntax alone cannot establish which outcome applies.

Recommendations therefore include a triggering condition. They do not automatically modify source. Use safe identifiers and metadata as context; do not include credentials, raw request bodies, or sensitive payloads simply because they are available.

## Configuration

The CLI reads `flowsignal.toml`, or `[tool.flowsignal]` in `pyproject.toml`, from the scan root. Use `--config` for another location. CLI entrypoints and exclusions are appended to configured values. See [flowsignal.example.toml](flowsignal.example.toml).

```toml
[flowsignal]
exclude = ["tests", "generated", "vendor"]
entrypoints = ["app.api.checkout", "app.worker.process_job"]
logger_names = ["audit_logger"]
max_files = 10000
max_file_bytes = 2000000
max_path_depth = 8
max_paths = 5
lifecycle_info = false

[[flowsignal.boundaries]]
pattern = "company.gateway.Client.*"
category = "network"

[[flowsignal.loggers]]
pattern = "company.telemetry.record_failure"
level = "ERROR"
```

Patterns match resolved callable names, such as an import alias expanded to `company.gateway.fetch`. Custom boundary patterns can also intentionally classify unresolved names. `logger_names` identifies receivers by their source spelling, such as `audit_logger` or `self.audit`. Logger wrapper levels are explicit project assertions; the scanner does not verify the wrapper implementation.

Default excluded directories include `.git`, `.venv`, `venv`, `env`, `node_modules`, `__pycache__`, `build`, `dist`, `.tox`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `site-packages`, and `.artifacts`. Directory scans skip symbolic links and Windows reparse points. Source reads are bounded, checked for replacement or modification, and respect Python encoding declarations. Explicit single-file targets are scanned regardless of exclusion patterns.

For monorepos, set `source_roots = ["services/orders/src", "libraries/common/src"]` in the configuration, or repeat `--source-root` with paths relative to the scan target. These determine import names; they do not restrict which files are scanned. The longest matching root wins. Duplicate module names remain ambiguous and make the scan incomplete rather than selecting an arbitrary implementation.

Aggregate defaults are 64,000,000 source bytes, 2,000,000 visited AST nodes, AST depth 120, and 1,000,000 caller-graph work units. Configure these with `max_total_bytes`, `max_ast_nodes`, `max_ast_depth`, and `max_graph_steps`. These bound selected stages of analysis; they are not hard memory or time limits. AST parsing happens before the AST-node check.

Receiver and helper-return inference has a separate `max_type_steps` budget, defaulting to 1,000,000. Exhaustion adds `type_work_limit`, marks the scan incomplete, returns CLI exit 2, and prevents saving a baseline. The consumed count is `analysis_stats.type_steps`.

`max_discovery_entries` defaults to 100,000 filesystem entries, counting directories and non-Python files as well as Python files. Excluded directory contents are not enumerated. Reaching this budget marks the report incomplete and returns CLI exit 2. Discovery stops reading additional directory entries before sorting the collected subset; an incomplete subset can depend on filesystem enumeration order. Complete scans retain sorted traversal.

`summary.status` is `incomplete` when input or analysis failures prevent completing the configured scan. The CLI returns 2 even if a confidence filter removes every finding. Text and interactive reports display this status; Mermaid exports include an incomplete-scan notice. `complete` means completion within the supported model, exclusions, and budgets, not complete execution coverage. Deferred expressions and dynamic dispatch can still be unmodeled. JSON includes an `inventory` with analyzed file hashes and encountered skip reasons, plus `analysis_stats` with resource counters. Pruned directories are recorded once; budget stops do not enumerate every remaining descendant.

Use `--no-source` or `include_source = false` to omit source excerpts and literal-bearing call expressions from exported reports. Paths, symbol names, metadata, configuration, and diagnostic messages remain. This is source minimization, not a general secret redactor.

Report publication is atomic. Source/configuration extensions and common project manifest names are refused as report destinations; the active configuration is protected even with a custom filename. A destination that is a symbolic link, reparse point, or directory is also refused. Ordinary report files can be replaced. These checks are not a universal classifier of repository assets; choose a dedicated report path.

## Execution diagrams

Generate a self-contained interactive report and open the HTML file in a browser:

```powershell
.venv\Scripts\flowsignal scan examples/instrumented_checkout.py `
  --entrypoint instrumented_checkout.checkout `
  --format html --output flow-report.html
```

The report uses embedded SVG, JavaScript, and CSS; it makes no network requests and needs no server, CDN, Mermaid installation, or additional Python dependency. Select a node to inspect its current logs/traces, source evidence, recommendation conditions, confidence, and assumptions. Functions with existing instrumentation can also have recommendations.

Select a boundary to inspect its **Coverage decision**. Explanations identify unconditional logs, configured owners, span options, conditional signals, silent consumption, uncertain propagation, and caller limits. Source buttons navigate to the owning function. JSON and text include every boundary decision; Mermaid includes decisions for the displayed neighborhood. See [typed flow and coverage](docs/TYPED_FLOW_AND_COVERAGE.md) for a reproducible example and limits.

- **Green:** a recognized log or call inside a trace scope. This establishes presence, not operational coverage or delivery.
- **Amber:** a conditional recommendation. Multiple levels represent alternative outcomes, not instructions to log the same event at every level.
- **Gray:** no signal recognized at that node. It does not automatically mean instrumentation is missing.
- **Solid arrows:** resolved calls and known external boundaries.
- **Dashed arrows:** lexical exception-handler candidates or handler ownership. These do not prove exception propagation or statement ordering.
- **Dotted arrows:** coroutine or generator creation, whose deferred execution must be distinguished from an awaited call.

Search for a function or file to change focus, include or exclude callers, zoom, or download the current diagram as SVG. The SVG's accessible description retains the displayed recommendations' triggering conditions. On narrow screens the diagram scrolls independently; **Fit view** provides a complete overview.

The default view is a neighborhood around a recognized entrypoint or a finding path, capped at 60 nodes. The report explicitly distinguishes nodes omitted by the view limit from nodes outside the selected neighborhood. Unresolved calls remain visible as counts and source examples; their targets are not invented. Individual internal call sites are aggregated as edges between symbols, so this is not a statement-by-statement control-flow graph or a runtime sequence diagram.

Use `--diagram-focus app.api.checkout` (an exact qualified name or symbol ID) and `--diagram-max-nodes 100` (1–200) to set the initial view. These options affect the diagram, not the scan scope. The HTML includes the graph projection for the entire scan so you can change focus locally. It also includes source evidence; handle exported reports as source-containing artifacts.

For Markdown and documentation tools, use `--format mermaid --output flow-diagram.mmd`. The exported text uses the same graph projection and initial-view limits, with conditional recommendations preserved in comments. It can be pasted into a Mermaid code block. HTML is the richer format for reviewing the evidence and conditions.

## Python API and JSON

```python
from flowsignal import scan
from flowsignal.config import Config

report = scan("./my-project", Config(entrypoints=["app.main"]))
payload = report.to_dict()
for finding in report.findings:
    print(finding.rule_id, finding.symbol, finding.recommendations)
```

The Python API uses only the `Config` you supply; CLI-style config autodiscovery is not implicit. The JSON schema identifier is `flowsignal-report-1`. The report contains `summary`, `settings`, `inventory`, `analysis_stats`, `symbols`, `calls`, `handlers`, `logs`, `findings`, `diagnostics`, and `limitations`. Call handler IDs refer to records in `handlers`. Possible paths may stop at an entrypoint, an unresolved caller frontier, a cycle, or a configured depth limit. They are bounded review examples, not an exhaustive set of paths.

Finding IDs incorporate rule, symbol, line, and column. Moving source can change an ID. Structural fingerprints support [baselines and reasoned dismissals](docs/REVIEW_WORKFLOW.md); they tolerate line movement but do not guarantee reconciliation across symbol renames or incompatible scanner changes. Reports also contain `reporting_signals`, `baseline`, `coverage`, `review_history`, and optional `runtime` comparisons. Coverage records reference `calls` through `call_id` and include status, source evidence, and an explanation-truncation flag.

`summary.resolution_counts` separates calls resolved lexically, inferred from receivers, recognized as builtins, attributed through an import/annotation, and unresolved or ambiguous calls. Import/annotation attribution does not prove that the dependency exists or resolve its implementation. `summary.diagnostic_counts` groups analysis limitations, while `summary.propagation_uncertain_calls` counts calls inside contexts whose suppression behavior is unknown. None of these counts is a coverage percentage.

## Boundaries of this version

The current readiness assessment is **a supervised enterprise pilot and advisory review tool**. It is not yet validated as an exhaustive scan or a release gate. See [the readiness assessment](docs/READINESS.md) for the hardening changes, measured workloads, and remaining gaps. The core has no workflow-framework dependency; ordinary Python is analyzed consistently regardless of which orchestrator uses it. Framework-declared routing and callback scheduling are not automatically reconstructed as execution edges.

This is **not a complete control-flow graph or whole-program type analysis**. Call resolution is approximate; lexical reachability is not runtime reachability. Unresolved dispatch is retained instead of guessing a target. Inheritance, complex reassignments, decorators, metaprogramming, dependency injection, monkey-patching, callbacks, and framework lifecycle behavior can limit resolution. Framework entrypoint patterns cover common cases and are extensible through explicit entrypoints.

Exception types from callees are not inferred. Typed handlers, conditional logging, mixed recovery/rethrow paths, and incomplete callers reduce what can be credited as coverage. Generator expressions retain only their eager outer iterable; lambda bodies and `ExceptionGroup` splitting emit diagnostics. Generator-function iteration, definition-time annotations, and decorator application are not fully modeled. Recognizing a logger or trace scope does not verify exporter configuration, delivery, filters, or runtime log levels.

Lambda defaults are scanned where the lambda is created; its body remains deferred. Pattern-match captures invalidate shadowed names. Unknown context managers are treated as possible exception-suppression barriers: instrumentation inside the barrier can be credited, but outer handlers/spans cannot. Multiple `with` items are modeled in nesting order. Known file, null, and recognized tracing contexts retain their modeled propagation behavior. This conservative approach can add review candidates for custom managers that do not suppress exceptions; the diagnostic makes that assumption explicit.

Boundary rules identify review candidates. They cannot infer business importance from a network request or database call. Generic adapters intentionally miss unknown APIs rather than label every `.get()` or `.execute()` as an external operation. A small pinned PyCG evaluation set now measures call-pair accuracy and preserves known misses. Enterprise precision/recall and independent instrumentation judgments remain uncalibrated; see [accuracy results](docs/ACCURACY_AND_REVIEW.md).

LLM enrichment remains a future extension. Runtime call-pair import is available through `--runtime-trace`, with source hashes and separate observation/inference statuses. There is no LLM integration or source upload in this version. Any later enrichment should cite existing finding IDs and remain separate from deterministic facts; it should not be required for discovery.

## Accuracy, exports, and lifecycle review

```powershell
flowsignal scan C:\path\to\repo --format sarif --output report.sarif
flowsignal scan C:\path\to\repo --runtime-trace trace.json --format html --output report.html
python scripts/benchmark_accuracy.py --check benchmarks/baseline.json
python scripts/validate_product_features.py
```

The [accuracy and review guide](docs/ACCURACY_AND_REVIEW.md) documents the trace schema, review expiry, reporting-path controls, benchmark labels, and current validation.

## Development

The project can scan and validate itself. Run `python scripts/dogfood.py` to exercise the CLI, compare its core static calls with observed execution, check report consistency, and inject failures. It creates `.artifacts/dogfood/self.html` plus machine-readable evidence. The [current validation record](docs/ACCURACY_AND_REVIEW.md) describes 26 required observed relationships, a labeled accuracy corpus, and runtime import; the [earlier dogfood review](docs/DOGFOOD.md) retains historical evidence. This is behavioral validation with explicit limitations, not a claim of full coverage.

```powershell
$env:PYTHONPATH = "src"
python -m unittest discover -s tests -v
python -m compileall -q src
```

Tests cover call resolution, negative controls for shadowed names, exception propagation and reporting ownership, async execution timing, conditional instrumentation, source limits, non-execution of scanned code, deterministic reports, and CLI exit semantics.

Diagram tests additionally cover instrumentation-to-source alignment, bounded/cyclic graphs, deferred execution, preservation of internal calls at custom boundaries, and safe embedding of source text. An optional browser check requires a separate Playwright installation and its Chromium browser:

```text
node scripts/check_diagram_browser.cjs flow-report.html .artifacts/visual
```

Run that check against the `instrumented_checkout.py` example report shown above. It verifies keyboard selection, navigation, zoom, SVG download, no remote requests, and layout at 2560×1600, 1440×1000, and 390×844.

Run `python scripts/benchmark_scan.py --files 1000 --output .artifacts/benchmark.json` for a reproducible general-Python fixture. It checks exact expected findings and unresolved-call counts, and reports local scan time separately from fixture creation. `--target path/to/source` measures an existing codebase without claiming its findings are ground truth. The CI workflow covers Python 3.11 and 3.14 on Windows and Linux, unit tests, a smaller synthetic workload, lint, formatting, and wheel builds; browser checks are a separate local step.

The project draws its scope from the Python analysis and failure-path portions of PySFMEA. It is implemented as a small independent package; it does not depend on PySFMEA's assurance, standards, review governance, or fault-tree machinery.
