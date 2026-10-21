# Operating principles

These principles explain the project's design choices. They are not a claim that every rule is automatically enforced by this public toolkit.

1. **Preserve user work.** Review changes and keep reversible local changes reversible.
2. **Check current evidence.** Verify changing technical facts against the relevant source.
3. **Keep scope small.** Reuse useful components before adding more machinery.
4. **Publish deliberately.** Review private data, attribution, and redistribution rights before release.
5. **Show provenance.** Identify sources, modifications, and the evidence behind a result.
6. **Keep orchestration legible.** Make responsibilities and handoffs explicit.
7. **Separate creation from assessment.** Use an independent reviewer for consequential work; a different model can provide another perspective, but is not proof of correctness.
8. **Measure cost honestly.** Distinguish measured provider usage from supplied or estimated metrics.
9. **Make focused changes.** Avoid unrelated refactoring.
10. **Fix causes.** Reproduce failures and add a useful regression case.
11. **Respect trust boundaries.** Untrusted context, private data, network access, and write permissions require deliberate separation and runtime controls.
12. **Teach the workflow.** A fresh user should be able to run an example and understand its limits.

The public evaluator implements checks and verdict validation described in the [evaluation guide](EVALUATION.md). The historical audit and hooks illustrate the personal system's approach; their presence does not establish enforcement in a public clone.
