# Implementation research and improvement priorities

**Follow-up implemented:** [reporting contracts, supported property reads, and structural baselines](REVIEW_WORKFLOW.md). The measurements below describe the preceding research increment; the follow-up guide explains the current behavior and remaining limits.

This review examined selected implementations in six related projects and applied three bounded improvements to FlowSignal. The useful combination is call analysis, instrumentation semantics, and explicit uncertainty. None of the inspected components provides evidence that arbitrary enterprise Python execution can be recovered completely.

The review is source-based, not an end-to-end competitor benchmark or a complete audit of each repository. Revisions are pinned below. No upstream implementation was copied, vendored, or added as a runtime dependency; FlowSignal remains an original, standard-library-only implementation. License observations refer to the inspected repositories, not every associated product or rules collection.

## What the implementations teach us

| Project / inspected revision | Implementation inspected | Useful idea and decision |
| --- | --- | --- |
| Pyan3 — `456fa0ae0b633fcd98bea28ad0b19daa2b37e337`; GPL-2.0 | [`pyan/analyzer.py`](https://github.com/Technologicat/pyan/blob/456fa0ae0b633fcd98bea28ad0b19daa2b37e337/pyan/analyzer.py): `visit_Call`, annotation binding, attribute resolution | Class construction can contribute an initializer relationship. Parameter annotations provide receiver evidence. Adopt explicit initializer candidates; preserve uncertainty rather than guessing a union arm. Pyan's inspected annotation resolver deliberately leaves optional/union/string annotations unresolved; our nullable handling is a separately designed extension. |
| PyCG — `8d5dc40837803beef1d8d379fbf2cdad6cd94641`; Apache-2.0 | [`definitions.py`](https://github.com/vitsalis/PyCG/blob/8d5dc40837803beef1d8d379fbf2cdad6cd94641/pycg/machinery/definitions.py), [`cgprocessor.py`](https://github.com/vitsalis/PyCG/blob/8d5dc40837803beef1d8d379fbf2cdad6cd94641/pycg/processing/cgprocessor.py) | Assignment relationships and transitive closure are a stronger foundation for aliases and higher-order calls than spelling-based guesses. Adopt a narrow same-type binding improvement now; defer a general points-to engine. The upstream repository is archived, so it is an architectural reference rather than a new dependency. |
| code2flow — `c2c22afe5e12f969cc256373bf8f4eec592dc762`; MIT | [`engine.py`](https://github.com/scottrogowski/code2flow/blob/c2c22afe5e12f969cc256373bf8f4eec592dc762/code2flow/engine.py): `_find_link_for_call` | It distinguishes unique candidates, ambiguity, unknown external modules, and constructors. Retain explicit ambiguity. Do not adopt global method-name matching for instrumentation coverage: a plausible name match could incorrectly suppress a missing-instrumentation finding. |
| Ruff — `660350be2648e60e0c241e24a6ed05a38b4098fa`; MIT | [`TRY400`](https://github.com/astral-sh/ruff/blob/660350be2648e60e0c241e24a6ed05a38b4098fa/crates/ruff_linter/src/rules/tryceratops/rules/error_instead_of_exception.rs), [`logging.rs`](https://github.com/astral-sh/ruff/blob/660350be2648e60e0c241e24a6ed05a38b4098fa/crates/ruff_python_semantic/src/analyze/logging.rs) | Resolve logging identities semantically, recognize `sys.exc_info()`, and distinguish known logging APIs from logger-like names. Adopt the traceback case with alias and shadowing controls. Do not automatically rewrite log levels or assume an arbitrary `.error()` is logging. |
| Semgrep — `0516c0f23a3dceac5c8f5ff3fecd402af4450182`; LGPL-2.1 core | [`cli/src/semgrep/test.py`](https://github.com/semgrep/semgrep/blob/0516c0f23a3dceac5c8f5ff3fecd402af4450182/cli/src/semgrep/test.py): expected/reported line comparison; [rule-testing documentation](https://semgrep.dev/docs/writing-rules/testing-rules) | Validate exact expected findings and false-positive controls together. Apply that discipline to the new receiver, constructor, and traceback fixtures. Keep our existing unittest harness; adding a rule language or Semgrep dependency is unnecessary for this increment. |
| OpenTelemetry Python — `b1f499beb552f7a3fbb4e07a2e436b7846a4e6b7`; Apache-2.0 | [`trace/__init__.py`](https://github.com/open-telemetry/opentelemetry-python/blob/b1f499beb552f7a3fbb4e07a2e436b7846a4e6b7/opentelemetry-api/src/opentelemetry/trace/__init__.py): `use_span` | Exception recording, error status, and active recording are separate conditions. Escaping `Exception` instances are handled; arbitrary `BaseException` subclasses are not. This supports retaining FlowSignal's distinction between trace presence and potential failure coverage. Exporter delivery and sampling still require runtime evidence. |

Errlog and LogAdvisor, discussed in the earlier landscape review, are research references rather than source implementations inspected in this pass. No claim about their code quality, licensing, or comparative accuracy follows from this review. Learned logging placement remains outside this deterministic increment.

## Implemented changes

### Explicit initializer candidates

A known class call can now connect to that class's explicit synchronous `__init__`. Import aliases and module-qualified class names are supported. The call retains its source expression and class name, with resolution `inferred_constructor`; diagrams label the edge **possible initializer**.

Class/import references are tracked separately from instance receiver types. Consequently, `worker = Worker(); worker()` does not invent a second initializer edge. Shadowed class names, duplicate definitions, decorators, explicit metaclass/other class keywords, a local custom `__new__`, decorated initializers, and async initializers prevent this inference or produce ambiguity.

This is a possible relationship, not a complete model of class construction. Inherited initializers, inherited metaclasses or allocation hooks, monkey-patching, and dynamic replacement remain limitations. A class with an explicit initializer may have external bases; the initializer relationship remains inferred. If that initializer executes and raises during an ordinary synchronous construction, the caller's reporting handler can own that failure.

### Nullable receiver annotations and agreement across assignments

`Client | None`, `Optional[Client]`, `Union[Client, None]`, and quoted equivalents can retain the one concrete receiver type. Imported typing aliases are resolved; arbitrary lookalike wrappers are not. This permits `client = client or Client(); client.send()` to retain a possible `Client.send` edge.

Multiple known bindings must agree. A different concrete union arm, an unsupported annotation arm, an unknown fallback, or conflicting reassignment keeps the receiver unresolved. Variadic parameter annotations describe the elements, not the tuple/dict receiver, so they no longer create misleading method edges.

An annotation remains an assumption. These edges do not prove that a receiver is non-null or that callers obey annotations. The implementation does not add general flow-sensitive narrowing, return-type inference, container-element inference, or callback propagation.

### Existing traceback context

An import-resolved, argument-free `sys.exc_info()` supplied to `exc_info` now counts as explicit traceback context, including import aliases. FS006 no longer recommends adding context already present in this form. Shadowed names, similarly named unrelated APIs, unknown values, and malformed calls retain the recommendation. Severity still depends on the operational outcome; this change does not choose INFO, WARNING, or ERROR from syntax alone.

## Validation and practical effect

- **112 tests:** 111 passed and one Windows symlink-capability skip on both Python 3.11.4 and 3.14.5. Fourteen new test methods contain positive and negative subcases for the changes above.
- **Self-dogfood:** the previous scan missed eight observed call pairs between indexed symbols. The updated source's execution had 111 such pairs, 109 present in its static graph and two missing. Six of the original eight omissions are now present: four initializer relationships and two `Config` receiver calls. Source changes also changed the denominator; this is not a same-corpus precision/recall score.
- **Regression enforcement:** the dogfood harness now requires those six relationships, alongside the original five core relationships, to appear in execution, static facts, and the generated diagram.
- **Remaining observed omissions:** both are `Handler.catches_all` property accesses. Another 22 observed pairs involve unindexed expressions, mainly generators/lambdas. The scanner's 12 self-review candidates remain; this work does not suppress alternative-reporting findings merely to produce a clean report.
- **Scale fixture:** 1,000 framework-neutral files, 8,000 symbols, 7,500 calls; exactly 500 expected FS005 findings, zero unresolved calls and zero diagnostics. One local concurrent run took 4.127 seconds. This checks that workload, not enterprise-wide accuracy or a performance improvement.

See [DOGFOOD.md](DOGFOOD.md) for failure injection, report checks, browser reproduction, and the review of recommendations. The reviewed external tools were not executed against a common labeled corpus, so no comparative accuracy or speed ranking is claimed.

## Next improvements, in priority order

1. **Receiver provenance and implicit execution.** Add explicit class/instance/module provenance throughout the model, then property/descriptor access and context-manager enter/exit relationships. Distinguish these edge kinds in reports. Acceptance should include class-level property access, shadowing, overridden descriptors, and container-derived receivers; the remaining self-dogfood property cases require more than simply recognizing `@property`.
2. **Bounded callback and return-value analysis.** Build a small multi-target points-to analysis inspired by assignment-graph approaches. Use a fixed-point work budget, cap candidate sets, and emit uncertainty when widening or truncating. Never count an ambiguous target's logger as guaranteed coverage. Validate higher-order functions, recursion, decorators, and conflicting assignments with a labeled corpus before enabling suppression based on these edges.
3. **Reporting-owner configuration beyond logging.** Model structured diagnostics, error-return contracts, and CLI stderr separately from log events. Require explicit configuration and execution/propagation conditions. Our self-scan demonstrates why an ordinary `.append()` cannot automatically mean a failure was reported.
4. **Enterprise review continuity.** Add diff baselines, reviewed suppressions with reasons/expiry, and SARIF export. Stable identities need to survive harmless line movement. Validate renamed symbols and changed rules before using baselines as a CI gate.
5. **Runtime corroboration.** Import a documented trace/edge format and annotate observed paths separately from inferred paths. Missing runtime evidence must not erase a static candidate. Sampling, disabled exporters, and untested branches prevent traces from proving full observability coverage.

The next release should remain suitable for supervised advisory scanning. These improvements strengthen useful evidence; independent accuracy calibration, hard worker resource isolation, and broader Python dispatch handling remain open enterprise-readiness work.
