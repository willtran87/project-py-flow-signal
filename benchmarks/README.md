# Accuracy corpus provenance

`manifest.json` defines development/evaluation membership, expected labels, and exact fixture SHA-256 digests. `baseline.json` records the current result, including misses. Run `python scripts/benchmark_accuracy.py --check benchmarks/baseline.json` from the repository root. Python source is parsed only.

## Development

Eight original FlowSignal-authored cases cover the original eight instrumentation rules, known calls, an outer reporting owner, and negative controls. Five original PyCG evaluation cases have moved into development because they were used to guide callable-value implementation: `functions/call`, `functions/imported_call`, `returns/call`, `assignments/tuple`, and `classes/super_class_return`. All nine expected pairs in those five cases now resolve, including the six former misses. These 13 cases are regression tests, not independent validation. Authored instrumentation labels do not establish real-world rule accuracy.

## Evaluation

Twelve fresh fixtures and their original `callgraph.json` labels were copied byte-for-byte from [PyCG](https://github.com/vitsalis/PyCG/tree/8d5dc40837803beef1d8d379fbf2cdad6cd94641/micro-benchmark/snippets), commit `8d5dc40837803beef1d8d379fbf2cdad6cd94641`:

- `args/call`, `args/imported_call`, `args/param_call`
- `kwargs/chained_call`
- `classes/base_class_calls_child`, `classes/imported_nested_attr_access`, `classes/parameter_call`
- `imports/relative_import`, `imports/import_as`
- `functions/assigned_call_lit_param`
- `returns/imported_call`
- `lambdas/parameter_call`

These were selected as a small cross-section of argument/callback propagation, imports, returned callables, inheritance, and lambdas. The implementation was not changed to fit this new evaluation set; unsupported cases remain measured misses. Current results are 14 correct pairs, zero extras, and 11 misses (100% precision, 56% recall). This is not the full PyCG corpus, a random sample, or independent evaluation of observability findings. Upstream labels are not a guarantee of correctness.

PyCG material is redistributed under Apache License 2.0; the complete original license is retained in [corpus/evaluation/pycg/LICENSE](corpus/evaluation/pycg/LICENSE). Original source, labels and accompanying README files are unchanged. Attribution belongs to the PyCG contributors; the FlowSignal manifest and evaluator are separate additions. `.gitattributes` disables newline conversion for fixture bytes so hashes remain stable across Windows/Linux checkouts.

Results compare unique qualified caller/callee pairs. FlowSignal module names have `.<module>` stripped to match upstream labels. Expected pairs are not filtered merely because the scanner did not index or resolve them. No upstream observability labels exist, so those categories are explicitly unscored in evaluation. Results include per-feature call metrics and per-rule authored finding metrics.

## Independent instrumentation review

`corpus/review_pending/workflow` is an authored three-file workflow with storage boundaries, recovery branches, silent consumption, and multiple entrypoints. It is parsed but never executed. The [source-only review packet](instrumentation-review.json) intentionally contains no scanner judgments and no completed labels. Independent review is pending; these cases are not scored as negatives or successes.

Generate a packet with `python scripts/benchmark_accuracy.py --export-review .artifacts/review.json`. A reviewer fills identity, rationale, completion state, findings, and owner labels. Import with `--review-file .artifacts/review.json` to score those labels. The importer verifies source hashes and configuration and rejects missing/incomplete labels. Declared reviewer identity is not authenticated. Do not call self-authored judgments independent.

The baseline gate verifies corpus membership, split, metric membership, and a signature of source/configuration/labels in addition to checking per-case false-positive and false-negative counts. See [workflow analysis and review](../docs/WORKFLOW_ACCURACY.md) for current results and limits. Cases used for tuning must move to development; do not overwrite labels to hide a regression.


## Contextual callable metric

The definitive call metric above is unchanged. The separate `contextual_call_totals` metric in `baseline.json` includes call-site callable-argument candidates: 20 correct pairs, zero extras, five misses (80% recall) on the same 25 evaluation pairs. These edges are contextual possibilities and do not establish universal call targets or reporting coverage. The gate checks both metrics without changing upstream labels. FS009 and FS010 are covered by authored unit/integration cases; they have no independent recommendation labels yet.

## Thirty-scope independent workflow cohort

`workflow-cohort.json` freezes ten selected scopes from each of Flask, Billiard, and Tenacity, covering service, background, and callback/retry workflows. Full Python package trees are included for context under `corpus/workflow_review`, together with original licenses/notices. Exact revisions, configurations, source membership, hashes, workflow scope, development-use status, and the scoring rubric are in the manifest. This is a selected sample, not a random enterprise cohort. The code is parsed only.

Prepare source-only packets with `python scripts/workflow_review.py --packet .artifacts/workflow-review.json`. No actual reviewer labels have been supplied. Pending and abstained cases remain unscored; authored importer test labels are not independent evidence. See the [current guide](../docs/OUTCOME_ANALYSIS.md) for scoring, overlap/adjudication requirements, metric denominators, and limitations.

Pinned upstream sources and retained license locations:

- [flask](https://github.com/pallets/flask/tree/d73fa1cdcbd8b1465c151db8924ba58b1dd14e35) (`d73fa1cdcbd8b1465c151db8924ba58b1dd14e35`); [LICENSE.txt](corpus/workflow_review/flask/LICENSE.txt).
- [billiard](https://github.com/celery/billiard/tree/7fb0abe4eb706b44cd839f3757f47d1a962e78a0) (`7fb0abe4eb706b44cd839f3757f47d1a962e78a0`); [LICENSE.txt](corpus/workflow_review/billiard/LICENSE.txt).
- [tenacity](https://github.com/jd/tenacity/tree/3e58094d3bc414975aad9eadf343a32bdb3b89b3) (`3e58094d3bc414975aad9eadf343a32bdb3b89b3`); [LICENSE](corpus/workflow_review/tenacity/LICENSE).
