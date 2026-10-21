# Curated review skills

Three MIT-licensed adaptations of Chase AI's review skills are included, with the upstream licenses, Matt Pocock notices, exact source revision, and a change record in each directory.

| Skill | Start here when |
|---|---|
| [codex-review](codex-review/SKILL.md) | A substantial plan already exists and needs critique. |
| [grill-me-codex](grill-me-codex/SKILL.md) | Material requirements or design choices remain unresolved. |
| [grill-with-docs-codex](grill-with-docs-codex/SKILL.md) | Domain terminology and architecture decisions must stay aligned. |

These are Claude-hosted planning workflows with Codex as the intended separate critic. They do not make Codex's own review independent, and they do not provide API automation. They need a compatible skill host, Codex CLI, authenticated usage, and a user-authorized review context. CLI help was checked; a live authenticated review remains unverified.

## Use without changing global configuration

Open the selected `SKILL.md` in your host and explicitly ask it to follow that workflow for your project. Keep the complete skill directory available so relative references, licenses, and notices remain together. If you install it through your host's skill-discovery mechanism, use that host's current installation instructions; this repository does not install files globally or edit your runtime configuration.

Read the commands and privacy boundary before granting tool access. Review files and transcripts may contain sensitive context; keep `.codex-review/` local unless you have inspected and authorized its contents for publication. An approval from a model is advisory; tests, code review, and the user's authorization still govern implementation and release.

## Updating

Each `PROVENANCE.md` names the pinned upstream revision. Compare against upstream deliberately and retain all license notices when redistributing. The full personal skill library is not included, and the root MIT license does not relicense third-party content.
