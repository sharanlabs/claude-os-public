#!/usr/bin/env python3
"""golden-lint.py — lint Markdown evaluation tasks for known check defects.

D0 rejects malformed checks blocks. D1-D8 flag patterns such as prompt-echo
checks, fragile regular expressions, missing grading criteria, ID/filename
mismatches, and headings the runner cannot read. These detectors are heuristics:
a clean result does not establish task correctness or completeness.

Exit 0 means at least one task was scanned with no finding. Exit 1 means a
finding, unreadable input, or empty directory. Usage errors exit 2.

Suppress a justified heuristic finding with a comment in the task body:
  <!-- noqa: golden-lint D1 check-name — explanation -->
Do not put comments inside the JSON checks block. D0 cannot be suppressed.

Python standard library only. Usage:
  python3 bin/golden-lint.py examples/review-contract/tasks
  python3 bin/golden-lint.py                    # default: tasks/golden
  python3 bin/golden-lint.py --self-test
"""
import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
GOLDEN = REPO / "tasks" / "golden"

# Words too common to count as evidence that a check echoes the task's own prompt.
_STOP = {"the", "a", "an", "and", "or", "not", "is", "it", "to", "of", "in", "on", "for", "with",
         "use", "when", "goal", "yes", "no", "id", "type", "name", "value", "check", "task"}


def _frontmatter(text):
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.S)
    return m.group(1) if m else ""


def _section(body, name):
    out, on = [], False
    for ln in body.split("\n"):
        if re.match(rf"^##\s+{name}\b", ln, re.IGNORECASE):
            on = True
            continue
        if on and ln.startswith("## "):
            break
        if on:
            out.append(ln)
    return "\n".join(out)


def _checks(text):
    m = re.search(r"```json\s*\n(.*?)\n```", text, re.S)
    if not m:
        if "```json" in text:
            raise ValueError("unterminated JSON checks block")
        return []
    try:
        p = json.loads(m.group(1))
    except json.JSONDecodeError as exc:
        raise ValueError(f"malformed JSON checks block: {exc.msg}") from exc
    if isinstance(p, dict):
        if "checks" not in p or set(p) - {"checks", "min_dimension"}:
            raise ValueError("checks object requires 'checks' and permits only 'min_dimension' alongside it")
        p = p["checks"]
    if not isinstance(p, list):
        raise ValueError("checks must be a list")
    if any(not isinstance(check, dict) for check in p):
        raise ValueError("each check must be an object")
    return p


def lint_one(name, text):
    """Return a list of (detector_id, message) for one task's raw text."""
    out = []
    fm = _frontmatter(text)
    body = text[len(fm) + 8:] if fm else text
    inp = _section(body, "Input") or _section(body, "The task")
    # Read the CHECK SECTION, not the first ```json block anywhere in the file
    # (fixed 2026-09-16, finding F13 — confirmed). `eval-runner` loads checks from
    # `## Check` (`_section(body, "Check")`); this linter searched the whole document,
    # so a task whose Input section carries an illustrative JSON block had its EXAMPLE
    # linted while the runner scored the real test. The linter and the runner must read
    # the same bytes or the linter is grading a different file.
    try:
        checks = _checks(_section(body, "Check"))
    except ValueError as exc:
        out.append(("D0", f"{name}: {exc}"))
        checks = []
    raw_lines = text.split("\n")

    def suppressed(det, cname):
        """A justified case is escaped by an HTML comment ANYWHERE in the task file:
            <!-- noqa: golden-lint D1 csv-constraint-survives — reason -->

        NOT on the check's own line. The first cut of this linter said "on the check's own line",
        which is inside a ```json fence — a comment there is invalid JSON, `json.loads` raises,
        `_checks` returns [], and the file silently reports ZERO findings. An escape hatch that
        blinds the linter is worse than no escape hatch, and it is the same silent-no-match class
        this file exists to catch. Caught by trying to use it.
        """
        return f"noqa: golden-lint {det} {cname}" in text

    inp_l = inp.lower()

    for c in checks:
        if not isinstance(c, dict):
            continue
        cname = c.get("name", "?")
        ctype = c.get("type", "")
        pat = str(c.get("pattern", ""))
        vals = c.get("values") if isinstance(c.get("values"), list) else (
            [c.get("value")] if c.get("value") is not None else [])
        vals = [str(v) for v in vals]

        # ---- D1: the check's own value is already in the task's Input -> cannot discriminate.
        # This is the echo/inversion root class. FP mode: a task that legitimately asks the
        # candidate to REPEAT a term (rare). Escape: noqa on the check line.
        if ctype in ("contains", "contains_all") and inp_l:
            echoed = [v for v in vals
                      if len(v) > 2 and v.lower() not in _STOP and v.lower() in inp_l]
            if echoed and not suppressed("D1", cname):
                out.append(("D1", f"{name}: check `{cname}` looks for {echoed!r}, which is ALREADY in "
                                 f"the task's own ## Input — a candidate can satisfy it by echoing the "
                                 f"prompt, so it cannot discriminate. Grade an emitted signal instead."))

        # ---- D2: `\s` inside a line-anchored regex crosses NEWLINES (the 003 bug).
        if ctype.startswith("regex") and pat.startswith("(?m)^") and r"\s*" in pat and not suppressed("D2", cname):
            out.append(("D2", f"{name}: check `{cname}` is line-anchored `(?m)^` but uses `\\s*`, which "
                             f"matches a NEWLINE — the match walks off the line and the check degrades to "
                             f"'this token appears somewhere'. Use `[ \\t]*`."))

        # ---- D3: a contract-field regex with no line anchor (the 015 fail-open).
        if ctype.startswith("regex") and re.search(r"(?i)(VERDICT|STATUS|RESULT)\s*:", pat) \
                and "^" not in pat and not suppressed("D3", cname):
            out.append(("D3", f"{name}: check `{cname}` matches a contract field ({pat[:40]!r}) with NO "
                             f"line anchor, so prose that merely QUOTES the field satisfies it. Anchor it "
                             f"to the line and tolerate emphasis."))

        # ---- D4: a secret/credential literal matched case-sensitively (the 014 bug).
        if ctype in ("not_contains", "contains") and not c.get("ignore_case"):
            for v in vals:
                if re.search(r"(?i)(sk[-_]|api[-_]?key|token|secret|password|bearer)", v) \
                        and not suppressed("D4", cname):
                    out.append(("D4", f"{name}: check `{cname}` matches a credential-shaped literal "
                                     f"case-SENSITIVELY — the same value typed in capitals or wrapped "
                                     f"across a line walks straight through. Use a case- and "
                                     f"separator-insensitive `regex_not_match`."))
                    break

        # ---- D5: min_length at or below the length of the task's own Input -> an echo clears it.
        if ctype == "min_length" and inp:
            try:
                v = float(c.get("value"))
            except (TypeError, ValueError):
                v = None
            if v is not None and v <= len(inp) and not suppressed("D5", cname):
                out.append(("D5", f"{name}: check `{cname}` floors length at {int(v)} but the task's own "
                                 f"## Input is {len(inp)} chars — an echo of the prompt clears the floor "
                                 f"that exists to forbid exactly that."))

    # ---- D6: a task with NO deterministic discriminator at all and no rubric either.
    has_rubric = any(l.strip().lstrip("- ").lower().startswith("subjective") for l in body.split("\n"))
    if not checks and not has_rubric:
        out.append(("D6", f"{name}: no checks AND no `subjective:` rubric — no grading criteria were found."))

    # ---- D7: schema — id must match the filename slug, type must be a known value.
    idm = re.search(r"^id:\s*(\S+)", fm, re.M)
    stem = name[:-3] if name.endswith(".md") else name
    if idm and idm.group(1) != stem:
        out.append(("D7", f"{name}: `id: {idm.group(1)}` does not match the filename slug — "
                         f"`resolve_candidate` keys on `id:`, so the candidate file must be named "
                         f"`{idm.group(1)}.txt`, which is a trap for anyone naming it after the task."))
    tym = re.search(r"^type:\s*(\S+)", fm, re.M)
    if tym and tym.group(1).lower() not in ("deterministic", "judge"):
        out.append(("D7", f"{name}: `type: {tym.group(1)}` is outside the documented "
                         f"`deterministic|judge` enum — the loader COERCES it silently, so a typo "
                         f"changes how the task is scored without erroring."))

    # ---- D8: a `## Checks` (plural) heading is invisible to the loader's `Check\b` matcher.
    if re.search(r"^##\s+Checks\b", body, re.M):
        out.append(("D8", f"{name}: heading is `## Checks` (plural). The loader matches `^##\\s+Check\\b` "
                         f"and `\\b` does not exist between `k` and `s`, so the whole block is invisible "
                         f"and the task is silently reclassified."))
    return out


# ------------------------------------------------------------------ self-test
_GOOD = """---
id: 900-good
type: deterministic
---

## Input
Produce a verdict block for the widget review.

## Reference
A verdict block.

## Check
```json
{"checks": [
  {"name": "verdict-is-block", "type": "regex", "pattern": "(?im)^\\\\s*VERDICT:\\\\s*BLOCK\\\\s*$"},
  {"name": "no-key", "type": "regex_not_match", "pattern": "(?i)sk[-_\\\\s]*live"},
  {"name": "long-enough", "type": "min_length", "value": 5000}
]}
```
- subjective: the reasoning is sound
"""

_BAD = {
    "D1": """---
id: 901-bad
type: deterministic
---

## Input
Keep the export csv not json, csv is non negotiable.

## Check
```json
{"checks": [{"name": "csv-survives", "type": "contains", "value": "csv"}]}
```
""",
    "D2": """---
id: 902-bad
type: deterministic
---

## Input
x

## Check
```json
{"checks": [{"name": "has-tools", "type": "regex", "pattern": "(?m)^tools:\\\\s*\\\\S+"}]}
```
""",
    "D3": """---
id: 903-bad
type: deterministic
---

## Input
x

## Check
```json
{"checks": [{"name": "verdict-is-block", "type": "regex", "pattern": "(?i)VERDICT:\\\\s*BLOCK"}]}
```
""",
    "D4": """---
id: 904-bad
type: deterministic
---

## Input
x

## Check
```json
{"checks": [{"name": "secret-never-carried", "type": "not_contains", "value": "EXAMPLE_SECRET_DO_NOT_USE"}]}
```
""",
    "D5": """---
id: 905-bad
type: deterministic
---

## Input
This input section is deliberately made longer than the floor the check below sets, so that a
verbatim echo of this very prompt would clear it. That is the defect being detected here.

## Check
```json
{"checks": [{"name": "no-dilution", "type": "min_length", "value": 50}]}
```
""",
    "D7": """---
id: 906-mismatched
type: judge
---

## Input
x

## Reference
y
""",
    "D8": """---
id: 907-bad
type: deterministic
---

## Input
x

## Checks
```json
{"checks": [{"name": "a", "type": "contains", "value": "zzz"}]}
```
""",
}


def self_test() -> int:
    fails = []
    got = lint_one("900-good.md", _GOOD)
    if got:
        fails.append(f"known-NEGATIVE (healthy task) wrongly flagged: {got}")
    for det, doc in _BAD.items():
        ids = {d for d, _ in lint_one(f"{det}.md", doc)}
        if det not in ids:
            fails.append(f"known-POSITIVE {det} NOT caught (got {sorted(ids) or 'nothing'})")
    # The escape must suppress — and it must do so WITHOUT breaking the JSON block, which is why
    # it is an HTML comment in the body rather than a comment on the check's line.
    esc = _BAD["D4"] + "\n<!-- noqa: golden-lint D4 secret-never-carried — fixture, key is synthetic -->\n"
    if any(d == "D4" for d, _ in lint_one("esc.md", esc)):
        fails.append("the `noqa: golden-lint D4 <check>` escape did NOT suppress the finding")
    # ...and the escape must be SPECIFIC: escaping a different check must not silence this one.
    wrong = _BAD["D4"] + "\n<!-- noqa: golden-lint D4 some-other-check -->\n"
    if not any(d == "D4" for d, _ in lint_one("esc2.md", wrong)):
        fails.append("a noqa naming a DIFFERENT check wrongly suppressed the finding (too broad)")

    if fails:
        print("SELF-TEST FAIL")
        for f in fails:
            print("  x " + f)
        return 1
    print(f"SELF-TEST PASS — {len(_BAD)} known-positives caught, 1 known-negative left alone, "
          f"noqa escape honoured")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Lint Markdown evaluation tasks for known check defects.")
    parser.add_argument("tasks", nargs="?", type=Path, default=GOLDEN,
                        help="task directory (default: tasks/golden relative to this repository)")
    parser.add_argument("--self-test", action="store_true", help="run isolated built-in tests")
    args = parser.parse_args()
    if args.self_test:
        return self_test()
    target = args.tasks
    if not target.is_dir():
        print(f"ERROR: task directory {target} not found")
        return 1
    files = sorted(p for p in target.glob("*.md") if p.name.lower() != "readme.md")
    if not files:
        print(f"ERROR: no task files under {target} — refusing to report an empty suite as clean")
        return 1
    findings = []
    for p in files:
        try:
            findings += lint_one(p.name, p.read_text(encoding="utf-8"))
        except (OSError, UnicodeError) as exc:
            findings.append(("D0", f"{p.name}: task could not be read ({type(exc).__name__})"))
    print(f"golden-lint: {len(files)} task(s) scanned, {len(findings)} finding(s)")
    if findings:
        print()
        by = {}
        for det, msg in findings:
            by.setdefault(det, []).append(msg)
        for det in sorted(by):
            print(f"  [{det}] {len(by[det])} finding(s)")
            for m in by[det]:
                print(f"      - {m}")
        print()
        print("Advisory: these are the CLASSES that made this set untrustworthy on 2026-07-29.")
        print("Escape an advisory check with an HTML comment: <!-- noqa: golden-lint <id> <check-name> -->")
        print("Malformed input (D0) cannot be suppressed.")
        return 1
    print("OK — none of the known defect classes present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
