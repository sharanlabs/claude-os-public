# Security and data boundaries

The supported local evaluator makes no network or model calls and does not execute candidate text. It reads caller-selected files and may write a report to the `--json` path supplied by the caller. Only use output paths you intend to create or overwrite.

Task definitions are trusted configuration. Regular expressions supplied by a task can be computationally expensive. Run untrusted task definitions in a separately constrained environment with time and resource limits. This toolkit is not a sandbox.

Review skills run through a separate agent runtime. Read their prompts and permission requirements before use. Do not include credentials, production configuration, personal records, or unauthorized source code in model context. A read-only label does not prove that an agent has no network access.

The legacy `bin/hooks/` scripts and owner-specific audit are reference material. They are not installed or verified as active security controls by this release.

## Reporting a problem

For a vulnerability, use the repository's private vulnerability reporting feature **if available**. Otherwise, open an issue requesting a private contact channel, without exploit payloads, sensitive logs, or credentials. For ordinary scoring bugs, a minimal synthetic public reproduction is welcome.

Do not post actual tokens or private data to demonstrate a problem. Describe the affected component, expected boundary, observed behavior, and a harmless reproduction.

## Release review

Public releases review explicit file lists, license provenance, personal-machine references, and credential patterns before publication. Such review reduces risk; it does not prove the absence of secrets or vulnerabilities. CI validates covered software behavior, not security certification or model reliability.
