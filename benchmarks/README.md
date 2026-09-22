# Accuracy corpus provenance

`manifest.json` defines development/evaluation membership, expected labels, and exact fixture SHA-256 digests. `baseline.json` records the current result, including misses. Run `python scripts/benchmark_accuracy.py --check benchmarks/baseline.json` from the repository root. Python source is parsed only.

## Development

Eight FlowSignal-authored cases cover all eight instrumentation rules, known calls, an outer reporting owner, and negative controls. They are regression tests, not independent validation. Labels were written for the intended behavior of each small fixture. They do not establish real-world rule accuracy.

## Evaluation

Five fixtures and their original `callgraph.json` labels were copied byte-for-byte from [PyCG](https://github.com/vitsalis/PyCG/tree/8d5dc40837803beef1d8d379fbf2cdad6cd94641/micro-benchmark/snippets), commit `8d5dc40837803beef1d8d379fbf2cdad6cd94641`:

- `functions/call`: direct call
- `functions/imported_call`: imported callable alias
- `returns/call`: returned callable
- `assignments/tuple`: callable tuple assignment
- `classes/super_class_return`: inherited returned bound method

These were selected as a small cross-section of direct calls, imports, higher-order behavior, tuple bindings, and inheritance. The current implementation was not changed to fit their labels; unsupported cases remain measured misses. This is not the full PyCG corpus, a random sample, or independent evaluation of observability findings. Upstream labels are not a guarantee of correctness.

PyCG material is redistributed under Apache License 2.0; the complete original license is retained in [corpus/evaluation/pycg/LICENSE](corpus/evaluation/pycg/LICENSE). Original source, labels and accompanying README files are unchanged. Attribution belongs to the PyCG contributors; the FlowSignal manifest and evaluator are separate additions. `.gitattributes` disables newline conversion for fixture bytes so hashes remain stable across Windows/Linux checkouts.

Results compare unique qualified caller/callee pairs. FlowSignal module names have `.<module>` stripped to match upstream labels. Expected pairs are not filtered merely because the scanner did not index or resolve them. There are no implicit-constructor exclusions in these five cases. No upstream observability labels exist, so those categories are explicitly unscored in evaluation.

See [accuracy and review guide](../docs/ACCURACY_AND_REVIEW.md) for metrics and remaining limitations. New independent samples should remain held out until evaluated; do not overwrite benchmark expectations to hide a regression.
