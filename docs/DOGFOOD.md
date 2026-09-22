# FlowSignal scanning and checking itself

The dogfood run validates the working CLI, report generation, selected call relationships, and failure handling. It also reveals concrete gaps in the graph and recommendations. It does **not** establish complete execution coverage or an independent accuracy benchmark.

## What ran

`scripts/dogfood.py` scanned the nine Python files in `src`, with `flowsignal.cli.main` configured as the entrypoint. It exercised the CLI both in-process and through `python -m flowsignal`, generated JSON, HTML, text, and Mermaid reports, and compared repeated findings and the embedded HTML graph against JSON.

Local runs passed on Python 3.11.4 and 3.14.5. Both produced:

| Observation | Result |
| --- | ---: |
| Files / symbols / calls | 9 / 93 / 873 |
| Resolved internal call sites | 186 |
| Unresolved call sites | 214 |
| Recommendation candidates | 12: eight FS001 and four FS005 |
| Analysis diagnostics | 53: 42 deferred generators, eight deferred lambdas, three context-manager uncertainties |
| Calls in uncertain propagation contexts | 15 |
| Scan status | Complete within configured scope and budgets |

The JSON CLI scan took 0.206 seconds on Python 3.14 in the recorded run, including serialization and publication. Profiling was performed in a separate invocation and is not included in that timing. This is a local observation, not a performance guarantee. The JSON inventory records source hashes for the exact input bytes.

## Observed execution compared with static analysis

The harness used `sys.setprofile` while the analyzer performed an HTML CLI scan of itself. It required these five relationships to appear in actual execution, the static call facts, and the diagram:

- `cli.main → scanner.scan`
- `scanner.scan → rules.evaluate`
- `cli.main → diagram.render_html`
- `diagram.render_html → diagram.graph_data`
- `cli.main → cli.write_report`

All five passed. These are observed call relationships, not an assertion that the diagram contains every executed statement or branch.

In the recorded Python 3.14 run, 105 distinct observed caller/callee pairs connected symbols indexed by the scanner. The static graph contained 97 of those pairs and missed eight. Another 22 observed pairs involved functions not indexed as standalone symbols, primarily generator expressions and lambdas. Python 3.11 had different runtime counts, including comprehension frames, but the same eight missing pairs between indexed symbols. These counts describe one execution per interpreter; they are not whole-program recall or precision.

| Missing relationship | Explanation from source inspection |
| --- | --- |
| `rules.evaluate → Handler.catches_all` | Property access invokes code without an explicit call expression. |
| `rules.evaluate.effective_handlers → Handler.catches_all` | Same property-access limitation. |
| `FactVisitor.visit_ClassDef → BindingCollector.__init__` | Class construction is not expanded to an initializer edge. |
| `scanner.scan → BindingCollector.__init__` | Same constructor limitation. |
| `scanner.scan → FactVisitor.__init__` | Same constructor limitation. |
| `scanner.scoped_bindings → BindingCollector.__init__` | Same constructor limitation. |
| `scanner.scan → Config.validate` | Optional annotation and reassignment through `config or Config()` lose receiver information. |
| `scanner.scan → Config.to_dict` | Same receiver-inference limitation. |

These are reproducible gaps, not fixed by this validation work. Future changes can use the retained runtime edge list to evaluate improvements without guessing at runtime behavior.

## Review of all 12 findings

Locations below refer to the source snapshot in the recorded self-scan. They are review judgments based on source inspection; the scanner itself still emits its original conditional recommendations. No blanket logging changes or suppressions were made to make the scan look clean.

| Rule and location | Review disposition |
| --- | --- |
| FS005, `cli.py:113`, `write_report` | Publication failures reach the CLI error handler. Injected replacement failure produced stderr plus exit 2, preserved the previous report, and removed temporary output. An additional inner ERROR log is unnecessary for this tested path. |
| FS005, `cli.py:225`, JSON serialization | Serialization is a real failure boundary, but this candidate does not demonstrate a missing operational log. Review unexpected serialization defects at the CLI owner rather than logging every conversion. |
| FS001, `cli.py:249`, CLI error handler | Errors are printed to stderr and return exit 2. That is existing user-visible reporting, which logger recognition does not model. Verified for configuration and publication failures. |
| FS005, `config.py:108`, TOML parsing | Parse failures propagate to the CLI's `ValueError` handler. This is an ownership review, not evidence that `load_config` needs another log. The direct Python API intentionally propagates errors to its caller. |
| FS005, `diagram.py:393`, JSON serialization | Same conditional serialization concern as CLI JSON output. No new missing-log defect was established. |
| FS001, `scanner.py:349`, annotation parsing fallback | A useful follow-up: malformed string annotations lose receiver information without a specific diagnostic. The resulting call may be unresolved, but a dedicated analysis diagnostic would explain why. An operational ERROR log is not the appropriate default. |
| FS001, `scanner.py:1189`, eligibility failure | The handler appends a structured `source_unreadable` diagnostic. Additional error logging would duplicate the report's existing reporting path. |
| FS001, `scanner.py:1231`, discovery failure | The handler appends `directory_unreadable`, which makes the scan incomplete. Inspected; this specific filesystem branch was not separately fault-injected. |
| FS001, `scanner.py:1250`, entry inspection failure | The handler appends `source_unreadable`. Inspected; this particular branch was not separately fault-injected. |
| FS001, `scanner.py:1368`, source parsing/read failure | The handler appends an input diagnostic and preserves an incomplete result. Malformed source and injected read failure both verified this behavior. |
| FS001, `scanner.py:1391`, module binding recursion | The handler appends `analysis_depth`. Reporting already exists; this exact recursion branch was inspected, not induced in the dogfood run. |
| FS001, `scanner.py:1417`, symbol traversal recursion | The handler appends `analysis_depth` with file and symbol location. Same reporting disposition and test limitation. |

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

The browser step requires a separate Playwright installation and Chromium. The Python step uses only the standard library plus this project. It is included in the CI matrix; the browser step remains separate. Remote CI has not been run during this local validation.

Outputs in `.artifacts/dogfood/` include `self.json`, `self.html`, `self.txt`, `self.mmd`, `validation.json`, `runtime-edges.json`, browser screenshots/SVGs, and `browser-validation.json`. The Python 3.11 run is retained separately in `.artifacts/dogfood-py311/`.

The result supports supervised use of the tested functionality. The eight missing call relationships, unindexed deferred expressions, and alternative-reporting findings remain concrete reasons to retain human review.
