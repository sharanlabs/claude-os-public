# Provenance and public adaptations

- Upstream: [Chase AI, claudex-loop](https://github.com/chaseai-yt/claudex-loop), formerly `grill-me-codex`.
- Pinned source: [ba71f82e8469395d3f7c4ed824334b16676e87a0](https://github.com/chaseai-yt/claudex-loop/tree/ba71f82e8469395d3f7c4ed824334b16676e87a0/skills/grill-me-codex).
- License: MIT, Copyright (c) 2026 Chase AI. The complete upstream license is retained in `LICENSE`.
- Secondary attribution: Matt Pocock's interview/domain-documentation material, MIT; complete notice retained in `THIRD-PARTY-NOTICES.md`.
- Public adaptation: Sharan Kumar / claude-os, 2026-10-21, under the root MIT license for the modifications. This is an adaptation, not an unchanged upstream mirror or an upstream endorsement.

## Changes from the local edition

Removed personal paths, private queue-wrapper dependencies, fixed model/version claims, private directives, account-specific errors, and runtime claims that were not verified for public users. Replaced session resumption with fresh per-round review sessions; each prompt carries prior findings. Added unique evidence directories, explicit incomplete states, privacy boundaries, and limits on what sandboxing and model agreement establish. The interview and plan-review methods remain derived from the attributed sources.

## Verification scope

The pinned upstream license was checked against its public source on 2026-10-21. Command flags were inspected in installed `codex exec --help`; no paid model call or authenticated review was performed as part of this adaptation. These files are instructions for a compatible skill host, not an autonomous runner or a tested end-to-end Codex integration.
