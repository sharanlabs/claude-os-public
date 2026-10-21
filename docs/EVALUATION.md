# What the evaluation proves

The local examples test the evaluator's mechanics. They answer questions such as: does a wrong structured decision fail; does an over-budget supplied value fail; does a missing required judgment remain incomplete?

They do not ask a model to find a bug in a real pull request. A fixture that contains a known finding demonstrates a scoring rule, not defect discovery. Do not report synthetic pass/fail counts as model accuracy, adoption, or maintenance-time savings.

## Evidence layers

1. **Deterministic checks:** the runner evaluates the checks you supplied. Weak checks can reward an incorrect answer. Pair every important check with a correct output and a deliberately incorrect output that differs in the relevant property.
2. **Judge assessment:** a separate reviewer scores the task's rubric. Ingestion checks score structure, rubric coverage, and scoring rules. It does not make subjective judgment objective or verify the reviewer's identity.
3. **Runtime evidence:** actual tests, builds, or reproductions belong alongside the review. A fluent verdict cannot replace them.
4. **Human acceptance:** a maintainer decides what to merge and publish. These tools do not approve changes automatically.

A run with pending subjective judgment exits 2 even when its deterministic checks pass. Verdict ingestion is a separate command/report; maintain both records. A complete workflow should combine deterministic outcomes, all required judgments, and runtime evidence before acceptance.

## Task and candidate paths

Task IDs must start with an ASCII letter or digit and contain only ASCII letters, digits, underscores, and hyphens. Dots, spaces, separators, and duplicate IDs within a suite are rejected. Candidate files are resolved beneath the supplied candidate directory; symlinks that escape it are rejected. These checks reduce unintended reads; task definitions and regexes still require trust.

## Metrics and limits

- Usage fields such as tokens and latency are supplied by the candidate producer. The evaluator records/checks them; it does not measure a provider call.
- Regex and substring checks are proxies. The task linter catches some known weak designs, not all of them.
- The lightweight frontmatter parser supports flat fields; it is not a general YAML parser.
- Final-output checks do not prove the safety or quality of the process that produced an answer.
- No live provider integration, model comparison, external pilot, or production security audit is included in this release.

## Planned real-review evaluation

Before claiming effectiveness, create a consented set of real or realistically seeded code changes with human-established expected findings and clean controls. Separate development cases from held-out cases. Record model/version, prompt, relevant settings, run date, finding-matching rules, false positives, missed defects, latency, and provider-reported usage. Review ambiguous findings manually. Publish reproducible cases and limitations without exposing private code.

The planned Codex integration would generate candidate reviews for this process. It is future work; the current runner consumes outputs supplied by a person or another tool.
