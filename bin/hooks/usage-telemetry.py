#!/usr/bin/env python3
"""usage-telemetry — PreToolUse sensor: record which skills/agents actually FIRE.

The keystone of the Utilization Coordinator (NORTH-STAR organ 14). The
estate's "is this resource actually used?" question can only be answered honestly
by OBSERVED use — not by "is it named/reachable somewhere?" (that is the
presence-vs-use theater the Coordinator exists to kill). This hook is that sensor:
it records every Skill / Agent / Task invocation (the resource-firing signal) to a
machine-local JSONL log, so utilization becomes MEASURED, not inferred.

A SENSOR, NOT A GATE: it never blocks, rewrites, or denies a tool call — it appends
one line and allows. Cheap, deterministic, fail-open (any error -> allow, log
nothing). Matched to Skill|Agent|Task ONLY (the capability-firing signal);
high-traffic primitives (Read/Grep/Bash) are deliberately NOT recorded — they are
not the "which capability fired" question this answers, and logging them would be
pure noise + cost (the project that birthed this was about saving tokens).

Note (v1 honesty): PreToolUse fires BEFORE the call, so a record is an *attempted*
firing — if a later guard denies the call it is still logged. That over-counts
denials slightly; acceptable for a utilization signal. Tighten to PostToolUse if
exact success counts are ever needed.

Log:  $CLAUDE_PROJECT_DIR/.telemetry/usage.jsonl  (gitignored; machine-local)
      one JSON object per line: {"ts","tool","resource"}

Wiring (PreToolUse, additive — runs alongside the existing guards):
  {"matcher": "Skill|Agent|Task", "hooks": [{"type": "command",
      "command": "python3 \\"$CLAUDE_PROJECT_DIR/bin/hooks/usage-telemetry.py\\""}]}

Imports stay stdlib-minimal (os/sys/json/time) on purpose: NO xml/plistlib, so the
sensor runs even under the broken Homebrew python the other hooks trip over.

Self-test:  python3 bin/hooks/usage-telemetry.py --self-test
"""
import sys, os, json, time


def extract(data):
    """Return {tool, resource} for a capability-firing call, else None. Pure; type-guarded."""
    if not isinstance(data, dict):
        return None
    tool = data.get("tool_name")
    if not isinstance(tool, str) or not tool:
        return None
    if tool not in ("Skill", "Agent", "Task"):
        return None
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}
    if tool == "Skill":
        resource = ti.get("skill")
    else:  # Agent / Task
        resource = ti.get("subagent_type")
    if resource is not None and not isinstance(resource, str):
        resource = None
    rec = {"tool": tool, "resource": resource}
    # Route-tier capture (2026-07-01 rework): the model/effort override a delegated
    # dispatch carries — the raw signal for the down-tier success-rate calibration
    # (MODEL-ROUTING § Down-tier eligibility). Absent = inherited session model.
    if tool in ("Agent", "Task"):
        for k in ("model", "effort"):
            v = ti.get(k)
            if isinstance(v, str) and v:
                rec[k] = v
    return rec


def _log_path():
    root = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return os.path.join(root, ".telemetry", "usage.jsonl")


def record(rec, path=None):
    """Append rec as one JSONL line (ts stamped here). Best-effort; never raises."""
    path = path or _log_path()
    try:
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        out = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "tool": rec.get("tool"), "resource": rec.get("resource")}
        for k in ("model", "effort"):     # route-tier fields, only when present
            if isinstance(rec.get(k), str):
                out[k] = rec[k]
        with open(path, "a") as f:
            f.write(json.dumps(out, sort_keys=True) + "\n")
        return True
    except Exception:
        return False


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("{}")
        return 0
    try:
        rec = extract(data)
        if rec is not None:
            record(rec)
    except Exception:
        pass
    print("{}")           # allow — sensor never gates
    return 0


def _self_test():
    cases = [
        ("Skill call -> record skill", {"tool_name": "Skill", "tool_input": {"skill": "graphify"}},
         {"tool": "Skill", "resource": "graphify"}),
        ("Agent call -> record subagent_type", {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore"}},
         {"tool": "Agent", "resource": "Explore"}),
        ("Agent call w/ route tier -> record model+effort", {"tool_name": "Agent",
         "tool_input": {"subagent_type": "Explore", "model": "tier-x", "effort": "high"}},
         {"tool": "Agent", "resource": "Explore", "model": "tier-x", "effort": "high"}),
        ("Skill never carries route fields", {"tool_name": "Skill", "tool_input": {"skill": "weigh", "model": "x"}},
         {"tool": "Skill", "resource": "weigh"}),
        ("non-string model dropped", {"tool_name": "Task", "tool_input": {"subagent_type": "claude", "model": 7}},
         {"tool": "Task", "resource": "claude"}),
        ("Task call -> record subagent_type", {"tool_name": "Task", "tool_input": {"subagent_type": "claude"}},
         {"tool": "Task", "resource": "claude"}),
        ("Read call -> ignored", {"tool_name": "Read", "tool_input": {"file_path": "x"}}, None),
        ("Bash call -> ignored", {"tool_name": "Bash", "tool_input": {"command": "ls"}}, None),
        ("Skill missing name -> record null resource", {"tool_name": "Skill", "tool_input": {}},
         {"tool": "Skill", "resource": None}),
        ("non-string skill -> null resource", {"tool_name": "Skill", "tool_input": {"skill": 7}},
         {"tool": "Skill", "resource": None}),
        ("missing tool_name -> None", {"tool_input": {}}, None),
        ("non-dict -> None", [], None),
    ]
    ok = True
    for name, payload, expect in cases:
        got = extract(payload)
        passed = got == expect
        ok = ok and passed
        print(f"  [{'OK ' if passed else 'FAIL'}] {name}: {got} (exp {expect})")

    # record() actually writes a line
    import tempfile
    tmp = os.path.join(tempfile.gettempdir(), "usage-telemetry-selftest.jsonl")
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass
    wrote = record({"tool": "Skill", "resource": "graphify"}, path=tmp)
    line_ok = False
    try:
        with open(tmp) as f:
            obj = json.loads(f.readline())
        line_ok = obj.get("tool") == "Skill" and obj.get("resource") == "graphify" and "ts" in obj
    except Exception:
        line_ok = False
    ok = ok and wrote and line_ok
    print(f"  [{'OK ' if wrote and line_ok else 'FAIL'}] record() appends a valid JSONL line")

    # fail-open on malformed stdin
    import subprocess
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       input="not json", capture_output=True, text=True)
    fo = p.stdout.strip() == "{}" and p.returncode == 0
    print(f"  [{'OK ' if fo else 'FAIL'}] fail-open on malformed stdin -> '{p.stdout.strip()}' rc={p.returncode}")
    ok = ok and fo

    print(f"\nSELF-TEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
