# FlowSignal scanning and checking itself

Latest update: [accuracy and review](ACCURACY_AND_REVIEW.md) implements the six P1/P2 enhancements, including resolution of the four indexed dogfood omissions, SARIF, dismissal lifecycle, and runtime import. Implementation and validation statements below describe their earlier increment.

The newer [typed-flow and coverage increment](TYPED_FLOW_AND_COVERAGE.md) adds receiver/helper-return inference, source-linked coverage explanations and updated validation. Measurements below remain historical evidence for their stated revision.

**Historical research-increment results:** the measurements and source lines below describe the earlier nine-file scanner. The [review-workflow increment](REVIEW_WORKFLOW.md) adds two modules, closes both property omissions listed below, and extends the harness to thirteen required observed relationships. Its newly added code has additional unresolved relationships; use the current `validation.json` for the current source snapshot. This document's earlier counts must not be interpreted as the latest scan totals.

The dogfood run validates the working CLI, report generation, selected call relationships, and failure handling. It also reveals concrete gaps in the graph and recommendations. It does **not** establish complete execution coverage or an independent accuracy benchmark.

## What ran

`scripts/dogfood.py` scanned the nine Python files in `src`, with `flowsignal.cli.main` configured as the entrypoint. It exercised the CLI both in-process and through `python -m flowsignal`, generated JSON, HTML, text, and Mermaid reports, and compared repeated findings and the embedded HTML graph against JSON.

Local runs passed on Python 3.11.4 and 3.14.5. Both produced:

| Observation | Result |
| --- | ---: |
| Files / symbols / calls | 9 / 96 / 926 |
| Resolved internal call sites | 205 |
| Unresolved call sites | 217 |
| Recommendation candidates | 12: eight FS001 and four FS005 |
| Analysis diagnostics | 56: 44 deferred generators, nine deferred lambdas, three context-manager uncertainties |
| Calls in uncertain propagation contexts | 15 |
| Scan status | Complete within configured scope and budgets |

The JSON CLI scan took 0.281 seconds on Python 3.14 in the recorded research-improvement run, including serialization and publication. Profiling was performed in a separate invocation and is not included in that timing. This is a local observation, not a performance guarantee. The JSON inventory records source hashes for the exact input bytes.

## Observed execution compared with static analysis

The harness used `sys.setprofile` while the analyzer performed an HTML CLI scan of itself. It requires eleven relationships to appear in actual execution, the static call facts, and the diagram, including these five original checks:

- `cli.main → scanner.scan`
- `scanner.scan → rules.evaluate`
- `cli.main → diagram.render_html`
- `diagram.render_html → diagram.graph_data`
- `cli.main → cli.write_report`

It also requires the four initializer relationships and two `Config` method relationships marked resolved in the table below. All eleven passed on both interpreters. These are observed call relationships, not an assertion that the diagram contains every executed statement or branch.

Before the [research-informed improvements](RELATED_WORK.md), the Python 3.14 run observed 105 distinct caller/callee pairs between indexed symbols: 97 present and eight missing. The updated source's run observed 111 such pairs: 109 present and two missing. Six of the original eight omissions are now captured. Another 22 observed pairs involved functions not indexed as standalone symbols, primarily generator expressions and lambdas. Python 3.11 had different runtime counts, including comprehension frames, but the same two remaining missing pairs between indexed symbols. Source changes changed the denominator; these counts describe one execution per interpreter, not whole-program recall or precision.

| Originally missing relationship | Current status |
| --- | --- |
| `rules.evaluate → Handler.catches_all` | Property access invokes code without an explicit call expression. |
| `rules.evaluate.effective_handlers → Handler.catches_all` | Same property-access limitation. |
| `FactVisitor.visit_ClassDef → BindingCollector.__init__` | Resolved as an inferred constructor relationship. |
| `scanner.scan → BindingCollector.__init__` | Resolved as an inferred constructor relationship. |
| `scanner.scan → FactVisitor.__init__` | Resolved as an inferred constructor relationship. |
| `scanner.scoped_bindings → BindingCollector.__init__` | Resolved as an inferred constructor relationship. |
| `scanner.scan → Config.validate` | Resolved through nullable annotation and same-type fallback binding. |
| `scanner.scan → Config.to_dict` | Resolved through nullable annotation and same-type fallback binding. |

The property gaps remain reproducible. The six corrected relationships are enforced by the harness so future changes cannot silently lose them.

## Review of all 12 findings

Locations below refer to the source snapshot in the recorded self-scan. They are review judgments based on source inspection; the scanner itself still emits its original conditional recommendations. No blanket logging changes or suppressions were made to make the scan look clean.

| Rule and location | Review disposition |
| --- | --- |
| FS005, `cli.py:113`, `write_report` | Publication failures reach the CLI error handler. Injected replacement failure produced stderr plus exit 2, preserved the previous report, and removed temporary output. An additional inner ERROR log is unnecessary for this tested path. |
| FS005, `cli.py:225`, JSON serialization | Serialization is a real failure boundary, but this candidate does not demonstrate a missing operational log. Review unexpected serialization defects at the CLI owner rather than logging every conversion. |
| FS001, `cli.py:249`, CLI error handler | Errors are printed to stderr and return exit 2. That is existing user-visible reporting, which logger recognition does not model. Verified for configuration and publication failures. |
| FS005, `config.py:108`, TOML parsing | Parse failures propagate to the CLI's `ValueError` handler. This is an ownership review, not evidence that `load_config` needs another log. The direct Python API intentionally propagates errors to its caller. |
| FS005, `diagram.py:395`, JSON serialization | Same conditional serialization concern as CLI JSON output. No new missing-log defect was established. |
| FS001, `scanner.py:63`, annotation parsing fallback | A useful follow-up: malformed string annotations lose receiver information without a specific diagnostic. The resulting call may be unresolved, but a dedicated analysis diagnostic would explain why. An operational ERROR log is not the appropriate default. |
| FS001, `scanner.py:1288`, eligibility failure | The handler appends a structured `source_unreadable` diagnostic. Additional error logging would duplicate the report's existing reporting path. |
| FS001, `scanner.py:1330`, discovery failure | The handler appends `directory_unreadable`, which makes the scan incomplete. Inspected; this specific filesystem branch was not separately fault-injected. |
| FS001, `scanner.py:1349`, entry inspection failure | The handler appends `source_unreadable`. Inspected; this particular branch was not separately fault-injected. |
| FS001, `scanner.py:1469`, source parsing/read failure | The handler appends an input diagnostic and preserves an incomplete result. Malformed source and injected read failure both verified this behavior. |
| FS001, `scanner.py:1493`, module binding recursion | The handler appends `analysis_depth`. Reporting already exists; this exact recursion branch was inspected, not induced in the dogfood run. |
| FS001, `scanner.py:1519`, symbol traversal recursion | The handler appends `analysis_depth` with file and symbol location. Same reporting disposition and test limitation. |

The main recommendation limitation exposed here is recognizing alternative reporting mechanisms. A structured diagnostic or CLI stderr message can be appropriate instrumentation even though it is not a recognized logger call. The presence of a finding alone should not trigger automatic logging edits.

## Failure handling and runtime probes

Five failure scenarios passed:

| Scenario | Observed result |
| --- | --- |
| Malformed Python source | Incomplete JSON report; input diagnostic; exit 2 |
| Injected source read failure | Incomplete JSON report; input diagnostic; exit 2 |
| Source-size budget exhausted | Incomplete JSON report; limit diagnostic; exit 2 |
| Invalid configuration | Stderr and exit 2; previous report unchanged |
| Injected report publication failure | Stderr and exit 2; previous report unchanged; temporary files removed |

The finding threshold returned exit 1 with a complete report, distinguishing it from incomplete analysis. A scanned file containing a marker-file write and a top-level exception produced neither side effect: the analyzer never executed it.

Separately, the harness deliberately executed its own small, standard-library-only probe in a subprocess. That authored probe is distinct from arbitrary scan targets:

- A return from `finally` replaced a raised exception; FS002 identified it.
- A silent exception recovery returned a fallback without a log; FS001 identified it.
- A recovery with a WARNING event returned the fallback and emitted exactly one WARNING; no finding was attached to that recovery function.

These three examples corroborate selected rules. They do not validate every severity recommendation or every rule.

## Browser verification and reproduction

The actual self-report passed Playwright checks at 2560×1600, 1440×1000, and 390×844: initial focus, bounded node rendering, search, keyboard selection, matching finding counts, visible diagnostics, conditional advice, SVG download, no page overflow, no remote requests, and no JavaScript errors. Desktop and mobile screenshots were inspected. A fitted full graph can have small labels on mobile; zoom and the detail pane remain necessary for reading them.

From the repository, with FlowSignal installed in the active Python environment:

```text
python scripts/dogfood.py
node scripts/check_dogfood_browser.cjs .artifacts/dogfood
```

The browser step requires a separate Playwright installation and Chromium. The Python step uses only the standard library plus this project. It is included in the CI matrix; the browser step remains separate. [Remote CI passed for the initial published revision](https://github.com/willtran87/project-py-flow-signal/actions/runs/35679950366). That run predates these research-informed changes; the results reported here for this increment are local.

Outputs in `.artifacts/dogfood/` include `self.json`, `self.html`, `self.txt`, `self.mmd`, `validation.json`, `runtime-edges.json`, browser screenshots/SVGs, and `browser-validation.json`. The Python 3.11 run is retained separately in `.artifacts/dogfood-py311/`.

The result supports supervised use of the tested functionality. The two remaining property relationships, unindexed deferred expressions, and alternative-reporting findings remain concrete reasons to retain human review.
