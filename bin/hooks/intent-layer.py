#!/usr/bin/env python3
"""intent-layer — UserPromptSubmit hook making the intent contract PERVASIVE.

The /intent skill (owner NON-NEGOTIABLE, 2026-06-09) converts raw owner
prompts into faithful structured prompts — but it only fires where something
invokes it (step 0 of /claude-os and /enhance, or the session remembering the
CLAUDE.md contract). This hook closes the gap (the reassessment charter's
"intent-layer hardening, e.g. UserPromptSubmit hook", 2026-06-09): every
SUBSTANTIAL plain-English prompt gets a one-line context injection reminding
the session to apply the contract before acting.

A LAYER, NOT A GATE (the skill's own invariant): it never blocks or rewrites
the prompt — it adds context only. Cheap, deterministic, fail-open.

Fires when the prompt is ≥ MIN_CHARS and is NOT a slash command / shell
passthrough (those route themselves). Deliberately simple — a false positive
costs one harmless line; a missed short prompt loses nothing (trivial
one-liners are explicitly out of the intent contract's scope).

Wiring (OWNER ACTION — classifier-gated self-modification; see tasks/flags.md):
  "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python3 \\"$CLAUDE_PROJECT_DIR/bin/hooks/intent-layer.py\\""}]}]

Self-test:  python3 bin/hooks/intent-layer.py --self-test
"""
import sys, json

MIN_CHARS = 150

REMINDER = (
    "[intent-layer] This owner prompt is substantial — apply the /intent contract "
    "(skills/intent/SKILL.md) before acting: extract the core intent + EVERY detail "
    "(convert, never dilute), surface real ambiguities as questions, structure per the "
    "current model's prompting profile (docs/MODEL-ROUTING.md), then proceed. "
    "At intake/stage boundaries state the stage + recommended route (model@effort, "
    "orchestration) from the matrix; on a main-session mismatch worth the switch, emit "
    "the one-line ⎈ handoff prompt — mismatch-only, never nag. A layer, not a gate."
)


def decide(data):
    """Return additionalContext string or None. Pure; type-guarded."""
    if not isinstance(data, dict):
        return None
    prompt = data.get("prompt")
    if not isinstance(prompt, str):
        return None
    p = prompt.strip()
    if len(p) < MIN_CHARS:
        return None
    if p.startswith(("/", "!", "#")):     # slash command / shell passthrough / memory note
        return None
    return REMINDER


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("{}")
        return 0
    try:
        ctx = decide(data)
    except Exception:
        print("{}")
        return 0
    if ctx:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx}}))
    else:
        print("{}")
    return 0


def _self_test():
    long_raw = ("so what i want is the thing we discussed yesterday but also make sure it "
                "covers the other case and dont forget the part about the files being moved "
                "and it should work on any machine not just this one ok")
    cases = [
        ("short prompt -> no-op", {"prompt": "run the tests"}, False),
        ("long raw prompt -> inject", {"prompt": long_raw}, True),
        ("slash command -> no-op", {"prompt": "/claude-os " + long_raw}, False),
        ("shell passthrough -> no-op", {"prompt": "! " + long_raw}, False),
        ("memory note -> no-op", {"prompt": "# " + long_raw}, False),
        ("missing prompt -> no-op", {}, False),
        ("non-string prompt -> no-op", {"prompt": 42}, False),
        ("non-dict -> no-op", [], False),
    ]
    ok = True
    for name, payload, expect in cases:
        got = decide(payload) is not None
        if got != expect:
            ok = False
        print(f"  [{'OK ' if got == expect else 'FAIL'}] {name}: inject={got} (exp {expect})")
    import subprocess, os
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       input="not json", capture_output=True, text=True)
    fo = p.stdout.strip() == "{}"
    print(f"  [{'OK ' if fo else 'FAIL'}] fail-open on malformed stdin -> '{p.stdout.strip()}'")
    ok = ok and fo
    print(f"\nSELF-TEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
