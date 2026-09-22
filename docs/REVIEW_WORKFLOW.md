# Reporting owners, property paths, and repeatable reviews

Latest update: [accuracy and review](ACCURACY_AND_REVIEW.md) implements the six P1/P2 enhancements, including resolution of the four indexed dogfood omissions, SARIF, dismissal lifecycle, and runtime import. Implementation and validation statements below describe their earlier increment.

The newer [typed-flow and coverage increment](TYPED_FLOW_AND_COVERAGE.md) adds receiver/helper-return inference, source-linked coverage explanations and updated validation. Measurements below remain historical evidence for their stated revision.

This increment implements the three recommended product enhancements: explicit non-log reporting recognition, supported property execution, and structural scan baselines. It retains deterministic, framework-neutral analysis without importing or executing target code.

## Declare reporting contracts

Recognition is opt-in. The scanner does not interpret an arbitrary `print()`, `.append()`, or error-shaped return as adequate reporting.

```toml
[flowsignal]

[[flowsignal.reporters]]
pattern = "myapp.diagnostics.publish"
kind = "diagnostic"
owner = "Workflow diagnostic report"

[[flowsignal.reporters]]
pattern = "builtins.print"
scope = "myapp.cli.main"
kind = "stderr"
owner = "CLI caller"

[[flowsignal.reporters]]
pattern = "myapp.results.Failure"
kind = "error_return"
owner = "Result consumer"
```

`pattern` matches the import-resolved API name by default; aliases are supported and unresolved or shadowed names do not match. `scope` optionally restricts the qualified calling symbol using a glob. `owner` names the consumer responsible for the outcome. These declarations are project assertions, not scanner-verified delivery guarantees.

- **diagnostic:** the matched API publishes an outcome through the declared reporting channel.
- **stderr:** only recognized `print(..., file=sys.stderr)` or `sys.stderr.write(...)` forms count. Stdout and unknown/shadowed stderr objects do not count. Configure the corresponding pattern for each form you use.
- **error_return:** the configured constructor/call must be directly returned, as in `return Failure(...)`. Creating an object and discarding it does not count. The declaration asserts that callers consume that error contract; consumption is not proven.

For project-specific storage whose receiver cannot be resolved, an explicitly scoped expression contract is available:

```toml
[[flowsignal.reporters]]
pattern = "analysis.diagnostics.append"
match = "expression"
scope = "myapp.scanner.*"
kind = "diagnostic"
owner = "Published scan report"
```

Expression matching requires a restricted symbol scope and is labeled as a configured expression contract. It intentionally relies on the reviewed project convention; it does not prove that the receiver is the expected object. Prefer a resolved API where possible.

An unconditional, immediate reporter in an exception handler can satisfy FS001 and participate in FS005's existing handler/caller coverage checks. Conditional calls, deferred coroutine calls, partial typed-handler coverage, and unknown exception-suppressing contexts remain conservative. A reporter outside a handler is displayed as evidence but is not credited as exception coverage. Existing log traceback recommendations remain independent.

JSON contains separate `reporting_signals`, including source, kind, owner, recognition basis, and conditionality. Text and diagrams show the reporting owner without relabeling it as a log or inventing a severity. The checked [self-review configuration](../examples/self-review.toml) illustrates this project's structured diagnostics and CLI stderr contracts:

```text
flowsignal scan src --config examples/self-review.toml --format html --output self-review.html
```

## Inspect implicit property execution

Supported `instance.property` reads produce call facts with `resolution=inferred_property` and `execution=implicit_property`. Diagram edges say **property getter**. A getter's failure-sensitive calls can be connected to the handler surrounding the property read.

Receiver evidence includes annotated arguments, known local construction, simple instance aliases, same-type fallbacks, annotated class fields, dictionary/list element access, and dictionary `.values()` iteration. This closes both original `Handler.catches_all` omissions in FlowSignal's self-dogfood run.

The implementation intentionally leaves unsupported cases unresolved: class-level property access, unknown or conflicting receivers, slice results, shadowed variables, customized class construction/attribute access, inherited property lookup, setter/deleter definitions that create duplicate symbol candidates, and arbitrary descriptors. A callable returned by a property creates a getter edge plus an unresolved subsequent call; it does not invoke the getter twice. Annotations are assumptions and do not prove that runtime values obey them.

The self-dogfood harness now requires thirteen observed relationships, including the two property reads, in the static facts and diagram. New analysis code also has unresolved runtime-observed relationships, so closing these two omissions is not a claim of complete self-coverage.

## Save and compare baselines

```text
flowsignal scan ./my-project --save-baseline baseline.json
flowsignal scan ./my-project --baseline baseline.json --format json --output current.json
flowsignal scan ./my-project --baseline baseline.json --format html --output current.html
```

The separate `flowsignal-baseline-1` document stores structural fingerprints, rule/symbol identity, source location, review priority, scope settings, and review decisions. Ordinary report IDs remain location-based for existing consumers; the new `fingerprint` is the baseline matching key. Fingerprints hash normalized AST structure and recommendation semantics, without exporting the underlying source expressions.

The comparison identifies **new**, **unchanged**, and **resolved** occurrences. Identical findings are counted as occurrences, so adding or removing a duplicate changes the counts. An incomplete scan marks absent baseline entries **unverified**, never resolved. A scan requested with `--save-baseline` must be complete; otherwise the command returns 2 before publishing either requested output, preserving existing files.

Exclusions, import roots, entrypoints, reporter/logger/boundary recognition, lifecycle review, and confidence filters must match the baseline. Source omission and work-budget values may differ; incomplete status still prevents false resolution. Renames, changed AST structure or recommendations, ambiguous duplicate definitions with line-qualified identities, and fingerprint-version changes can require a new review. Matching fingerprints do not establish unchanged runtime behavior.

## Record and retain decisions

```text
flowsignal review dismiss baseline.json FINDING_FINGERPRINT --reason "Reviewed: the caller owns this outcome"
flowsignal scan ./my-project --baseline baseline.json --fail-on medium
flowsignal review restore baseline.json FINDING_FINGERPRINT
```

Dismissal requires a nonempty reason. It applies to matching occurrences in the saved baseline, survives supported line movement, and remains visible in JSON, text, HTML, and Mermaid. It is excluded from `--fail-on` thresholds. Restoring a finding removes the dismissal. Newly added unmatched occurrences remain active. Multiple indistinguishable occurrences are paired in report order; the baseline does not track statement identity beyond its fingerprint and occurrence count.

To gate only new active findings:

```text
flowsignal scan ./my-project --baseline baseline.json --fail-on medium --fail-on-new
```

This requires `--baseline`; exit 1 means a new active finding met the threshold. Incomplete scans still return 2. A default `--fail-on none` never produces a finding-threshold failure, even with `--fail-on-new`.

To promote a reviewed comparison while retaining decisions:

```text
flowsignal scan ./my-project --baseline baseline.json --save-baseline next-baseline.json
```

The baseline can be committed to the target project's repository and reviewed like configuration. Report output cannot overwrite baseline input or the active configuration. A baseline and report must use distinct output paths. Each output is atomically replaced, but publishing both is not a multi-file transaction. Dismissals have no expiry or reviewer authentication in this version; those remain follow-up work.

## Reproduce the end-to-end demonstration

```text
python -m unittest discover -s tests
python scripts/dogfood.py
python scripts/validate_review_workflow.py
node scripts/check_review_browser.cjs .artifacts/review-workflow
```

The demonstration uses only authored source as scan input; it never executes that input or its placeholder external APIs. It verifies a configured reporting owner and a property getter, moves source lines, retains a reasoned dismissal, removes one finding, introduces another, and requires the new-finding gate to return 1. It writes HTML, JSON, text, Mermaid, a baseline, and machine-readable validation under `.artifacts/review-workflow/`.

The Python demonstration runs in the Windows/Linux, Python 3.11/3.14 CI matrix. Browser checks require a separate Playwright/Chromium installation and verify 2560×1600, 1440×1000, and 390×844 viewports. They check reporting ownership, property-edge metadata, visible baseline counts and reasons, keyboard selection, layout overflow and node overlap, and absence of remote requests or JavaScript errors.

## Recorded local validation

| Check | Observed result |
| --- | --- |
| Unit and regression suite, Python 3.11.4 and 3.14.5 | 134 passed, one Windows symlink-capability skip per interpreter; 23 new review-workflow tests |
| Authored CLI review workflow, both interpreters | One new, one unchanged, one resolved and one dismissed finding; dismissal survived line movement; new-finding threshold returned 1 |
| Self-dogfood, both interpreters | Thirteen required relationships corroborated in execution, static facts and diagrams; failure-injection checks passed |
| Python 3.14 self-scan | 11 files, 132 symbols, 1,221 calls; 268 resolved internal call sites, 277 unresolved sites, 19 candidates and 66 diagnostics |
| Python 3.14 observed pairs between indexed symbols | 148 of 157 pairs present; nine missing pairs in new receiver-analysis code, with another 29 observed pairs involving unindexed expressions |
| Self-scan with the explicit self-review contracts | 17 reporting-signal sites recognized; candidates reduced from 19 to 12 through seven FS001 handlers with declared reporting owners |
| 1,000-file synthetic workflow | 8,000 symbols, 7,500 calls, exactly 500 expected FS005 findings, no unresolved calls or diagnostics; 4.296 seconds in one local run |
| Browser checks | Review demonstration and self-report passed at 2560×1600, 1440×1000, and 390×844; high-resolution and mobile screenshots inspected |
| Distribution | Wheel built and installed in a separate environment; installed CLI created and compared baselines and generated HTML successfully |

These are local Windows results. The CI workflow includes the new Python demonstration, but remote CI for this increment has not run. The self-comparison is a single observed execution, not whole-program precision/recall. The nine remaining observed omissions include calls through `ReceiverScope`/`ReceiverIndex` members and values returned from helper methods. Neither those omissions nor the remaining recommendation candidates were hidden to obtain a clean result.

Independent accuracy calibration, callback/return-value analysis, hard worker memory/time limits, and SARIF remain outside this increment.
