# Original-tool provenance

The evaluator and task linter were adapted from Sharan Kumar's local claude-os source at commit `b2ba6ba5` (2026-10-19). That repository's original code is MIT-licensed. The release uses selected files, not the private repository's history or complete working tree.

Public changes add explicit input validation, a task-directory argument for the linter, synthetic examples, CLI tests, and portable documentation. The source corpus, local state, settings, credentials, and private research are excluded. Historical comments inside the scripts document their development; they are not independent evidence for model quality or security claims.

The existing historical hooks remain reference-only. Sample machine paths in their self-tests were anonymized for this release; they have not been installed or verified as live controls here.

Third-party review skills have separate licenses and per-directory provenance, collected in [THIRD-PARTY-NOTICES.md](../THIRD-PARTY-NOTICES.md).
