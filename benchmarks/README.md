# Accuracy corpus provenance

`manifest.json` defines development/evaluation membership, expected labels, and exact fixture SHA-256 digests. `baseline.json` records the current result, including misses. Run `python scripts/benchmark_accuracy.py --check benchmarks/baseline.json` from the repository root. Python source is parsed only.

## Development

Eight FlowSignal-authored cases cover all eight instrumentation rules, known calls, an outer reporting owner, and negative controls. Five original PyCG evaluation cases have moved into development because they were used to guide callable-value implementation: `functions/call`, `functions/imported_call`, `returns/call`, `assignments/tuple`, and `classes/super_class_return`. All nine expected pairs in those five cases now resolve, including the six former misses. These 13 cases are regression tests, not independent validation. Authored instrumentation labels do not establish real-world rule accuracy.

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
