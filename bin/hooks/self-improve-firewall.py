#!/usr/bin/env python3
"""self-improve-firewall — PreToolUse hook: the recursive-self-improvement wall.

The autonomous self-improvement engine (docs/SELF-IMPROVEMENT-ENGINE.md) may widen
its autonomy LOCALLY — but it must never be able to edit its own brakes. A loop that
can rewrite its own executor, hooks, gate, Laws, or kill-switch can disable the very
guards that make it safe. This hook hard-denies that, mechanically.

KEY PROPERTY — DORMANT unless an autonomous pass is active:
  - A normal owner/dev session edits audit.py, hooks, NORTH-STAR freely (this hook
    returns allow). Nothing changes for human-driven work.
  - The firewall fires ONLY while an autonomous self-improve pass is active, signalled by
    the sentinel  tasks/.auto-improve-active  (the executor creates it at pass start,
    removes it at pass end) OR by agent_type ∈ EXECUTOR_AGENTS. Then edits to PROTECTED
    paths are denied, and the loop may not `git push` (public is owner-only) or leave its
    auto/self-improve branch.

HONEST SCOPE (mirrors trifecta-guard's discipline):
  - Edit/Write/NotebookEdit are screened RELIABLY (the path is in the tool input).
  - Bash is BEST-EFFORT: obvious mutating commands touching a protected path, plus
    `git push`/merge/checkout-main, are caught; arbitrary Bash can construct a path the
    scanner never sees. The real boundary for a future sub-agent executor is its toolset
    (no Bash / no push) — this hook is defence-in-depth for the main-session driver.
  - FAIL-OPEN on unparseable input (a crashing safety hook must not block all work).
  - v1 protects the JUDGE agents + safety files wholesale; finer-grained agent-frontmatter
    (identity/tools-only) protection is a v2 refinement (tasks/todo.md).

Self-test:  python3 bin/hooks/self-improve-firewall.py --self-test
"""
import sys, os, json, re

HERE = os.path.dirname(os.path.abspath(__file__))         # bin/hooks
ROOT = os.path.dirname(os.path.dirname(HERE))             # repo root (hook is two levels deep: bin/hooks/<file>)
SENTINEL = os.path.join(ROOT, "tasks", ".auto-improve-active")
KILLSWITCH = os.path.join(ROOT, "tasks", ".auto-improve-disabled")
EXECUTOR_AGENTS = {"self-improve-executor"}       # a future sub-agent driver, if used

# Paths the autonomous loop may NEVER mutate (its own brakes). Relative to repo root.
PROTECTED_REL = [
    "bin/hooks",                       # all hooks, incl. THIS firewall
    "bin/audit.py",                    # the deterministic checks
    "bin/coverage-ledger.py",          # the coverage gate
    "bin/snapshot.py",                 # the rewind mechanism
    "bin/registry-check.py",
    ".claude/settings.json",           # hook wiring / kill-switch surface
    ".claude/settings.local.json",
    "docs/NORTH-STAR.md",              # the 12 Laws
    "agents/acceptance-gate.md",       # the gate (judge)
    "agents/claude-os-auditor.md",     # the system auditor (judge)
    "agents/utilization-coordinator.md",
    "agents/guidelines-monitor.md",
    "tasks/.auto-improve-active",      # the sentinel itself (loop can't extend its own pass)
    "tasks/.auto-improve-disabled",    # the kill-switch (loop can't delete its own off-switch)
]
PROTECTED_ABS = [os.path.join(ROOT, p) for p in PROTECTED_REL]

_MUTATING_BASH = re.compile(
    r"(^|[|&;]|\s)(rm|mv|truncate|dd|tee|shred)\s|>{1,2}\s*\S|sed\s+-i|"
    r"\bcp\b[^|;]*\s\S", re.I)


def _real(p):
    try:
        return os.path.realpath(p)
    except Exception:
        return os.path.abspath(p)


def _rel(p):
    try:
        return os.path.relpath(p, ROOT)
    except Exception:
        return p


def _is_protected(fp):
    if not isinstance(fp, str) or not fp:
        return False
    rp = _real(fp if os.path.isabs(fp) else os.path.join(ROOT, fp))
    for prot in PROTECTED_ABS:
        pr = _real(prot)
        if rp == pr or rp.startswith(pr + os.sep):
            return True
    return False


def _pass_active(data):
    """An autonomous self-improve pass is mutating the repo right now?"""
    if os.path.exists(SENTINEL):
        return True
    at = data.get("agent_type") if isinstance(data, dict) else None
    return at in EXECUTOR_AGENTS


def decide(data, pass_active=None):
    """Return (deny: bool, reason: str). Dormant unless a pass is active."""
    if not isinstance(data, dict):
        return (False, "")
    if pass_active is None:
        pass_active = _pass_active(data)
    if not pass_active:
        return (False, "")                       # normal owner/dev session — firewall dormant

    tool = data.get("tool_name")
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return (False, "")

    if tool in ("Edit", "Write", "NotebookEdit"):
        fp = ti.get("file_path") or ti.get("notebook_path")
        if _is_protected(fp):
            return (True, f"self-modification firewall: an autonomous self-improve pass may not edit a "
                          f"protected path ({_rel(fp) if isinstance(fp, str) else fp}) — the loop's own "
                          f"brakes (hooks/audit/gate/Laws/settings/kill-switch). Allowed only in a normal "
                          f"owner session.")
        return (False, "")

    if tool == "Bash":
        cmd = ti.get("command")
        if not isinstance(cmd, str):
            return (False, "")
        if re.search(r"\bgit\s+push\b", cmd):
            return (True, "self-modification firewall: the autonomous loop may not `git push` — promotion "
                          "to the public repo is owner-only.")
        if (re.search(r"\bgit\s+(merge|rebase)\b", cmd)
                or re.search(r"\bgit\s+(checkout|switch)\s+(main|master)\b", cmd)):
            return (True, "self-modification firewall: the autonomous loop stays on auto/self-improve — no "
                          "merge / checkout to main.")
        if _MUTATING_BASH.search(cmd):
            for prel in PROTECTED_REL:
                if prel in cmd:
                    return (True, f"self-modification firewall: an autonomous pass may not mutate a protected "
                                  f"path ({prel}) via Bash.")
        return (False, "")

    return (False, "")


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("{}")
        return 0
    try:
        deny, reason = decide(data)
    except Exception:
        print("{}")
        return 0
    if deny:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
        return 0
    print("{}")
    return 0


def _self_test():
    aud = os.path.join(ROOT, "bin", "audit.py")
    hook = os.path.join(ROOT, "bin", "hooks", "trifecta-guard.py")
    laws = os.path.join(ROOT, "docs", "NORTH-STAR.md")
    gate = os.path.join(ROOT, "agents", "acceptance-gate.md")
    normal = os.path.join(ROOT, "tasks", "improvement-queue.md")
    cases = [
        # name, payload, pass_active, expect-deny
        ("PASS active: edit audit.py", {"tool_name": "Edit", "tool_input": {"file_path": aud}}, True, True),
        ("PASS active: edit a hook", {"tool_name": "Write", "tool_input": {"file_path": hook}}, True, True),
        ("PASS active: edit the Laws", {"tool_name": "Edit", "tool_input": {"file_path": laws}}, True, True),
        ("PASS active: edit the gate", {"tool_name": "Edit", "tool_input": {"file_path": gate}}, True, True),
        ("PASS active: edit a normal queue file", {"tool_name": "Write", "tool_input": {"file_path": normal}}, True, False),
        ("PASS active: git push (public wall)", {"tool_name": "Bash", "tool_input": {"command": "git push origin auto/self-improve"}}, True, True),
        ("PASS active: git checkout main", {"tool_name": "Bash", "tool_input": {"command": "git checkout main"}}, True, True),
        ("PASS active: rm a hook via bash", {"tool_name": "Bash", "tool_input": {"command": "rm bin/hooks/loop-verify.py"}}, True, True),
        ("PASS active: normal commit on branch", {"tool_name": "Bash", "tool_input": {"command": "git add -A && git commit -m x"}}, True, False),
        ("PASS active: read audit.py (read is fine)", {"tool_name": "Read", "tool_input": {"file_path": aud}}, True, False),
        # --- DORMANT: normal owner session must be unaffected ---
        ("DORMANT: owner edits audit.py", {"tool_name": "Edit", "tool_input": {"file_path": aud}}, False, False),
        ("DORMANT: owner edits a hook", {"tool_name": "Write", "tool_input": {"file_path": hook}}, False, False),
        ("DORMANT: owner git push", {"tool_name": "Bash", "tool_input": {"command": "git push"}}, False, False),
        # --- executor agent_type triggers the firewall even without sentinel param ---
        ("agent_type executor: edit audit.py", {"tool_name": "Edit", "agent_type": "self-improve-executor", "tool_input": {"file_path": aud}}, None, True),
        ("malformed tool_input (list)", {"tool_name": "Edit", "tool_input": []}, True, False),
    ]
    ok = True
    for name, payload, pa, expect in cases:
        got, reason = decide(payload, pass_active=pa)
        if got != expect:
            ok = False
        print(f"  [{'OK ' if got == expect else 'FAIL'}] {name}: deny={got} (exp {expect})"
              f"{'  -> ' + reason[:70] if got else ''}")
    import subprocess
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       input="not json", capture_output=True, text=True)
    fo = p.stdout.strip() == "{}"
    print(f"  [{'OK ' if fo else 'FAIL'}] fail-open on malformed stdin -> '{p.stdout.strip()}'")
    ok = ok and fo

    # --- ROOT-sanity + REAL-PATH end-to-end (catches a miscomputed ROOT that silently
    #     makes the firewall inert — the unit cases above share ROOT/forced pass_active so
    #     they pass even when the live hook never fires; these exercise the real entry point) ---
    root_ok = (os.path.isfile(os.path.join(ROOT, "bin", "audit.py"))
               and os.path.isdir(os.path.join(ROOT, "tasks")))
    print(f"  [{'OK ' if root_ok else 'FAIL'}] ROOT is the repo root (bin/audit.py + tasks/ resolve under it)")
    ok = ok and root_ok
    if os.path.exists(SENTINEL):
        print("  [SKIP] real-sentinel round-trip — a real pass is active, not disturbing it")
    else:
        rt_ok = e2e_ok = False
        try:
            open(SENTINEL, "w", encoding="utf-8").close()          # the EXACT path the executor writes
            detect = _pass_active({})
            # end-to-end: a protected edit through the real main() must DENY while the sentinel exists
            pr = subprocess.run([sys.executable, os.path.abspath(__file__)],
                                input=json.dumps({"tool_name": "Edit",
                                                  "tool_input": {"file_path": os.path.join(ROOT, "bin", "audit.py")}}),
                                capture_output=True, text=True)
            denied = '"permissionDecision": "deny"' in pr.stdout or '"permissionDecision":"deny"' in pr.stdout
            e2e_ok = bool(detect) and denied
        finally:
            if os.path.exists(SENTINEL):
                os.remove(SENTINEL)
        rt_ok = e2e_ok and not _pass_active({})                   # and dormant again after removal
        print(f"  [{'OK ' if rt_ok else 'FAIL'}] real sentinel detected + protected edit DENIED via main(), then dormant")
        ok = ok and rt_ok

    print(f"\nSELF-TEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
