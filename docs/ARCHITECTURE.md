# Public toolkit architecture

The broader claude-os system uses slash commands and specialist agents to guide AI product builds. This document describes the supported public subset; see [the specialist workflow](SPECIALIST-WORKFLOW.md) for the larger product experience and command availability.

The supported execution path is `bin/eval-runner.py`. It reads task Markdown and candidate text from paths provided by the caller, evaluates configured deterministic checks, and prints a report. An optional `--json` path writes that report. Candidate text may contain JSON when a check targets a dotted field.

A task can require subjective judgment. The runner prepares a prompt containing the task, reference, rubric, and candidate. The operator gives that prompt to a separate reviewer, saves the structured verdict, and uses `ingest-verdict` to validate and score it. The runner does not authenticate to a provider, submit a prompt, decide who wrote the candidate, or prove that the reviewer is independent.

## Supported files

- `bin/eval-runner.py`: task loading, deterministic checks, prompt preparation, verdict ingestion, CLI, and built-in regression tests.
- `bin/golden-lint.py`: heuristic checks for evaluation-design defects, with a caller-selected task directory.
- `examples/`: synthetic task/candidate pairs. No personal research corpus or private project records.
- `tests/`: subprocess tests that exercise CLI behavior from a clean working directory.
- `skills/`: optional review instructions with local licenses and provenance.
- `.github/workflows/verify.yml`: automated Python checks. It does not run models or access project secrets.

There are no third-party Python runtime dependencies. Python 3.10 is the minimum supported version for this release; CI checks multiple versions. A live review skill has separate Codex or Claude Code prerequisites documented in its own directory.

## Historical reference material

`bin/audit.py`, `bin/hooks/`, `agents/acceptance-gate.md`, and `assets/` come from the earlier public snapshot. They illustrate the larger personal system and are outside the supported portable runtime. The personal system's global settings, installed library, state, research, automation jobs, and account configuration are not part of this release.

Do not infer that a guard is active because its file exists. Hook installation, tool permissions, private dependencies, and real runtime enforcement need their own integration tests.

## Trust boundaries

Tasks, references, candidates, and model verdicts are data supplied by the operator. The evaluator does not execute candidate text, but task-defined regular expressions can consume excessive CPU. Use trusted task definitions and externally bounded jobs for untrusted material. The optional review skills invoke a separate model runtime, where permission settings and data exposure require deliberate review.

Model prompts and evaluation outputs can contain sensitive information. Use synthetic or explicitly authorized inputs, and inspect reports before sharing them. No claim of a complete sandbox or comprehensive secret detection is made.
