#!/usr/bin/env python3
"""loop-verify.py — the closed verify-fix-retry loop, made physical (Stop hook).

Closes the line into a circle: when the session tries to STOP, run the project's
natural correctness check; on failure, block the stop (exit 2) and feed the failing
output back so the model must fix the CAUSE and retry — up to MAX attempts, then
allow the stop and report what still fails. This turns NORTH-STAR §4b "verify
relentlessly / never declare done without proof" from prose into enforcement.

Grounded in claude-os's own harvested research (docs/research/2026-06-09-owner-sweep.md;
reward-hacking arXiv 2605.02964 — RL agents exploit verifiers 0–13.9%, which is WHY the
protocol forbids weakening the check). Protocol: docs/LOOP-PROTOCOL.md.

ADAPTED from the community "self-improving loop" pattern, three deliberate changes:
  1. PROJECT-TYPE-AWARE + NO-OP-SAFE — detects claude-os / npm / pytest / cargo / go and
     runs the right check; exits 0 cleanly when a project has none. (The source hardcodes
     `npm test`, which would misfire on every non-JS project — wrong for a GLOBAL layer.)
  2. DETERMINISTIC retry cap in the hook itself (per-cwd state file), not trusting the
     model to count. Same-cause-twice / cap-hit => allow stop + report (never silent loop).
  3. Read-only + FAIL-OPEN — any hook error, missing runner, or bad input exits 0; a
     verify hook must never trap a session or mutate the repo (Law 1 do-no-harm).

Stop-hook contract (Claude Code): exit 2 => block stop, stderr shown to the model =>
it continues. exit 0 => allow stop. Infinite-loop guard = the MAX counter (more robust
than relying on stop_hook_active alone, which is also honored).

Self-test: python3 bin/hooks/loop-verify.py --self-test
"""
import sys, os, json, subprocess, hashlib, tempfile

MAX_ATTEMPTS = 5
TIMEOUT_S = 180


def detect(cwd):
    """Return (label, argv, ok_codes) for the project's correctness check, or (None, None, None).
    ok_codes = extra return codes to treat as success (e.g. pytest 5 = 'no tests collected')."""
    j = lambda *p: os.path.join(cwd, *p)
    # claude-os itself: audit.py is the deterministic integrity gate (exit!=0 only on HIGH).
    if os.path.isfile(j("bin", "audit.py")) and os.path.isfile(j("STATE.md")):
        return ("claude-os audit", [sys.executable, j("bin", "audit.py")], set())
    # npm: only if a test script actually exists.
    if os.path.isfile(j("package.json")):
        try:
            scripts = (json.load(open(j("package.json"))) or {}).get("scripts", {})
            if "test" in scripts:
                return ("npm test", ["npm", "test", "--silent"], set())
        except Exception:
            pass
    # python: pytest if there's any sign of it. exit 5 = no tests collected => treat as pass.
    if os.path.isfile(j("pyproject.toml")) or os.path.isfile(j("pytest.ini")) \
            or os.path.isfile(j("setup.cfg")) or os.path.isdir(j("tests")):
        return ("pytest", ["pytest", "-q"], {5})
    if os.path.isfile(j("Cargo.toml")):
        return ("cargo test", ["cargo", "test", "--quiet"], set())
    if os.path.isfile(j("go.mod")):
        return ("go test", ["go", "test", "./..."], set())
    return (None, None, None)


def state_path(cwd):
    h = hashlib.sha256(os.path.realpath(cwd).encode()).hexdigest()[:16]
    return os.path.join(tempfile.gettempdir(), f"claude-loop-{h}")


def decide(returncode, ok_codes, state_file, label, output):
    """Pure decision: (exit_code, stderr_message, new_counter_or_None).
    new_counter None => remove the state file (pass / cap reached)."""
    if returncode == 0 or returncode in (ok_codes or set()):
        return (0, "", None)  # checks pass → really done
    n = 1
    try:
        n = int(open(state_file).read().strip()) + 1
    except Exception:
        n = 1
    tail = (output or "")[-1500:]
    if n > MAX_ATTEMPTS:
        return (0, (f"[loop-verify] {label} STILL FAILING after {MAX_ATTEMPTS} attempts — "
                    f"stopping per docs/LOOP-PROTOCOL.md. Report exactly what remains and what "
                    f"you tried; do NOT weaken or skip the check:\n{tail}"), None)
    return (2, (f"[loop-verify] {label} FAILED (attempt {n}/{MAX_ATTEMPTS}). Fix the CAUSE and "
                f"continue — do NOT weaken/skip the check or edit it to pass. If the SAME error "
                f"repeats, switch to root-cause (systematic-debugging, or a FRESH subagent with "
                f"clean context) per docs/LOOP-PROTOCOL.md:\n{tail}"), n)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)  # fail-open: never trap a session on bad/empty input
    cwd = data.get("cwd") or os.getcwd()
    label, argv, ok_codes = detect(cwd)
    if not argv:
        sys.exit(0)  # no detectable check here → allow stop (safe global no-op)
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT_S)
    except Exception:
        sys.exit(0)  # runner missing / timed out / blew up → don't trap the session
    sf = state_path(cwd)
    code, msg, counter = decide(proc.returncode, ok_codes, sf,
                                label, (proc.stdout or "") + "\n" + (proc.stderr or ""))
    if counter is None:
        try:
            os.remove(sf)
        except OSError:
            pass
    else:
        try:
            open(sf, "w").write(str(counter))
        except OSError:
            pass
    if msg:
        print(msg, file=sys.stderr)
    sys.exit(code)


def _self_test():
    import tempfile as t
    fails = []
    d = t.mkdtemp()
    sf = os.path.join(d, "ctr")
    # 1. pass clears + exits 0
    c, m, n = decide(0, set(), sf, "x", "ok")
    if (c, n) != (0, None):
        fails.append(f"pass: {c},{n}")
    # 2. pytest no-tests (exit 5) treated as pass
    c, m, n = decide(5, {5}, sf, "pytest", "no tests")
    if c != 0:
        fails.append(f"pytest-5: {c}")
    # 3. first failure → block (exit 2), counter 1
    if os.path.exists(sf):
        os.remove(sf)
    c, m, n = decide(1, set(), sf, "x", "boom")
    if (c, n) != (2, 1) or "do NOT weaken" not in m:
        fails.append(f"fail1: {c},{n}")
    # 4. counter climbs to cap then allows stop + reports
    open(sf, "w").write("4")
    c, m, n = decide(1, set(), sf, "x", "boom")  # 4+1=5, still <=MAX → block
    if c != 2:
        fails.append(f"fail5: {c}")
    open(sf, "w").write(str(MAX_ATTEMPTS))
    c, m, n = decide(1, set(), sf, "x", "boom")  # MAX+1 → allow stop, report, clear
    if (c, n) != (0, None) or "STILL FAILING" not in m:
        fails.append(f"cap: {c},{n}")
    # 5. detect: no-op project
    empty = t.mkdtemp()
    if detect(empty)[1] is not None:
        fails.append("detect-empty not None")
    # 6. detect: claude-os shape
    co = t.mkdtemp()
    os.makedirs(os.path.join(co, "bin"))
    open(os.path.join(co, "bin", "audit.py"), "w").write("")
    open(os.path.join(co, "STATE.md"), "w").write("")
    if detect(co)[0] != "claude-os audit":
        fails.append("detect-claude-os")
    # 7. detect: npm only with test script
    npm = t.mkdtemp()
    open(os.path.join(npm, "package.json"), "w").write('{"scripts":{"test":"jest"}}')
    if detect(npm)[0] != "npm test":
        fails.append("detect-npm")
    npm2 = t.mkdtemp()
    open(os.path.join(npm2, "package.json"), "w").write('{"scripts":{"build":"x"}}')
    if detect(npm2)[1] is not None:
        fails.append("detect-npm-notest should be None")
    print("loop-verify self-test:", "PASS" if not fails else f"FAIL {fails}")
    sys.exit(0 if not fails else 1)


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        _self_test()
    main()
