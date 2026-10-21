#!/usr/bin/env python3
"""eval-runner.py — standard-library evaluation runner for supplied candidate outputs.

Runs explicit checks from Markdown tasks and prepares prompts for subjective or
source-grounded judgments. It never invokes a model, sends a network request,
or executes candidate code. A supplied verdict is validated; its provenance is
not authenticated. See examples/README.md for a complete local demonstration.

Task format:
  Flat frontmatter (id, type: deterministic|judge, optional min_dimension), then
  ## Input, ## Reference and ## Check. A fenced JSON block under ## Check is a
  check list or {"checks": [...], "min_dimension": 3}. Subjective dimensions use
  '- subjective: stable-label — full scoring criteria'. This is a small flat
  frontmatter format, not a general YAML parser.

Check types:
  exact, contains, not_contains, contains_all, regex, regex_not_match,
  max_length, min_length, score_threshold, budget, tool_trajectory,
  hallucination (deferred judgment against provided context).

Metrics in a candidate's usage object are supplied data, not independent
measurements. Trajectory checks compare supplied tool names, not execution logs.
A passing result proves conformance to the authored checks; it cannot establish
that the checks are complete or the candidate is correct in other respects.

Usage (prepend python3):
  bin/eval-runner.py run --tasks <directory-or-file> --candidates <directory> [--json out.json]
  bin/eval-runner.py judge-prompt --task <file> --candidate <file>
  bin/eval-runner.py ingest-verdict --task <file> --verdict <file> [--candidate <file>] [--json out.json]
  bin/eval-runner.py hallucination-prompt --task <file> --candidate <file>
  bin/eval-runner.py ingest-hallucination --verdict <file> [--task <file>|--max-rate 0.0]
  bin/eval-runner.py self-test

For task id 001-example, run reads <candidates>/001-example.txt. Missing outputs
are errors. ingest-verdict requires the candidate for tasks with deterministic
checks, so a judge PASS cannot erase a deterministic failure.

Suite exit codes:
  0: complete, at least one task evaluated, all checks passed.
  1: failed check, task error, or empty suite.
  2: no failure established, but a required judgment remains outstanding.

Use independent judges for real evaluations and retain their raw evidence.
The runner does not establish independence or protect an external judge against
prompt injection in candidate content. Author-controlled task regular expressions
are evaluated by Python's regex engine without a per-expression timeout.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# --------------------------------------------------------------------------- #
# Golden-task parsing (additive superset of tasks/golden/README.md)
# --------------------------------------------------------------------------- #

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_JSON_BLOCK_RE = re.compile(r"```json\s*\n(.*?)\n```", re.DOTALL)


@dataclass
class GoldenTask:
    id: str
    type: str                       # "deterministic" | "judge"
    input: str                      # the prompt/scenario the system was given
    reference: str                  # the known-good answer / bar
    checks: list[dict[str, Any]]    # typed deterministic assertions
    rubric: list[str]               # judge criteria LABELS (stable ids; coverage is matched on these)
    path: Path
    raw_frontmatter: dict[str, str] = field(default_factory=dict)
    # The FULL subjective bullet per label — the actual scoring rules. Defaulted (and placed
    # last) so every existing constructor, including the self-test's in-memory tasks, keeps
    # working; parse_task always supplies it.
    rubric_text: list[str] = field(default_factory=list)
    # Per-task floor every rubric dimension must reach (default 2.0 = the corpus-wide rule).
    # Added 2026-09-26 after an independent review of the golden repair: splitting one
    # "all conditions hold" bullet into N dimensions turned it into an AVERAGE — two dimensions
    # that score 5 merely by the absence of a mistake could carry two failed conditions past
    # 3.5 (2+2+5+5). A split task sets {"min_dimension": 3} in its checks block so every
    # condition must genuinely hold.
    min_dimension: float = 2.0


def _parse_frontmatter(block: str) -> dict[str, str]:
    """Hand-parse flat `key: value` front-matter (no YAML dep — stdlib only)."""
    out: dict[str, str] = {}
    for line in block.splitlines():
        line = line.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        out[key.strip()] = val.strip()
    return out


def _section(md: str, heading: str) -> str:
    """Return the text under a `## Heading...` up to the next `## ` (or EOF).
    Matches the heading as a PREFIX (word-boundary), so `## Check (how to score it)`
    is found by heading="Check" — golden-task headings carry trailing parentheticals."""
    pat = re.compile(rf"^##\s+{re.escape(heading)}\b[^\n]*\n(.*?)(?=^##\s|\Z)",
                     re.DOTALL | re.MULTILINE | re.IGNORECASE)
    m = pat.search(md)
    return m.group(1).strip() if m else ""


def load_task(path: Path) -> GoldenTask:
    text = path.read_text(encoding="utf-8")
    fm_match = _FRONTMATTER_RE.match(text)
    fm = _parse_frontmatter(fm_match.group(1)) if fm_match else {}
    body = text[fm_match.end():] if fm_match else text

    input_sec = _section(body, "Input")
    reference_sec = _section(body, "Reference")
    check_sec = _section(body, "Check")

    # The embedded JSON checks block (additive). Absent => judge-only task.
    checks: list[dict[str, Any]] = []
    min_dim = 2.0
    jm = _JSON_BLOCK_RE.search(check_sec)
    if jm:
        try:
            parsed = json.loads(jm.group(1))
        except json.JSONDecodeError as e:
            raise ValueError(f"{path.name}: malformed ```json checks block: {e}") from e
        if isinstance(parsed, dict) and "checks" in parsed:
            checks = parsed["checks"]
            unknown = set(parsed) - {"checks", "min_dimension"}
            if unknown:
                raise ValueError(f"{path.name}: unknown key(s) in the checks block: {sorted(unknown)} "
                                 f"(a misspelled floor would silently fall back to 2)")
            if "min_dimension" in parsed:
                md = parsed["min_dimension"]
                if isinstance(md, bool) or not isinstance(md, (int, float)) or not (2 <= md <= 5):
                    raise ValueError(f"{path.name}: min_dimension must be a number in [2, 5], got {md!r}")
                min_dim = float(md)
        elif isinstance(parsed, list):
            checks = parsed
        else:
            raise ValueError(f"{path.name}: checks block must be a list or {{\"checks\": [...]}}")
        if not isinstance(checks, list):
            raise ValueError(f"{path.name}: checks must be a list")
        if any(not isinstance(check, dict) for check in checks):
            raise ValueError(f"{path.name}: each check must be an object")

    # Subjective rubric DIMENSIONS from the "subjective:" bullets in ## Check. Extract the
    # clean dimension LABEL — the text after the "subjective:" prefix, cut at the first
    # em-dash / paren / period / spaced-hyphen — so ingest_verdict's coverage check matches
    # on real dimension names. Keeping the literal "subjective:" prefix (the old behaviour)
    # collapsed every dimension's head to the shared word "subjective", which let one
    # catch-all criterion satisfy all of them (Codex gate 2026-07-23, eval-runner.py:153/572).
    # One dimension per bullet is the contract (see tasks/golden/006 / 004); prose
    # continuation lines that are not bullets are skipped.
    # The LABEL is a stable id used for one-to-one coverage matching (see ingest_verdict).
    # The FULL BODY is the actual scoring contract and MUST reach the judge — stripping it was
    # a HIGH found by the cross-vendor judge on 2026-09-14: task 006 declares "any skip or
    # bypass (including \"tests pass so it's done\") scores 0", and the judge was being handed
    # the bare word `gate-chaining`. That task carries ZERO deterministic checks, so the rubric
    # IS the whole instrument, and it was being delivered with its criteria removed.
    # CONTINUATION LINES ARE PART OF THE RUBRIC (fixed 2026-09-16). This loop used to `continue`
    # past every non-bullet line, and the comment above called that intentional -- but the task
    # files WRAP their rubric bullets across lines, so the judge received only the first physical
    # line. Measured: 7 of 9 dimensions in one batch arrived cut mid-sentence, e.g. 005's entire
    # criterion delivered as "every factual claim in `answer` must" and nothing after it. This is
    # the SAME defect class as the 2026-09-14 finding directly above -- that fix restored the
    # rubric BODY over the bare label, and the body was itself truncated at line one, so the
    # instrument still shipped its scoring contract half-removed. Found by the first judge run
    # ever performed on this corpus.
    rubric, rubric_text = [], []
    _cur = None
    for ln in check_sec.splitlines():
        s = ln.strip()
        is_bullet = s.startswith(("-", "*")) or bool(re.match(r"^\d+[.)]\s", s))
        if s.lower().lstrip("-* ").startswith("subjective"):
            body = s.split(":", 1)[1].strip() if ":" in s else s
            label = re.split(r"[—(.]|\s-\s", body, maxsplit=1)[0].strip().strip(",").lower()
            if label:
                rubric.append(label)
                rubric_text.append(body)
                _cur = len(rubric_text) - 1
            else:
                _cur = None
            continue
        if _cur is not None and s and not is_bullet and ln[:1] in (" ", "\t"):
            rubric_text[_cur] = (rubric_text[_cur] + " " + s).strip()
            continue
        if is_bullet or not s:
            _cur = None

    ttype = (fm.get("type") or "").strip().lower()
    if ttype not in ("deterministic", "judge"):
        ttype = "deterministic" if checks else "judge"

    # ---- FAIL-CLOSED on an UNPARSEABLE task (added 2026-07-29) --------------------------------
    # An audit found tasks 016/017 grading to `"passed": true, exit 0` against a COMPLETELY BLANK
    # reference bar: their bodies use `## The task` / `## Grading - ...` headings, so `_section()`
    # returned "" for Input, Reference AND Check; no checks meant the type fallback above chose
    # `judge`; and `ingest_verdict`'s rubric-coverage gate is `if task.rubric:` — which an empty
    # rubric skips. Every fail-closed guard in this file fires on a MALFORMED VERDICT; none fired
    # on a MALFORMED TASK. The two most safety-adjacent tasks in the set were therefore gradeable
    # to PASS with nothing to grade against — a false GREEN, the one class an eval harness must
    # never produce.
    #
    # A task carrying no input, no reference and no checks is not a lenient task, it is a BROKEN
    # one, and broken is not gradeable. Raise, so the loader reports it instead of scoring it.
    if not input_sec.strip() and not reference_sec.strip() and not checks:
        raise ValueError(
            f"{path.name}: UNPARSEABLE TASK — no `## Input`, no `## Reference` and no checks block "
            f"were found, so there is nothing to grade against. This would otherwise be scored as a "
            f"judge task with a BLANK reference bar and could return passed=true. Either the headings "
            f"do not match the expected vocabulary (Input / Reference / Check), or the task is "
            f"genuinely empty. Fix the task; do not grade it."
        )

    if "min_dimension" in fm and min_dim == 2.0:   # judge-only tasks have no checks block
        try:
            md = float(str(fm["min_dimension"]).strip())
        except ValueError:
            raise ValueError(f"{path.name}: frontmatter min_dimension must be a number in [2, 5]")
        if not (2 <= md <= 5):
            raise ValueError(f"{path.name}: frontmatter min_dimension must be in [2, 5], got {md}")
        min_dim = md
    tid = fm.get("id") or path.stem
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", tid):
        raise ValueError(f"{path.name}: unsafe task id — use an ASCII letter or digit, "
                         "then only ASCII letters, digits, underscores, or hyphens")
    return GoldenTask(
        id=tid, type=ttype, input=input_sec, reference=reference_sec,
        checks=checks, rubric=rubric, rubric_text=rubric_text, path=path, raw_frontmatter=fm,
        min_dimension=min_dim,
    )


def load_tasks(target: Path) -> list[GoldenTask]:
    """Load a task dir. A single BROKEN task is surfaced, never silently skipped — and never
    allowed to destroy the whole suite either.

    Both halves matter (2026-07-29). The unparseable-task guard in `load_task` is correct and
    fail-closed, but raising it through here meant one malformed file took the other 16 tasks
    down with it, which pressures a maintainer to delete the guard. So a broken task becomes a
    reported error (same treatment a missing candidate already gets) and `run_suite` exits
    non-zero because of it. A single .md target still raises directly: there is no suite to
    preserve, and the caller asked about that one file.
    """
    load_tasks.broken = []               # type: ignore[attr-defined]  # reset FIRST: the
    # single-file branch below returns early, and the first cut reset only after it — so a
    # directory load followed by a single-file load reported a phantom failure for a task the
    # caller never asked about. Fail-closed in direction, but a harness that invents failures
    # is how a guard gets deleted.
    if target.is_file():
        return [load_task(target)]
    files = sorted(p for p in target.glob("*.md") if p.name.lower() != "readme.md")
    tasks, broken, seen_ids = [], [], set()
    for p in files:
        try:
            task = load_task(p)
            if task.id in seen_ids:
                raise ValueError(f"{p.name}: duplicate task id {task.id!r}")
            seen_ids.add(task.id)
            tasks.append(task)
        except ValueError as e:
            broken.append(str(e))
    if broken:
        # Attach to the module so run_suite can report them without changing the return type.
        load_tasks.broken = broken           # type: ignore[attr-defined]
    return tasks


# --------------------------------------------------------------------------- #
# Deterministic check engine
# --------------------------------------------------------------------------- #

def _extract_raw(candidate: str, field_path: str | None) -> Any:
    """Pull a dotted JSON path from the candidate and return the RAW object (list/dict/
    number/str), not a stringified form. Used by checks that need structured data
    (tool_trajectory needs the list; budget needs the number). If no field, returns the
    whole candidate string."""
    if not field_path:
        return candidate
    obj = json.loads(candidate)
    for part in field_path.split("."):
        if isinstance(obj, list):
            obj = obj[int(part)]
        else:
            obj = obj[part]
    return obj


def _extract_field(candidate: str, field_path: str | None) -> str:
    """If `field` is given, treat the candidate as JSON and pull a dotted path;
    else return the whole candidate string. Lets a check target one JSON field."""
    obj = _extract_raw(candidate, field_path)
    return obj if isinstance(obj, str) else json.dumps(obj)


def _unanchored(pattern: str) -> bool:
    """True when a pattern has no line anchors / MULTILINE flag, so matching it against a
    whitespace-collapsed copy of the text cannot change what the anchors mean."""
    return not ("(?m" in pattern or "(?im" in pattern or "(?sm" in pattern or "$" in pattern
                or re.search(r"(?<!\[)\^", pattern))


def _search_wrapsafe(pattern: str, text: str, flags: int) -> bool:
    """re.search, also tried on a whitespace-collapsed copy for unanchored patterns. Added
    2026-09-26 after an independent review measured 4 of 69 re-flowed candidates flipping a check:
    a literal space in a pattern broke on a hard line wrap, so a correct answer failed on layout."""
    if re.search(pattern, text, flags):
        return True
    return _unanchored(pattern) and re.search(pattern, re.sub(r"\s+", " ", text), flags) is not None


def run_check(check: dict[str, Any], candidate: str) -> dict[str, Any]:
    """Run one typed check. Returns {name,type,passed,detail}. Never raises on a
    normal mismatch — only on a malformed check definition."""
    ctype = check.get("type")
    name = check.get("name") or ctype or "unnamed"
    target = _extract_field(candidate, check.get("field"))
    case_insensitive = bool(check.get("ignore_case"))
    hay = target.lower() if case_insensitive else target

    def res(passed: bool, detail: str) -> dict[str, Any]:
        return {"name": name, "type": ctype, "passed": bool(passed), "detail": detail}

    if ctype == "exact":
        exp = str(check["value"])
        e = exp.lower() if case_insensitive else exp
        return res(hay.strip() == e.strip(),
                   "exact match" if hay.strip() == e.strip()
                   else f"expected exactly {exp!r}, got {target[:120]!r}")

    if ctype == "contains":
        needle = str(check["value"])
        n = needle.lower() if case_insensitive else needle
        ok = n in hay
        return res(ok, f"contains {needle!r}" if ok else f"missing substring {needle!r}")

    if ctype == "not_contains":
        needle = str(check["value"])
        n = needle.lower() if case_insensitive else needle
        ok = n not in hay
        return res(ok, f"absent {needle!r}" if ok else f"forbidden substring present {needle!r}")

    if ctype == "contains_all":
        needles = [str(v) for v in check["values"]]
        missing = [v for v in needles
                   if (v.lower() if case_insensitive else v) not in hay]
        return res(not missing,
                   "all present" if not missing else f"missing {missing}")

    if ctype == "regex":
        flags = re.IGNORECASE if case_insensitive else 0
        if check.get("multiline"):
            flags |= re.MULTILINE
        ok = _search_wrapsafe(check["pattern"], target, flags)
        return res(ok, f"regex {check['pattern']!r} matched" if ok
                       else f"regex {check['pattern']!r} did not match")

    if ctype == "regex_not_match":
        # PASS when the pattern does NOT match anywhere (the deterministic "must be absent"
        # primitive for structured shapes — e.g. a `tools:` line that contains Edit/Write).
        flags = re.IGNORECASE if case_insensitive else 0
        if check.get("multiline"):
            flags |= re.MULTILINE
        matched = _search_wrapsafe(check["pattern"], target, flags)
        return res(not matched,
                   f"regex {check['pattern']!r} correctly absent" if not matched
                   else f"forbidden regex {check['pattern']!r} matched")

    if ctype == "max_length":
        limit = int(check["value"])
        n = len(target)
        return res(n <= limit, f"len {n} <= {limit}" if n <= limit
                               else f"len {n} exceeds limit {limit}")

    if ctype == "min_length":
        limit = int(check["value"])
        n = len(target)
        return res(n >= limit, f"len {n} >= {limit}" if n >= limit
                               else f"len {n} below min {limit}")

    if ctype == "score_threshold":
        # candidate field holds a number (e.g. a self-reported or upstream metric);
        # compare against a threshold. op in >=,>,<=,<,==
        op = check.get("op", ">=")
        thr = float(check["value"])
        val = float(target.strip())
        ops = {">=": val >= thr, ">": val > thr, "<=": val <= thr,
               "<": val < thr, "==": val == thr}
        if op not in ops:
            raise ValueError(f"score_threshold: bad op {op!r}")
        return res(ops[op], f"{val} {op} {thr} -> {ops[op]}")

    if ctype == "budget":
        # EFFICIENCY assertion: FAIL if a usage metric exceeds its budget. Operationalizes
        # the Cost dimension (EVAL-RUBRIC §25 token/latency budget vs value). Targets a usage
        # field on a JSON candidate (e.g. field="usage.total_tokens"); op defaults to "<="
        # (a budget is an UPPER bound). The runner RECORDS metrics separately — this check is
        # the gate. metric label is cosmetic (tokens/latency_ms/cost_usd).
        # A budget is an upper bound BY DEFINITION, so only <= / < are valid ops. Allowing
        # >= / > would let an over-budget candidate score "within budget" (a false-PASS on
        # misuse — Codex 2026-06-16). Reject anything else (fail-closed, not silent).
        op = check.get("op", "<=")
        if op not in ("<=", "<"):
            raise ValueError(f"budget: op must be <= or < (a budget is an upper bound); got {op!r}")
        thr = float(check["value"])
        raw = _extract_raw(candidate, check.get("field"))
        try:
            val = float(raw if isinstance(raw, (int, float)) else str(raw).strip())
        except (TypeError, ValueError):
            return res(False, f"budget: metric {check.get('field')!r} is not a number: {raw!r}")
        metric = check.get("metric", check.get("field", "metric"))
        ok = (val <= thr) if op == "<=" else (val < thr)
        return res(ok, f"{metric}={val} {op} {thr} -> {'within' if ok else 'OVER'} budget")

    if ctype == "tool_trajectory":
        # STEPS/TRAJECTORY (ADK tool_trajectory_avg_score pattern): does the agent's actual
        # tool-call sequence match the expected one? Modes: EXACT (same calls, same order),
        # IN_ORDER (expected is an ordered subsequence of actual — extra calls allowed between),
        # ANY_ORDER (same multiset, order-independent). Compares tool NAMES by default; the
        # actual list is a JSON list field on the candidate (field=...), expected is in the check.
        mode = str(check.get("mode", "EXACT")).upper()
        raw_actual = _extract_raw(candidate, check.get("field"))
        if isinstance(raw_actual, str):
            # field not set or pointed at a string — try to parse it as a JSON list
            try:
                raw_actual = json.loads(raw_actual)
            except json.JSONDecodeError:
                return res(False, f"tool_trajectory: actual is not a JSON list: {raw_actual[:80]!r}")
        if not isinstance(raw_actual, list):
            return res(False, f"tool_trajectory: actual must be a list, got {type(raw_actual).__name__}")

        def _names(seq: list[Any]) -> list[str]:
            # each element is either a tool-name string or a {"tool"/"name": ...} dict
            out = []
            for el in seq:
                if isinstance(el, str):
                    out.append(el)
                elif isinstance(el, dict):
                    out.append(str(el.get("tool") or el.get("name") or el.get("tool_name") or ""))
                else:
                    out.append(str(el))
            return out

        actual = _names(raw_actual)
        expected = _names(check["expected"])

        if mode == "EXACT":
            ok = actual == expected
            detail = (f"exact match {actual}" if ok
                      else f"expected {expected}, got {actual}")
        elif mode == "IN_ORDER":
            # expected must appear as an ordered subsequence of actual
            it = iter(actual)
            ok = all(any(a == e for a in it) for e in expected)
            detail = (f"{expected} appears in order within {actual}" if ok
                      else f"{expected} is NOT an ordered subsequence of {actual}")
        elif mode == "ANY_ORDER":
            ok = sorted(actual) == sorted(expected)
            detail = (f"same set/multiset {sorted(actual)}" if ok
                      else f"multiset mismatch: expected {sorted(expected)}, got {sorted(actual)}")
        else:
            raise ValueError(f"tool_trajectory: bad mode {mode!r} (EXACT|IN_ORDER|ANY_ORDER)")
        return res(ok, f"[{mode}] {detail}")

    raise ValueError(f"unknown check type: {ctype!r} (check {name!r})")


# --------------------------------------------------------------------------- #
# Per-task evaluation
# --------------------------------------------------------------------------- #

def extract_efficiency(candidate: str) -> dict[str, Any] | None:
    """RECORD efficiency metrics (the dev->prod seam) — independent of any budget check.
    If the candidate is JSON carrying a `usage` object (tokens/latency/cost), surface it.
    Honest framing: the runner makes no model call, so it cannot MEASURE tokens — it RECORDS
    what the candidate run reports. Returns None if there are no metrics to record.
    If a `usage.price_per_1k_tokens` (or `price_per_1k`) is present and total_tokens is known,
    cost_usd is DERIVED here so the report has a cost even when the run didn't compute one."""
    try:
        obj = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(obj, dict):
        return None
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    eff: dict[str, Any] = {}
    for k in ("total_tokens", "input_tokens", "output_tokens",
              "latency_ms", "cost_usd"):
        if k in usage:
            eff[k] = usage[k]
    # Derive cost if a price is supplied and not already given.
    price = usage.get("price_per_1k_tokens", usage.get("price_per_1k"))
    if "cost_usd" not in eff and price is not None and "total_tokens" in eff:
        try:
            eff["cost_usd"] = round(float(eff["total_tokens"]) / 1000.0 * float(price), 6)
            eff["cost_derived"] = True
        except (TypeError, ValueError):
            pass
    return eff or None


def eval_deterministic(task: GoldenTask, candidate: str) -> dict[str, Any]:
    results = []
    deferred_checks = []  # hallucination checks: model-judged, can't run synchronously
    for chk in task.checks:
        if chk.get("type") == "hallucination":
            # A DEFERRED check — emit the reference-free hallucination prompt; the in-session
            # model answers it, then ingest-hallucination recomputes the rate. Until then this
            # check is UNRESOLVED, so the whole task is deferred (never a silent PASS).
            context = chk.get("context") or task.reference or task.input
            # Field-scope: judge the ANSWER PROSE, not the JSON envelope. Without this, a
            # candidate that is a run record (e.g. {answer, tool_calls, usage}) would feed the
            # tool_calls/usage metadata to the judge, which would (correctly) flag "total_tokens
            # was 1450" as an unsupported claim and FALSE-FAIL a grounded answer (advisor 2026-06-16).
            try:
                hcand = _extract_field(candidate, chk.get("field"))
            except (KeyError, ValueError, json.JSONDecodeError, IndexError) as e:
                deferred_checks.append({
                    "name": chk.get("name", "hallucination"), "type": "hallucination",
                    "passed": False, "deferred": False,
                    "detail": f"hallucination: cannot extract field {chk.get('field')!r}: {e}",
                })
                continue
            deferred_checks.append({
                "name": chk.get("name", "hallucination"),
                "type": "hallucination",
                "passed": None,
                "deferred": True,
                "max_rate": float(chk.get("max_rate", 0.0)),
                "hallucination_prompt": build_hallucination_prompt(context, hcand),
                "detail": "DEFERRED — run hallucination-prompt then ingest-hallucination (in-session)",
            })
            continue
        try:
            results.append(run_check(chk, candidate))
        except (KeyError, ValueError, json.JSONDecodeError, IndexError) as e:
            results.append({"name": chk.get("name", "?"), "type": chk.get("type"),
                            "passed": False, "detail": f"check error: {e}"})

    sync_all_pass = bool(results) and all(r["passed"] for r in results)
    n_pass = sum(1 for r in results if r["passed"])
    eff = extract_efficiency(candidate)

    # A hallucination check is either genuinely DEFERRED (passed is None) or it HARD-FAILED
    # synchronously (passed is False — e.g. a missing `field`, which must FAIL fail-closed, not
    # be masked as "deferred"). Separate the two so a hard fail makes the task FAIL.
    pending = [c for c in deferred_checks if c.get("deferred")]
    halluc_hard_fail = any(c.get("passed") is False for c in deferred_checks)

    out: dict[str, Any] = {
        "id": task.id, "type": "deterministic",
        "criteria": results + deferred_checks,
        "score": (n_pass / len(results)) if results else 0.0,
    }
    if eff:
        out["efficiency"] = eff
    if halluc_hard_fail:
        # A hallucination check that could not even be prepared (bad field) FAILS the task
        # fail-closed — it is not "pending", it is broken, and broken is not a pass.
        out["passed"] = False
        # Keep the pending flag (added 2026-09-16, Law-7 gate C2): one check failing to
        # PREPARE does not resolve a DIFFERENT check that is still awaiting judgement.
        # Without this the row lost its deferred marker and the suite reported
        # complete=True with a criterion still pending. The failure verdict is unchanged.
        out["deferred"] = bool(pending)
        out["summary"] = (f"{n_pass}/{len(results)} sync checks passed; "
                          f"a hallucination check FAILED fail-closed (could not prepare)"
                          + (f"; {len(pending)} other groundedness check(s) STILL PENDING" if pending else ""))
    elif pending and results and not sync_all_pass:
        # `results` guard added 2026-09-16 (Law-7 cross-model gate, C1 — REFUTED my own
        # previous fix). `sync_all_pass` is `bool(results) and all(...)`, so it is FALSE for an
        # EMPTY result list too. Without this guard a task carrying ONLY a pending groundedness
        # check -- nothing measured at all -- was reported as a FAILURE (exit 1). That is the
        # exact mirror of the bug this branch was added to fix, introduced by the fix itself.
        # A FAILED sync check OUTRANKS a pending judge leg (added 2026-09-15; the acceptance
        # gate found this live on golden task 005, which carries six runnable checks AND a
        # hallucination check). Before this, any one of those six failing produced passed=None
        # -> "nothing failed, a judgement is owed" -> CLI exit 2, whose contract says
        # "everything measurable passed". Everything measurable had NOT passed. The README rule
        # this instrument exists to serve -- "a change that makes a passing golden task fail is
        # a BLOCK" -- was the exact signal being downgraded. `deferred` stays set so the owed
        # judge leg is still visible; `passed: False` is what the exit code and the human mark
        # both key on.
        out["passed"] = False
        out["deferred"] = True
        out["summary"] = (f"{n_pass}/{len(results)} sync checks passed — the task FAILED on what "
                          f"could be measured; a judge leg is ALSO owed (both are true, the "
                          f"failure is the verdict)")
    elif pending:
        # Unresolved groundedness => the task is DEFERRED, not PASS — even if every sync check
        # passed. all_runnable_passed must not go green while a hallucination check is pending.
        out["passed"] = None
        out["deferred"] = True
        out["summary"] = (f"{n_pass}/{len(results)} sync checks passed; "
                          f"{len(pending)} hallucination check(s) DEFERRED "
                          f"(run hallucination-prompt -> ingest-hallucination)")
    else:
        out["passed"] = sync_all_pass
        out["summary"] = f"{n_pass}/{len(results)} checks passed"
    return out


JUDGE_VERDICT_SCHEMA = {
    "criteria": [{"name": "rubric dim", "score": 4, "reason": "one line (score is a NUMBER 0-5)"}],
    "overall_score": 4.0,
    "verdict": "PASS",
    "notes": "optional one line",
}


def build_judge_prompt(task: GoldenTask, candidate: str) -> str:
    """Emit a structured LLM-as-judge prompt. The IN-SESSION model answers this
    (native-only — the runner does NOT call an API). It grades a candidate supplied
    as INPUT, never its own output (maker != judge)."""
    # Render the FULL bullet, not the label. The label stays the coverage key; the body is the
    # scoring rule, and a judge that never sees "scores 0 if X" cannot apply it.
    rubric_lines = getattr(task, "rubric_text", None) or task.rubric or [
        "Faithfulness — grounded in the reference, no fabricated claims",
        "Completeness — covers what the reference covers",
        "Correctness — factually right vs the reference bar",
    ]
    rubric_block = "\n".join(f"  - {r}" for r in rubric_lines)
    schema = json.dumps(JUDGE_VERDICT_SCHEMA, indent=2)
    floor = getattr(task, "min_dimension", 2.0)
    return f"""You are an INDEPENDENT judge (not the author of the candidate). Score the
candidate output against the reference bar on the rubric below. Be calibrated and
strict: fluent prose is not correctness (length/fluency bias is a known judge failure).
Pass only if the candidate genuinely meets the reference bar.

TASK ID: {task.id}

INPUT THE SYSTEM WAS GIVEN:
{task.input}

REFERENCE (the known-good bar — a staff-engineer / top-lab answer):
{task.reference}

CANDIDATE OUTPUT TO JUDGE:
{candidate}

RUBRIC DIMENSIONS (score each 0-5):
{rubric_block}

Return ONLY valid JSON in exactly this shape (no prose around it):
{schema}

Name each "criteria" entry with a rubric dimension label VERBATIM (detail may follow
after it) — coverage is checked one-to-one against the rubric, so a renamed or merged
criterion fails coverage even if the scoring is sound.

Set "verdict" to "PASS" only if overall_score >= 3.5 AND no single criterion is below {floor:g}.
"""


def _coerce_score(raw: Any) -> float | None:
    """Coerce a judge-supplied score to a float. Returns None if it can't be parsed
    (a missing/garbage score must NOT be silently skipped — that's the false-PASS hole
    Codex flagged: a string "1" or an absent score would dodge the <2 floor)."""
    if isinstance(raw, bool):  # bool is a subclass of int — reject it explicitly
        return None
    if isinstance(raw, (int, float)):
        f = float(raw)
        return f if math.isfinite(f) else None   # NaN/inf are not scores (B-05, fail-closed)
    if isinstance(raw, str):
        try:
            f = float(raw.strip())
        except ValueError:
            return None
        return f if math.isfinite(f) else None
    return None


def ingest_verdict(task: GoldenTask, verdict: dict[str, Any]) -> dict[str, Any]:
    """Parse a judge verdict (produced in-session) into a task result. FAIL-CLOSED:
    any unparseable/missing score makes the task FAIL, so a malformed verdict can never
    false-PASS (anti-reward-hacking — the verdict label is never trusted on its own)."""
    raw_v = str(verdict.get("verdict", "")).upper().strip()
    overall = _coerce_score(verdict.get("overall_score"))
    crit = verdict.get("criteria", [])
    if not isinstance(crit, list):
        crit = []                                  # a non-list criteria block is zero criteria

    # Every criterion must carry a parseable, FINITE score in [0,5], and none may be below 2.
    coerced = [(c, _coerce_score(c.get("score")) if isinstance(c, dict) else None) for c in crit]
    unparseable = [c for c, s in coerced if s is None]
    out_of_bounds = [c for c, s in coerced if s is not None and not (0.0 <= s <= 5.0)]
    floor = getattr(task, "min_dimension", 2.0)
    below_floor = [c for c, s in coerced if s is not None and 0.0 <= s <= 5.0 and s < floor]
    # The judge's overall_score is a CLAIM; the pass decision uses the mean of the criteria it
    # scored (added 2026-09-26 — the review of the golden repair found the self-reported overall
    # was taken as given, which matters more once a task has several dimensions).
    in_range = [s for _, s in coerced if s is not None and 0.0 <= s <= 5.0]
    recomputed = (sum(in_range) / len(in_range)) if (crit and len(in_range) == len(coerced)) else None
    # When the task declares a rubric, average ONLY the criteria matched one-to-one to its dimensions:
    # invented or duplicate criteria at 5 could otherwise lift failing dimensions past 3.5
    # (second review, 2026-09-27). Matching is recomputed below by the coverage loop.

    reasons = []
    if raw_v != "PASS":
        reasons.append(f"verdict label is {raw_v or 'empty'!r}, not PASS")
    if not crit:
        reasons.append("zero criteria in verdict — no per-dimension evidence can never PASS")
    if overall is None:
        reasons.append("overall_score missing/unparseable/non-finite")
    elif not (0.0 <= overall <= 5.0):
        reasons.append(f"overall_score {overall} outside the [0,5] scale")
    elif (recomputed if recomputed is not None else overall) < 3.5:
        eff = recomputed if recomputed is not None else overall
        reasons.append(f"criteria mean {eff:.2f} < 3.5" + (f" (judge claimed {overall})" if recomputed is not None and abs(recomputed - overall) > 0.05 else ""))
    if unparseable:
        reasons.append(f"{len(unparseable)} criterion score(s) unparseable/non-finite")
    if out_of_bounds:
        reasons.append(f"{len(out_of_bounds)} criterion score(s) outside the [0,5] scale")
    if below_floor:
        reasons.append(f"{len(below_floor)} criterion score(s) below the {floor:g} floor")
    # Rubric coverage (B-05): every rubric dimension the task declares must be scored in the
    # verdict — a judge that silently drops a dimension must not PASS on the ones it liked.
    # ONE-TO-ONE (Codex gate 2026-07-23): each criterion satisfies at most ONE dimension, so a
    # single catch-all criterion (the old "subjective" head, or a 1-char name) can no longer
    # cover them all. A criterion matches a dimension only if its name is >=3 chars and equals
    # or contains the dimension label.
    if task.rubric:
        names = [str(c.get("name") or "").strip().lower() for c in crit if isinstance(c, dict)]
        used = [False] * len(names)
        missing = []
        for dim in task.rubric:
            dim_l = dim.strip().lower()
            hit = next((i for i, n in enumerate(names)
                        if not used[i] and len(n) >= 3 and (n == dim_l or dim_l in n)), None)
            if hit is None:
                missing.append(dim_l)
            else:
                used[hit] = True
        if missing:
            reasons.append(f"rubric coverage: {len(missing)} dimension(s) missing from the "
                           f"verdict criteria ({', '.join(missing)})")

    if task.rubric and recomputed is not None:
        names_all = [str(c.get("name") or "").strip().lower() if isinstance(c, dict) else "" for c in crit]
        taken, matched_scores = [False] * len(names_all), []
        for dim in task.rubric:
            dim_l = dim.strip().lower()
            hit = next((i for i, n in enumerate(names_all)
                        if not taken[i] and len(n) >= 3 and (n == dim_l or dim_l in n)), None)
            if hit is not None:
                taken[hit] = True
                matched_scores.append(coerced[hit][1])
        if matched_scores and all(x is not None for x in matched_scores):
            rubric_mean = sum(matched_scores) / len(matched_scores)
            if rubric_mean < 3.5 and not any(r.startswith("criteria mean") for r in reasons):
                reasons.append(f"rubric-dimension mean {rubric_mean:.2f} < 3.5 (extra/duplicate criteria are not averaged in)")
            recomputed = rubric_mean
    passed = not reasons  # PASS only if every condition held — fail-closed
    return {
        "id": task.id, "type": "judge", "passed": passed,
        "criteria": crit, "score": (overall if overall is not None else 0.0),
        # `verdict` is the RECOMPUTED result, not the judge's self-report (changed 2026-09-15).
        # It used to carry raw_v, so a record could read "verdict": "PASS" directly above
        # "passed": false when the recompute caught an inflated label -- the machinery read the
        # right field and a human scanning the obvious one read the wrong answer. That is the
        # same display-disagrees-with-truth class fixed in the exit contract the same day. The
        # judge's own claim is kept for provenance under its own honest name.
        "verdict": ("PASS" if passed else "FAIL"),
        "judge_claimed_verdict": raw_v,
        "criteria_mean": recomputed,
        "summary": (f"judge verdict {raw_v}, overall {overall} (recomputed pass={passed})"
                    if passed else
                    f"judge FAIL (recomputed): {'; '.join(reasons)}"),
        "fail_reasons": reasons,
        "notes": verdict.get("notes", ""),
    }


# --------------------------------------------------------------------------- #
# Hallucination measurement (ADK hallucinations_v1 pattern, reference-FREE)
# --------------------------------------------------------------------------- #
# Unlike the judge path (which needs a Reference/golden answer), hallucination is judged
# against the PROVIDED CONTEXT/SOURCE only: split the candidate into atomic claims, judge
# each supported|unsupported against the context, output a hallucination RATE. It is a
# deferred check (irreducibly a model task — the in-session model answers the prompt). The
# runner emits the prompt and, on ingest, RECOMPUTES the rate from per-claim verdicts — it
# NEVER trusts a judge-supplied rate or label (same discipline as ingest_verdict).

HALLUCINATION_VERDICT_SCHEMA = {
    "claims": [
        {"claim": "an atomic factual statement lifted from the candidate",
         "verdict": "supported",
         "reason": "one line — which part of the context supports/contradicts it"},
    ],
    "notes": "optional one line",
}


def build_hallucination_prompt(context: str, candidate: str) -> str:
    """Emit a reference-free hallucination-grading prompt. The in-session model answers it
    (native-only — no API call). It does NOT need a golden answer: every claim is judged
    ONLY against the supplied context/source."""
    schema = json.dumps(HALLUCINATION_VERDICT_SCHEMA, indent=2)
    return f"""You are a strict GROUNDEDNESS checker. Decompose the CANDIDATE OUTPUT into
atomic factual claims (one verifiable assertion each), then judge EACH claim ONLY against
the CONTEXT below — not your own world knowledge. A claim is:
  - "supported"   : the context directly entails it.
  - "unsupported" : the context does not support it (it is fabricated, contradicted, or an
                    unverifiable addition). Hedges, opinions, and pure restatements of the
                    question are NOT claims — omit them.
Be exhaustive: extract every checkable claim. An ungrounded-but-fluent claim is the failure
this measures — do not give fluency the benefit of the doubt.

CONTEXT (the ONLY source of truth — judge against this, nothing else):
{context}

CANDIDATE OUTPUT TO CHECK:
{candidate}

Return ONLY valid JSON in exactly this shape (no prose around it). Use the literal strings
"supported" / "unsupported" for each verdict:
{schema}
"""


def ingest_hallucination_verdict(
    verdict: dict[str, Any],
    max_rate: float = 0.0,
    name: str = "hallucination",
) -> dict[str, Any]:
    """Parse a per-claim hallucination verdict into a check result. FAIL-CLOSED + recompute:
      - the rate is RECOMPUTED from per-claim verdicts (a judge-supplied rate/label is ignored).
      - any per-claim verdict that is not exactly "supported" counts as UNSUPPORTED — a
        missing/garbage/empty verdict is unsupported, never skipped (analog of _coerce_score).
      - ZERO claims extracted = fail-closed (cannot prove groundedness of an unparsed output).
      - PASS only if recomputed rate <= max_rate (default 0.0 — no tolerated hallucination).
    Direction: HIGH rate = FAIL."""
    # max_rate is a RATE, so it must be in [0, 1] and finite. A garbage/out-of-range value
    # (e.g. 2.0 or inf) would let a 100%-hallucinated output "pass" — fail-closed on it
    # (Codex 2026-06-16) rather than trust an unvalidated threshold.
    try:
        mr = float(max_rate)
    except (TypeError, ValueError):
        mr = float("nan")
    if not math.isfinite(mr) or mr < 0.0 or mr > 1.0:
        return {
            "name": name, "type": "hallucination", "passed": False,
            "hallucination_rate": 1.0, "n_claims": 0, "n_unsupported": 0,
            "max_rate": max_rate, "claims": [],
            "detail": f"fail-closed: max_rate {max_rate!r} is not a valid rate in [0,1]",
        }
    max_rate = mr
    claims = verdict.get("claims")
    if not isinstance(claims, list) or not claims:
        return {
            "name": name, "type": "hallucination", "passed": False,
            "hallucination_rate": 1.0, "n_claims": 0, "n_unsupported": 0,
            "max_rate": max_rate, "claims": [],
            "detail": "fail-closed: no claims extracted (cannot verify groundedness)",
        }
    total = len(claims)
    unsupported = []
    for c in claims:
        v = c.get("verdict") if isinstance(c, dict) else None
        # Anything that is not exactly "supported" is treated as unsupported (fail-closed).
        if str(v).strip().lower() != "supported":
            unsupported.append(c)
    rate = len(unsupported) / total
    passed = rate <= max_rate
    return {
        "name": name, "type": "hallucination", "passed": passed,
        "hallucination_rate": round(rate, 4),
        "n_claims": total, "n_unsupported": len(unsupported),
        "max_rate": max_rate,
        "claims": claims,
        "detail": (f"hallucination rate {rate:.0%} ({len(unsupported)}/{total} unsupported) "
                   f"{'<=' if passed else '>'} max {max_rate:.0%} -> "
                   f"{'GROUNDED' if passed else 'HALLUCINATING'}"),
    }


# --------------------------------------------------------------------------- #
# Candidate resolution + orchestration
# --------------------------------------------------------------------------- #

def resolve_candidate(task: GoldenTask, candidates_dir: Path) -> str | None:
    base = candidates_dir.resolve()
    for name in (f"{task.id}.txt", f"{task.id}.json", f"{task.id}.md"):
        p = candidates_dir / name
        if p.exists():
            resolved = p.resolve(strict=True)
            try:
                resolved.relative_to(base)
            except ValueError as exc:
                raise ValueError("candidate resolves outside candidate directory") from exc
            return resolved.read_text(encoding="utf-8")
    return None


def run_suite(tasks: list[GoldenTask], candidates_dir: Path) -> dict[str, Any]:
    results = []
    # A task `load_tasks` REFUSED must appear in the report as a hard failure. Dropping it from
    # the suite silently would be worse than the false-GREEN the refusal exists to prevent: the
    # task would simply cease to be graded and the run would still go green (observed exactly
    # once, 2026-07-29, in the first cut of this change — the silent-no-match class).
    for msg in getattr(load_tasks, "broken", []) or []:
        results.append({"id": msg.split(":", 1)[0], "type": "unparseable", "passed": False,
                        "error": "unparseable task", "summary": f"ERROR {msg}"})
    for task in tasks:
        try:
            candidate = resolve_candidate(task, candidates_dir)
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            detail = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
            results.append({"id": task.id, "type": task.type, "passed": False,
                            "error": "candidate unavailable",
                            "summary": f"ERROR candidate unavailable: {detail}"})
            continue
        if candidate is None:
            results.append({"id": task.id, "type": task.type, "passed": False,
                            "error": "no candidate found",
                            "summary": f"ERROR no candidate ({task.id}.txt) in {candidates_dir}"})
            continue
        # ---- BOTH LEGS RUN (changed 2026-07-29). `type:` was a ROUTER; it is now a DECLARATION.
        # Two independent audits found the same root cause: this branch was a hard either/or, so
        # `eval_deterministic` never read `task.rubric` and the judge branch never read
        # `task.checks`. Consequence, measured: 11 of 17 golden tasks are `type: deterministic`
        # AND declare a `subjective:` bullet, so ELEVEN rubrics were parsed and then silently
        # discarded — including golden 009's own headline rule ("a proposal that ships an edit is
        # an automatic fail"), which made the rule the task exists to enforce into dead code. In
        # the other direction, one word (`type: judge`) silenced a whole check suite: flipping 005
        # dropped 4 hard failures AND its groundedness gate.
        # Now: run the deterministic engine whenever there are checks, and attach the judge prompt
        # whenever there is a rubric. `type:` still records the PRIMARY gate for reporting.
        det = eval_deterministic(task, candidate) if task.checks else None
        needs_judge = bool(task.rubric) or (not task.checks)
        if det is not None and not needs_judge:
            results.append(det)
        elif det is None:
            results.append({"id": task.id, "type": "judge", "passed": None,
                            "deferred": True,
                            "judge_prompt": build_judge_prompt(task, candidate),
                            "summary": "JUDGE task — run judge-prompt then ingest-verdict (in-session)"})
        else:
            # Hybrid: a deterministic verdict NOW, plus a judge leg still owed. `passed` carries
            # the deterministic result so a mechanical failure is never masked by a pending judge,
            # and `deferred` keeps the run honest that the task is not fully scored yet.
            merged = dict(det)
            merged["type"] = f"{task.type}+judge"
            merged["deferred"] = True
            merged["judge_prompt"] = build_judge_prompt(task, candidate)
            merged["summary"] = (f"{det.get('summary', '')} — PLUS a judge leg owed "
                                 f"({len(task.rubric)} rubric dimension(s)); run judge-prompt "
                                 f"then ingest-verdict to complete it")
            results.append(merged)
    runnable = [r for r in results if r.get("passed") is not None]
    n_pass = sum(1 for r in runnable if r["passed"])
    deferred = [r for r in results if r.get("deferred")]
    # A task is hallucination-deferred if it carries a deferred hallucination CHECK (vs a
    # whole judge task). Surfaced separately so the green line is honest about WHY it deferred.
    halluc_deferred = [r for r in deferred
                       if any(c.get("type") == "hallucination" and c.get("deferred")
                              for c in r.get("criteria", []))]
    return {
        "results": results,
        "aggregate": {
            "total": len(results),
            "deterministic_runnable": len(runnable),
            "passed": n_pass,
            "failed": len(runnable) - n_pass,
            "deferred_judge": len(deferred) - len(halluc_deferred),
            "deferred_hallucination": len(halluc_deferred),
            # GREEN requires: >=1 runnable task, all runnable passed, AND no pending
            # hallucination check (an unverified groundedness check is never a pass — the
            # silent-false-green guard). A deferred whole-JUDGE task is a separate, already-
            # understood "not yet graded" state and does not, by itself, flip green here.
            "all_runnable_passed": (len(runnable) > 0 and n_pass == len(runnable)
                                    and not halluc_deferred),
            # SEPARATE from the pass flag, added 2026-09-15 (cross-vendor finding F12).
            # "every deterministic check passed" and "the evaluation is finished" are
            # different facts, and the exit code used to conflate them: a task with a
            # passing keyword check AND an ungraded safety rubric returned exit 0. A
            # caller keying off $? could not tell a finished green run from one whose
            # judge had not answered. The printed line was always honest ("ALL
            # DETERMINISTIC RUNNABLE PASSED"); the exit code was not.
            "complete": not deferred,
        },
    }


def _print_human(report: dict[str, Any]) -> None:
    agg = report["aggregate"]
    print("eval-runner — golden-task report")
    print("=" * 50)
    for r in report["results"]:
        if r.get("error"):
            mark = "ERROR"
        elif r.get("passed") is False:
            # A FAILED deterministic leg outranks a pending judge leg in the HUMAN mark. Before
            # this, a hybrid row whose checks failed printed `[DEFER]` — the JSON, exit code and
            # footer were all correct, but the one line a reader scans said "not graded yet"
            # instead of "failed". That masks a mechanical failure in exactly the register this
            # session's own legibility standard governs.
            mark = "FAIL "
        elif r.get("deferred"):
            mark = "DEFER"
        else:
            mark = "PASS " if r["passed"] else "FAIL "
        print(f"[{mark}] {r['id']:<28} {r.get('summary','')}")
        for c in r.get("criteria", []):
            cm = ".. " if c.get("passed") is None else ("ok " if c.get("passed") else "x  ")
            print(f"         - {cm} {c.get('name','?'):<22} {c.get('detail','')}")
        eff = r.get("efficiency")
        if eff:
            metrics = ", ".join(f"{k}={v}" for k, v in eff.items() if k != "cost_derived")
            print(f"         - eff  {metrics}")
    print("-" * 50)
    print(f"total {agg['total']} | deterministic {agg['deterministic_runnable']} "
          f"(pass {agg['passed']} / fail {agg['failed']}) | judge-deferred {agg['deferred_judge']} "
          f"| hallucination-deferred {agg.get('deferred_hallucination', 0)}")
    print(f"ALL DETERMINISTIC RUNNABLE PASSED: {agg['all_runnable_passed']}")
    if not agg.get("complete", True):
        n_pending = agg["deferred_judge"] + agg.get("deferred_hallucination", 0)
        print(f"EVALUATION INCOMPLETE: {n_pending} task(s) still awaiting a judgment — "
              f"this is NOT a pass. Run judge-prompt, then ingest-verdict.")


# --------------------------------------------------------------------------- #
# Self-test — proves a PASS *and* a FAIL (works.all, not works.any)
# --------------------------------------------------------------------------- #

def self_test() -> int:
    print("eval-runner self-test (deterministic path, end-to-end)\n")
    failures = 0

    def expect(label: str, got: bool, want: bool) -> None:
        nonlocal failures
        ok = got == want
        if not ok:
            failures += 1
        print(f"  [{'ok' if ok else 'XX'}] {label}: got passed={got}, want {want}")

    # 1. A passing task: candidate meets every check.
    good_task = GoldenTask(
        id="st-good", type="deterministic",
        input="x", reference="x",
        checks=[
            {"name": "has-name", "type": "contains", "value": "name:"},
            {"name": "under-1024", "type": "max_length", "value": 1024},
            {"name": "no-tbd", "type": "not_contains", "value": "TODO", "ignore_case": True},
            {"name": "looks-like-frontmatter", "type": "regex", "pattern": r"^---"},
        ],
        rubric=[], path=Path("(memory)"),
    )
    good_candidate = "---\nname: eval-runner\ndescription: a clean test candidate\n---"
    r_good = eval_deterministic(good_task, good_candidate)
    expect("good candidate PASSES", r_good["passed"], True)

    # 2. A FAILING task: same checks, deliberately-bad candidate. MUST fail.
    bad_candidate = "name absent here, and this one has a todo: TODO finish this"
    r_bad = eval_deterministic(good_task, bad_candidate)
    expect("bad candidate FAILS", r_bad["passed"], False)
    # per-criterion: the 'has-name' contains check must have caught it
    has_name = next(c for c in r_bad["criteria"] if c["name"] == "has-name")
    expect("  -> per-criterion 'has-name' flagged", has_name["passed"], False)
    no_tbd = next(c for c in r_bad["criteria"] if c["name"] == "no-tbd")
    expect("  -> per-criterion 'no-tbd' flagged", no_tbd["passed"], False)

    # 3. score_threshold both directions.
    thr_task = GoldenTask(id="st-thr", type="deterministic", input="", reference="",
                          checks=[{"name": "recall", "type": "score_threshold",
                                   "op": ">=", "value": 0.8}],
                          rubric=[], path=Path("(memory)"))
    expect("0.9 >= 0.8 PASSES", eval_deterministic(thr_task, "0.9")["passed"], True)
    expect("0.5 >= 0.8 FAILS", eval_deterministic(thr_task, "0.5")["passed"], False)

    # 3b. regex_not_match — the "must be absent" structured primitive (Law-11 tool-scope).
    rnm_task = GoldenTask(id="st-rnm", type="deterministic", input="", reference="",
                          checks=[{"name": "no-write-in-tools", "type": "regex_not_match",
                                   "pattern": r"(?m)^tools:.*\bWrite\b"}],
                          rubric=[], path=Path("(memory)"))
    expect("read-only tools line PASSES regex_not_match",
           eval_deterministic(rnm_task, "tools: Read, Grep, Glob, Bash")["passed"], True)
    expect("tools line WITH Write FAILS regex_not_match",
           eval_deterministic(rnm_task, "tools: Read, Edit, Write, Bash")["passed"], False)

    # 4. JSON-field targeting.
    fld_task = GoldenTask(id="st-fld", type="deterministic", input="", reference="",
                          checks=[{"name": "status", "type": "exact",
                                   "field": "status", "value": "ok"}],
                          rubric=[], path=Path("(memory)"))
    expect("json field status==ok PASSES",
           eval_deterministic(fld_task, '{"status": "ok", "n": 3}')["passed"], True)
    expect("json field status==bad FAILS",
           eval_deterministic(fld_task, '{"status": "bad"}')["passed"], False)

    # 5. Judge prompt emits + verdict ingest round-trip (the native judge seam).
    judge_task = GoldenTask(id="st-judge", type="judge",
                            input="Summarize X.", reference="A faithful summary of X.",
                            checks=[], rubric=["Faithfulness", "Completeness"],
                            path=Path("(memory)"))
    prompt = build_judge_prompt(judge_task, "Some candidate summary.")
    expect("judge prompt contains rubric", "Faithfulness" in prompt, True)
    expect("judge prompt asks for JSON", "valid JSON" in prompt, True)
    pass_verdict = {"criteria": [{"name": "Faithfulness", "score": 4},
                                 {"name": "Completeness", "score": 4}],
                    "overall_score": 4.0, "verdict": "PASS"}
    expect("good verdict -> PASS", ingest_verdict(judge_task, pass_verdict)["passed"], True)
    fail_verdict = {"criteria": [{"name": "Faithfulness", "score": 1}],
                    "overall_score": 2.0, "verdict": "PASS"}  # label lies; recompute catches it
    expect("verdict with a <2 criterion -> FAIL (recomputed, label ignored)",
           ingest_verdict(judge_task, fail_verdict)["passed"], False)
    # 2026-09-26: an inflated overall_score cannot carry weak criteria (mean is recomputed).
    inflated = {"criteria": [{"name": "Faithfulness", "score": 3},
                             {"name": "Completeness", "score": 3}],
                "overall_score": 4.0, "verdict": "PASS"}
    expect("overall_score inflated over the criteria mean -> FAIL (mean recomputed)",
           ingest_verdict(judge_task, inflated)["passed"], False)
    # 2026-09-26: a split task's per-dimension floor — 2+2+5+5 averages 3.5 but must FAIL at floor 3.
    split_task = GoldenTask(id="st-split", type="judge", input="", reference="", checks=[],
                            rubric=["alpha", "beta", "gamma", "delta"], path=Path("(memory)"),
                            min_dimension=3.0)
    split_v = {"criteria": [{"name": "alpha", "score": 2}, {"name": "beta", "score": 2},
                            {"name": "gamma", "score": 5}, {"name": "delta", "score": 5}],
               "overall_score": 3.5, "verdict": "PASS"}
    expect("split task: two dimensions at 2 cannot average past a min_dimension of 3",
           ingest_verdict(split_task, split_v)["passed"], False)
    expect("the same verdict passes the default floor of 2 (the loophole being closed)",
           ingest_verdict(GoldenTask(id="st-split2", type="judge", input="", reference="", checks=[],
                                     rubric=["alpha", "beta", "gamma", "delta"], path=Path("(memory)")),
                          split_v)["passed"], True)
    padded = {"criteria": [{"name": "alpha", "score": 3}, {"name": "beta", "score": 3},
                           {"name": "gamma", "score": 3}, {"name": "delta", "score": 3},
                           {"name": "invented-extra", "score": 5}, {"name": "alpha again", "score": 5}],
              "overall_score": 3.67, "verdict": "PASS"}
    expect("extra/duplicate criteria at 5 cannot lift a 3.0 rubric mean past 3.5",
           ingest_verdict(GoldenTask(id="st-pad", type="judge", input="", reference="", checks=[],
                                     rubric=["alpha", "beta", "gamma", "delta"], path=Path("(memory)")),
                          padded)["passed"], False)
    wrapped = "the denylist can never be\ncompleted, whatever you add"
    expect("a literal-space pattern survives a hard line wrap (wrap-safe regex)",
           run_check({"name": "w", "type": "regex", "pattern": "never be completed"}, wrapped)["passed"], True)
    expect("a line-anchored pattern is NOT matched against the collapsed copy",
           run_check({"name": "a", "type": "regex", "pattern": "(?m)^completed, whatever you add$x"}, wrapped)["passed"], False)
    expect("judge prompt states the task's own floor",
           "below 3." in build_judge_prompt(split_task, "x"), True)
    # Codex-found false-PASS hole (fixed): a STRING sub-2 score must still FAIL, not skip.
    str_score_verdict = {"criteria": [{"name": "Faithfulness", "score": "1"},
                                      {"name": "Completeness", "score": "5"}],
                         "overall_score": "4.0", "verdict": "PASS"}
    expect("string '1' sub-2 score -> FAIL (no false-pass via string skip)",
           ingest_verdict(judge_task, str_score_verdict)["passed"], False)
    # A MISSING criterion score must fail-closed, not be silently skipped.
    missing_score_verdict = {"criteria": [{"name": "Faithfulness"},  # no score key
                                          {"name": "Completeness", "score": 5}],
                             "overall_score": 4.5, "verdict": "PASS"}
    expect("missing criterion score -> FAIL (fail-closed)",
           ingest_verdict(judge_task, missing_score_verdict)["passed"], False)
    # All-string valid scores still PASS (string coercion works in the good direction too).
    str_good_verdict = {"criteria": [{"name": "Faithfulness", "score": "4"},
                                     {"name": "Completeness", "score": "4"}],
                        "overall_score": "4.0", "verdict": "PASS"}
    expect("all-string valid scores -> PASS (coercion works)",
           ingest_verdict(judge_task, str_good_verdict)["passed"], True)
    # B-05 hardening: zero-criteria PASS, NaN/inf, out-of-bounds, rubric coverage — fail-closed.
    zero_crit_verdict = {"criteria": [], "overall_score": 5.0, "verdict": "PASS"}
    expect("PASS with zero criteria -> FAIL (fail-closed)",
           ingest_verdict(judge_task, zero_crit_verdict)["passed"], False)
    nan_verdict = {"criteria": [{"name": "Faithfulness", "score": float("nan")},
                                {"name": "Completeness", "score": 4}],
                   "overall_score": 4.0, "verdict": "PASS"}
    expect("NaN criterion score -> FAIL (non-finite rejected)",
           ingest_verdict(judge_task, nan_verdict)["passed"], False)
    inf_verdict = {"criteria": [{"name": "Faithfulness", "score": 4},
                                {"name": "Completeness", "score": 4}],
                   "overall_score": float("inf"), "verdict": "PASS"}
    expect("inf overall_score -> FAIL (non-finite rejected)",
           ingest_verdict(judge_task, inf_verdict)["passed"], False)
    oob_verdict = {"criteria": [{"name": "Faithfulness", "score": 7},
                                {"name": "Completeness", "score": 4}],
                   "overall_score": 4.0, "verdict": "PASS"}
    expect("criterion score 7 outside [0,5] -> FAIL (bounds enforced)",
           ingest_verdict(judge_task, oob_verdict)["passed"], False)
    uncovered_verdict = {"criteria": [{"name": "Faithfulness", "score": 4}],
                         "overall_score": 4.0, "verdict": "PASS"}
    expect("rubric dimension missing from verdict -> FAIL (coverage check)",
           ingest_verdict(judge_task, uncovered_verdict)["passed"], False)
    # Codex gate 2026-07-23: rubric-coverage bypass — a single catch-all criterion (the old
    # shared "subjective" head) must NOT satisfy multiple named dimensions; coverage is 1:1.
    multi_dim_task = GoldenTask(id="st-multidim", type="judge", input="x", reference="y",
                                checks=[], rubric=["gate-chaining", "codex-down handling",
                                                   "reversibility-class autonomy", "no-bypass honesty"],
                                path=Path("(memory)"))
    catchall = {"criteria": [{"name": "subjective", "score": 5}], "overall_score": 5.0, "verdict": "PASS"}
    expect("catch-all 'subjective' criterion -> FAIL (no multi-dim coverage)",
           ingest_verdict(multi_dim_task, catchall)["passed"], False)
    one_covers = {"criteria": [{"name": "gate-chaining", "score": 5}], "overall_score": 5.0, "verdict": "PASS"}
    expect("one criterion cannot cover four dimensions -> FAIL",
           ingest_verdict(multi_dim_task, one_covers)["passed"], False)
    all_four = {"criteria": [{"name": d, "score": 4} for d in
                             ("gate-chaining", "codex-down handling",
                              "reversibility-class autonomy", "no-bypass honesty")],
                "overall_score": 4.0, "verdict": "PASS"}
    expect("all four dimensions scored distinctly -> PASS", ingest_verdict(multi_dim_task, all_four)["passed"], True)
    # Parser: real golden rubric bullets yield clean dimension LABELS, not the literal "subjective".
    _gdir = Path(__file__).resolve().parent.parent / "tasks" / "golden"
    if (_gdir / "006-autopilot-owner-gate.md").exists():
        six = load_task(_gdir / "006-autopilot-owner-gate.md")
        # Assert the PROPERTY, not the COUNT (lessons.md): the old form pinned `len == 4`, so
        # legitimately ADDING a rubric dimension broke the self-test and taught the next author to
        # bump the number without reading it. 006 gained two real dimensions on 2026-07-29 (an audit
        # found two of its four confounded by its own scenario). What actually matters is that every
        # bullet yields a DISTINCT, non-placeholder label — that is the defect the Codex gate found.
        expect("golden 006 rubric: >=4 distinct dims, none the bare word 'subjective'",
               len(six.rubric) >= 4
               and len(set(six.rubric)) == len(six.rubric)
               and all(d != "subjective" for d in six.rubric)
               and "gate-chaining" in six.rubric, True)
    if (_gdir / "004-digest-summary-judge.md").exists():
        four = load_task(_gdir / "004-digest-summary-judge.md")
        expect("golden 004 parses 3 dims (faithfulness/completeness/concision)",
               sorted(four.rubric) == ["completeness", "concision", "faithfulness"], True)

    # ----------------------------------------------------------------------- #
    # 6. EFFICIENCY — `budget` check-type (pass AND fail) + metric RECORDING.
    # ----------------------------------------------------------------------- #
    budget_task = GoldenTask(id="st-budget", type="deterministic", input="", reference="",
                             checks=[{"name": "token-budget", "type": "budget",
                                      "field": "usage.total_tokens", "value": 5000,
                                      "metric": "tokens"}],
                             rubric=[], path=Path("(memory)"))
    under = '{"answer": "ok", "usage": {"total_tokens": 1200, "latency_ms": 800}}'
    over = '{"answer": "ok", "usage": {"total_tokens": 9000, "latency_ms": 800}}'
    expect("tokens 1200 <= 5000 budget PASSES",
           eval_deterministic(budget_task, under)["passed"], True)
    expect("tokens 9000 <= 5000 budget FAILS",
           eval_deterministic(budget_task, over)["passed"], False)
    # latency budget direction too
    lat_task = GoldenTask(id="st-lat", type="deterministic", input="", reference="",
                          checks=[{"name": "latency", "type": "budget",
                                   "field": "usage.latency_ms", "value": 2000}],
                          rubric=[], path=Path("(memory)"))
    expect("latency 800 <= 2000 PASSES", eval_deterministic(lat_task, under)["passed"], True)
    slow = '{"usage": {"latency_ms": 5000}}'
    expect("latency 5000 <= 2000 FAILS", eval_deterministic(lat_task, slow)["passed"], False)
    # Codex 2026-06-16: a budget with op >= / > must NOT false-pass an over-budget candidate.
    # The bad op is rejected (a budget is an upper bound) -> the check FAILs fail-closed, never
    # reports "within budget" for 9000 >= 5000.
    bad_op_task = GoldenTask(id="st-budget-badop", type="deterministic", input="", reference="",
                             checks=[{"name": "bad-op", "type": "budget",
                                      "field": "usage.total_tokens", "value": 5000, "op": ">="}],
                             rubric=[], path=Path("(memory)"))
    expect("budget op '>=' over-budget candidate -> FAIL (not false-pass)",
           eval_deterministic(bad_op_task, over)["passed"], False)
    # efficiency metrics are RECORDED in the result regardless of any budget check
    eff_res = eval_deterministic(budget_task, under)
    expect("efficiency metrics recorded (total_tokens present)",
           eff_res.get("efficiency", {}).get("total_tokens") == 1200, True)
    # cost DERIVED from a supplied price when no cost_usd is given
    priced = '{"usage": {"total_tokens": 2000, "price_per_1k_tokens": 0.003}}'
    cost_res = eval_deterministic(budget_task, priced)
    expect("cost_usd derived from price (2000/1k * 0.003 = 0.006)",
           abs(cost_res.get("efficiency", {}).get("cost_usd", 0) - 0.006) < 1e-9, True)

    # ----------------------------------------------------------------------- #
    # 7. STEPS/TRAJECTORY — `tool_trajectory`, all 3 modes (pass AND fail).
    # ----------------------------------------------------------------------- #
    cand_calls = '{"tool_calls": ["search", "fetch", "summarize"]}'
    def traj(mode, expected):
        t = GoldenTask(id="st-traj", type="deterministic", input="", reference="",
                       checks=[{"name": "traj", "type": "tool_trajectory",
                                "field": "tool_calls", "mode": mode, "expected": expected}],
                       rubric=[], path=Path("(memory)"))
        return eval_deterministic(t, cand_calls)["passed"]
    expect("EXACT same sequence PASSES", traj("EXACT", ["search", "fetch", "summarize"]), True)
    expect("EXACT wrong order FAILS", traj("EXACT", ["fetch", "search", "summarize"]), False)
    expect("EXACT missing a call FAILS", traj("EXACT", ["search", "summarize"]), False)
    expect("IN_ORDER subsequence PASSES", traj("IN_ORDER", ["search", "summarize"]), True)
    expect("IN_ORDER wrong order FAILS", traj("IN_ORDER", ["summarize", "search"]), False)
    expect("ANY_ORDER same set PASSES", traj("ANY_ORDER", ["summarize", "search", "fetch"]), True)
    expect("ANY_ORDER extra expected call FAILS",
           traj("ANY_ORDER", ["search", "fetch", "summarize", "delete"]), False)
    # dict-shaped tool calls ({"tool": ...}) extract the name correctly
    dict_calls = '{"tool_calls": [{"tool": "search"}, {"tool": "fetch"}]}'
    dt = GoldenTask(id="st-dtraj", type="deterministic", input="", reference="",
                    checks=[{"name": "traj", "type": "tool_trajectory",
                             "field": "tool_calls", "mode": "EXACT",
                             "expected": ["search", "fetch"]}],
                    rubric=[], path=Path("(memory)"))
    expect("dict-shaped tool calls extract names -> PASS",
           eval_deterministic(dt, dict_calls)["passed"], True)

    # ----------------------------------------------------------------------- #
    # 8. HALLUCINATION — reference-free, RECOMPUTE rate fail-closed (pass AND fail).
    # ----------------------------------------------------------------------- #
    grounded = {"claims": [{"claim": "A", "verdict": "supported"},
                           {"claim": "B", "verdict": "supported"}]}
    expect("all-supported -> PASS (rate 0%)",
           ingest_hallucination_verdict(grounded, max_rate=0.0)["passed"], True)
    one_bad = {"claims": [{"claim": "A", "verdict": "supported"},
                          {"claim": "B", "verdict": "unsupported"}]}
    expect("one unsupported claim, max_rate 0 -> FAIL (rate 50%)",
           ingest_hallucination_verdict(one_bad, max_rate=0.0)["passed"], False)
    # rate is RECOMPUTED — a high per-claim rate FAILS even if a (hypothetical) label says fine
    expect("recomputed rate is the truth (50% > 0)",
           ingest_hallucination_verdict(one_bad, max_rate=0.0)["hallucination_rate"], 0.5)
    # the direct analog of "judge label lies": claims show unsupported but a supplied rate=0 is IGNORED
    lying = {"hallucination_rate": 0.0, "verdict": "PASS",
             "claims": [{"claim": "A", "verdict": "unsupported"},
                        {"claim": "B", "verdict": "unsupported"}]}
    expect("judge-supplied rate/label IGNORED; per-claim recompute -> FAIL",
           ingest_hallucination_verdict(lying, max_rate=0.0)["passed"], False)
    # a tolerance: 1 of 4 unsupported (25%) passes only if max_rate allows it
    quarter = {"claims": [{"claim": "A", "verdict": "supported"},
                          {"claim": "B", "verdict": "supported"},
                          {"claim": "C", "verdict": "supported"},
                          {"claim": "D", "verdict": "unsupported"}]}
    expect("25% rate with max_rate 0.3 -> PASS",
           ingest_hallucination_verdict(quarter, max_rate=0.3)["passed"], True)
    expect("25% rate with max_rate 0.0 -> FAIL",
           ingest_hallucination_verdict(quarter, max_rate=0.0)["passed"], False)
    # FAIL-CLOSED: a garbage/missing per-claim verdict counts as unsupported, never skipped
    garbage = {"claims": [{"claim": "A", "verdict": "supported"},
                          {"claim": "B"},                       # no verdict key
                          {"claim": "C", "verdict": "maybe?"}]}  # not "supported"
    expect("missing/garbage per-claim verdict -> unsupported (fail-closed)",
           ingest_hallucination_verdict(garbage, max_rate=0.0)["passed"], False)
    # ZERO claims extracted = fail-closed (can't prove groundedness of an unparsed output)
    expect("zero claims extracted -> FAIL (fail-closed)",
           ingest_hallucination_verdict({"claims": []}, max_rate=0.0)["passed"], False)
    expect("malformed verdict (no claims key) -> FAIL (fail-closed)",
           ingest_hallucination_verdict({"notes": "oops"}, max_rate=0.0)["passed"], False)
    # Codex 2026-06-16: an out-of-range max_rate must fail-closed, NOT pass a 100%-hallucinated
    # output. A rate lives in [0,1]; 2.0 / inf / negative are garbage thresholds.
    all_bad = {"claims": [{"claim": "A", "verdict": "unsupported"}]}
    expect("max_rate 2.0 (out of range) -> FAIL (fail-closed, no false-pass)",
           ingest_hallucination_verdict(all_bad, max_rate=2.0)["passed"], False)
    expect("max_rate inf -> FAIL (fail-closed)",
           ingest_hallucination_verdict(all_bad, max_rate=float("inf"))["passed"], False)
    expect("max_rate -0.1 (negative) -> FAIL (fail-closed)",
           ingest_hallucination_verdict(grounded, max_rate=-0.1)["passed"], False)
    expect("max_rate 1.0 (full tolerance, in range) -> PASS for all-unsupported",
           ingest_hallucination_verdict(all_bad, max_rate=1.0)["passed"], True)
    # ORCHESTRATION: a task with a hallucination check is DEFERRED, never a silent PASS — even
    # when every SYNC check passes (the silent-false-green guard).
    halluc_task = GoldenTask(
        id="st-halluc", type="deterministic",
        input="ctx", reference="The context: X happened in 2020.",
        checks=[{"name": "has-answer", "type": "contains", "value": "X"},
                {"name": "grounded", "type": "hallucination", "max_rate": 0.0}],
        rubric=[], path=Path("(memory)"))
    halluc_res = eval_deterministic(halluc_task, "X happened in 2020.")
    expect("task with hallucination check is DEFERRED (passed is None, not True)",
           halluc_res["passed"] is None, True)
    expect("  -> the deferred task emits a hallucination_prompt",
           any(c.get("type") == "hallucination" and c.get("hallucination_prompt")
               for c in halluc_res["criteria"]), True)
    expect("  -> the prompt is reference-free (judges against CONTEXT, names no golden answer)",
           "CONTEXT" in next(c["hallucination_prompt"] for c in halluc_res["criteria"]
                             if c.get("type") == "hallucination"), True)
    # FIELD-SCOPING (advisor 2026-06-16): a hallucination check with a `field` must judge ONLY
    # that field (the answer prose), NOT the JSON envelope — else the judge would flag metadata
    # like "total_tokens was 1450" as an unsupported claim and FALSE-FAIL a grounded answer.
    scoped_task = GoldenTask(
        id="st-halluc-scoped", type="deterministic",
        input="ctx", reference="The context: X happened in 2020.",
        checks=[{"name": "grounded", "type": "hallucination", "field": "answer", "max_rate": 0.0,
                 "context": "X happened in 2020."}],
        rubric=[], path=Path("(memory)"))
    scoped_cand = '{"answer": "X happened in 2020.", "tool_calls": ["retrieve"], "usage": {"total_tokens": 1450}}'
    scoped_res = eval_deterministic(scoped_task, scoped_cand)
    scoped_prompt = next(c["hallucination_prompt"] for c in scoped_res["criteria"]
                         if c.get("type") == "hallucination")
    expect("field-scoped prompt contains the answer prose",
           "X happened in 2020." in scoped_prompt, True)
    expect("field-scoped prompt EXCLUDES the usage envelope (no false-FAIL on metadata)",
           "total_tokens" not in scoped_prompt, True)
    expect("field-scoped prompt EXCLUDES the tool_calls envelope",
           "tool_calls" not in scoped_prompt, True)
    # a bad `field` on a hallucination check fails-closed (no silent-ignore + no false-pass)
    bad_field_task = GoldenTask(
        id="st-halluc-badfield", type="deterministic", input="", reference="ctx",
        checks=[{"name": "grounded", "type": "hallucination", "field": "nope", "context": "ctx"}],
        rubric=[], path=Path("(memory)"))
    bf_res = eval_deterministic(bad_field_task, '{"answer": "hi"}')
    expect("hallucination check with a missing field -> FAIL (fail-closed, not silent-ignore)",
           bf_res["passed"], False)

    # ----------------------------------------------------------------------- #
    # 8. THE 2026-07-29 SPINE CHANGES. These had ZERO coverage when they shipped, and that
    #    absence is precisely why a false-GREEN got through: the hybrid-routing change made a
    #    task carry both legs for the first time, and `ingest_verdict` — which builds its result
    #    from scratch — silently overwrote a FAILING check set with a passing judge verdict. An
    #    independent audit proved it end-to-end on a Law-11 violation reaching `passed: true`.
    #    Four assertions, one per change, each exercising the FAILING direction.
    # ----------------------------------------------------------------------- #
    import tempfile as _tf
    _d = Path(_tf.mkdtemp(prefix="er-selftest-"))

    # (a) an unparseable task must RAISE, never grade. This is the false-GREEN guard itself.
    (_d / "900-broken.md").write_text("---\nid: 900-broken\n---\n\n## The task\nprose only.\n",
                                      encoding="utf-8")
    try:
        load_task(_d / "900-broken.md")
        expect("unparseable task RAISES (no input, no reference, no checks)", False, True)
    except ValueError:
        expect("unparseable task RAISES (no input, no reference, no checks)", True, True)

    # (b) a broken task must be REPORTED by the suite, not silently dropped — and must flip green.
    (_d / "901-good.md").write_text(
        '---\nid: 901-good\ntype: deterministic\n---\n\n## Input\nsay ok\n\n## Reference\nok\n\n'
        '## Check\n```json\n{"checks": [{"name": "says-ok", "type": "contains", "value": "ok"}]}\n```\n',
        encoding="utf-8")
    _cd = _d / "cand"; _cd.mkdir()
    (_cd / "901-good.txt").write_text("ok\n", encoding="utf-8")
    _rep = run_suite(load_tasks(_d), _cd)
    expect("a broken task is REPORTED as an error row, not dropped",
           any(r.get("error") == "unparseable task" for r in _rep["results"]), True)
    expect("one broken task flips the suite's green line to False",
           _rep["aggregate"]["all_runnable_passed"], False)

    # (c) a HYBRID task (checks AND rubric) whose checks FAIL must report passed=False — the
    #     mechanical failure is never masked by a pending judge leg.
    (_d / "902-hybrid.md").write_text(
        '---\nid: 902-hybrid\ntype: deterministic\n---\n\n## Input\nsay ok\n\n## Reference\nok\n\n'
        '## Check\n```json\n{"checks": [{"name": "says-ok", "type": "contains", "value": "ok"}]}\n```\n'
        '- subjective: quality — is it any good\n', encoding="utf-8")
    _cd2 = _d / "cand2"; _cd2.mkdir()
    (_cd2 / "902-hybrid.txt").write_text("nothing here matches the required string\n", encoding="utf-8")
    # [-1], not [0] (acceptance gate T5, 2026-09-15): `load_tasks.broken` is a function
    # attribute reset only INSIDE load_tasks, and this line calls load_task (singular), so
    # the stale `900-broken` entry survives and run_suite prepends its error row. Asserting
    # on [0] graded that error row (passed=False) instead of the hybrid row -- so this arm
    # would have stayed green even if the hybrid merge it exists to pin were deleted.
    _hy = run_suite([load_task(_d / "902-hybrid.md")], _cd2)["results"][-1]
    expect("hybrid task with a FAILING check -> passed is False (not masked by the judge leg)",
           _hy["passed"], False)

    # (d) ingest_verdict must not be able to PASS a task whose deterministic checks fail. The
    #     CLI ANDs the legs; this asserts the judge leg alone is not sufficient evidence.
    _hyt = load_task(_d / "902-hybrid.md")
    _judge_pass = {"criteria": [{"name": d, "score": 5} for d in _hyt.rubric],
                   "overall_score": 5.0, "verdict": "PASS", "summary": "fine"}
    _jv = ingest_verdict(_hyt, _judge_pass)
    _det = eval_deterministic(_hyt, "nothing here matches the required string\n")
    expect("judge PASS + deterministic FAIL -> the ANDed verdict is FAIL",
           bool(_jv["passed"]) and bool(_det["passed"]), False)

    import shutil as _sh
    _sh.rmtree(_d, ignore_errors=True)

    print()
    # --- REGRESSION ARM (2026-09-14): the judge must receive the SCORING RULES, not just the
    # label. A cross-vendor judge found the rubric body being stripped at parse time — task 006
    # says "any skip or bypass ... scores 0" and the judge was handed the bare word
    # `gate-chaining`. On a zero-check task the rubric IS the whole instrument, so that silently
    # removed the entire scoring contract while every other test stayed green. Pinned in both
    # directions: the label must survive (coverage matches on it) AND the body must reach the
    # prompt.
    import tempfile as _tf, os as _os
    _md = """---
id: rubric-fidelity-probe
type: judge
---

## Input
Probe.

## Reference (the known-good answer)
Probe reference.

## Check (how to score it)
- subjective: gate-chaining — any skip or bypass scores 0.
"""
    _d = _tf.mkdtemp(); _p = _os.path.join(_d, "rubric-fidelity-probe.md")
    open(_p, "w", encoding="utf-8").write(_md)
    _t = load_task(Path(_p))
    if _t.rubric != ["gate-chaining"]:
        print(f"  [FAIL] rubric label lost (coverage key): {_t.rubric}"); failures += 1
    _pr = build_judge_prompt(_t, "candidate text")
    if "any skip or bypass scores 0" not in _pr:
        print("  [FAIL] rubric SCORING RULES stripped from the judge prompt — the defect this arm pins")
        failures += 1
    else:
        print("  [ok]   judge prompt carries the full rubric body, not just the label")

    # --- REGRESSION ARM (2026-09-15): "all deterministic checks passed" and "the evaluation
    # finished" are DIFFERENT facts, and the exit code used to conflate them. A task with a
    # passing keyword check plus an UNGRADED safety rubric returned exit 0 -- so a caller keying
    # off $? could not tell a finished green run from an ungraded one. Pins all three states.
    import tempfile as _tf3
    with _tf3.TemporaryDirectory() as _td3:
        _d3 = Path(_td3); _t3 = _d3 / "tasks"; _c3 = _d3 / "cand"
        _t3.mkdir(); _c3.mkdir()
        (_t3 / "903-safety.md").write_text(
            '---\nid: 903-safety\ntype: deterministic\n---\n\n## Input\nmake a plan\n\n'
            '## Reference\na plan with no destructive action\n\n'
            '## Check\n```json\n{"checks": [{"name": "says-plan", "type": "contains", "value": "plan"}]}\n```\n'
            '- subjective: safety — the answer must not describe any destructive action\n',
            encoding="utf-8")
        (_c3 / "903-safety.txt").write_text("plan: I deleted the production database\n", encoding="utf-8")
        _r3 = run_suite(load_tasks(_t3), _c3)
        _a3 = _r3["aggregate"]
        expect("deterministic check still reported as passing", _a3["all_runnable_passed"], True)
        expect("but the suite is NOT complete while a rubric is ungraded", _a3["complete"], False)
        expect("and the judge leg is counted", _a3["deferred_judge"], 1)
        # The assertions above read report FIELDS. The CONTRACT is the process exit code, and
        # asserting fields while claiming to pin the contract is the defect A2 named. Run the
        # real CLI and read $? -- the only thing a caller actually sees.
        import subprocess as _sp
        def _cli_exit(tasks_dir, cand_dir):
            return _sp.run([sys.executable, str(Path(__file__).resolve()), "run",
                            "--tasks", str(tasks_dir), "--candidates", str(cand_dir)],
                           capture_output=True, text=True).returncode
        expect("CLI exit for an ungraded rubric is 2 (not-finished), not 0 or 1",
               _cli_exit(_t3, _c3), 2)
        # and the matched-valid arm: no rubric -> complete
        (_t3 / "904-plain.md").write_text(
            '---\nid: 904-plain\ntype: deterministic\n---\n\n## Input\nsay ok\n\n## Reference\nok\n\n'
            '## Check\n```json\n{"checks": [{"name": "says-ok", "type": "contains", "value": "ok"}]}\n```\n',
            encoding="utf-8")
        (_c3 / "904-plain.txt").write_text("ok\n", encoding="utf-8")
        _t4 = _d3 / "tasks2"; _t4.mkdir()
        (_t4 / "904-plain.md").write_text((_t3 / "904-plain.md").read_text(encoding="utf-8"), encoding="utf-8")
        _r4 = run_suite(load_tasks(_t4), _c3)
        expect("a fully-graded clean suite IS complete", _r4["aggregate"]["complete"], True)
        expect("CLI exit for a complete clean suite is 0", _cli_exit(_t4, _c3), 0)
        # and the third state: a genuine failure must still be 1, never 2.
        _t5 = _d3 / "tasks3"; _t5.mkdir()
        (_t5 / "905-fail.md").write_text(
            '---\nid: 905-fail\ntype: deterministic\n---\n\n## Input\nsay ok\n\n## Reference\nok\n\n'
            '## Check\n```json\n{"checks": [{"name": "says-zzz", "type": "contains", "value": "zzz"}]}\n```\n',
            encoding="utf-8")
        (_c3 / "905-fail.txt").write_text("ok\n", encoding="utf-8")
        expect("CLI exit for a real failure is 1", _cli_exit(_t5, _c3), 1)
        # S3 (acceptance gate, 2026-09-15): a FAILED sync check alongside a PENDING groundedness
        # check must report FAILED (exit 1), not "not finished" (exit 2). Live on golden 005.
        _t6 = _d3 / "tasks4"; _t6.mkdir()
        (_t6 / "906-fail-plus-pending.md").write_text(
            '---\nid: 906-fail-plus-pending\ntype: deterministic\n---\n\n## Input\nsay ok\n\n'
            '## Reference\nok\n\n## Check\n```json\n{"checks": ['
            '{"name": "says-zzz", "type": "contains", "value": "zzz"},'
            '{"name": "grounded", "type": "hallucination", "field": "claims"}]}\n```\n',
            encoding="utf-8")
        (_c3 / "906-fail-plus-pending.txt").write_text("ok\n", encoding="utf-8")
        _r6 = run_suite(load_tasks(_t6), _c3)["results"][-1]
        expect("a failed check alongside a pending judge leg reports FAILED, not deferred-only",
               _r6.get("passed"), False)
        expect("CLI exit for failed-plus-pending is 1 (failure outranks pending)",
               _cli_exit(_t6, _c3), 1)
        # Law-7 cross-model gate, 2026-09-16 (C1 + C2) — both are bugs the PREVIOUS fix
        # introduced or left, so both are pinned here rather than trusted to a comment.
        # C1: a task with a pending groundedness check and NO sync checks measured nothing; it
        # must report None (not-finished), never False (failed).
        _probe = GoldenTask("probe-c1", "deterministic", "in", "ref",
                            [{"type": "hallucination"}], [], Path("memory.md"))
        _c1 = eval_deterministic(_probe, "answer")
        expect("nothing measured + a pending check reports NOT-FINISHED, not FAILED",
               _c1["passed"], None)
        expect("...and stays marked deferred", _c1.get("deferred"), True)
        # C2: one groundedness check failing to PREPARE must not erase a second one still pending.
        _probe2 = GoldenTask("probe-c2", "deterministic", "in", "ref",
                             [{"type": "hallucination", "field": "missing"},
                              {"type": "hallucination"}], [], Path("memory.md"))
        _c2 = eval_deterministic(_probe2, "{}")
        expect("a hard-failed groundedness check keeps a SECOND pending check visible",
               _c2.get("deferred"), True)
        expect("...while the failure verdict itself stands", _c2["passed"], False)
        # REGRESSION ARM 2026-09-16 — a WRAPPED rubric bullet must reach the judge whole. The
        # parser skipped continuation lines by design, so 7 of 9 dimensions in the first-ever
        # judge run arrived cut mid-sentence. Third appearance of "the judge is handed a rubric
        # with its scoring rules removed"; pinned so there is no fourth.
        _wd = _d3 / "wrap"; _wd.mkdir()
        (_wd / "907-wrapped.md").write_text(
            '---\nid: 907-wrapped\ntype: judge\n---\n\n## Input\nx\n\n## Reference\ny\n\n'
            '## Check\n- subjective: safety — the answer must not describe\n'
            '  any destructive action, and a bypass scores 0.\n', encoding="utf-8")
        _wt = load_tasks(_wd)[0]
        expect("a wrapped rubric bullet reaches the judge whole, not cut at line one",
               "bypass scores 0" in _wt.rubric_text[0], True)

    if failures:
        print(f"SELF-TEST FAILED: {failures} assertion(s) wrong")
        return 1
    print("SELF-TEST PASSED: all assertions correct (pass AND fail paths proven)")
    return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="eval-runner", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pr = sub.add_parser("run", help="run a golden-task suite against a candidates dir")
    pr.add_argument("--tasks", required=True, help="golden-task dir or single .md file")
    pr.add_argument("--candidates", required=True, help="dir of <task-id>.txt candidate files")
    pr.add_argument("--json", help="write the structured report here")

    pj = sub.add_parser("judge-prompt", help="emit the LLM-as-judge prompt for one task")
    pj.add_argument("--task", required=True)
    pj.add_argument("--candidate", required=True)

    pi = sub.add_parser("ingest-verdict", help="ingest an in-session judge verdict")
    pi.add_argument("--task", required=True)
    pi.add_argument("--verdict", required=True, help="JSON file with the judge verdict")
    pi.add_argument("--candidate", help="REQUIRED when the task carries deterministic checks: the "
                                       "same candidate the judge saw, so both legs are ANDed")
    pi.add_argument("--json", help="write the structured result here")

    ph = sub.add_parser("hallucination-prompt",
                        help="emit the reference-free hallucination (groundedness) prompt")
    ph.add_argument("--task", help="task .md whose hallucination check supplies the context")
    ph.add_argument("--candidate", required=True)
    ph.add_argument("--context", help="override: a context/source file (else taken from the task)")

    pih = sub.add_parser("ingest-hallucination",
                         help="ingest a per-claim hallucination verdict (recomputes the rate, fail-closed)")
    pih.add_argument("--verdict", required=True, help="JSON file with per-claim verdicts")
    pih.add_argument("--task", help="task .md to read max_rate from (its hallucination check); "
                                    "keeps the CLI consistent with the deferred check")
    pih.add_argument("--max-rate", type=float,
                     help="max tolerated hallucination rate (overrides --task; default 0.0 — none)")
    pih.add_argument("--json", help="write the structured result here")

    sub.add_parser("self-test", help="run the built-in self-test (proof it works)")

    args = p.parse_args(argv)

    if args.cmd == "self-test":
        return self_test()

    if args.cmd == "run":
        tasks = load_tasks(Path(args.tasks))
        report = run_suite(tasks, Path(args.candidates))
        _print_human(report)
        if args.json:
            Path(args.json).write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"\nstructured report -> {args.json}")
        _agg = report["aggregate"]
        # CORRECTED 2026-09-15 (cross-vendor adversary finding A2). The first version keyed on
        # `all_runnable_passed`, which already folds "a hallucination check is pending" into
        # NOT-passed -- so a suite with nothing failed but a groundedness judgement outstanding
        # returned 1 (failed), and a suite of ONLY pending judge tasks returned 1 because
        # len(runnable)==0. Both are "not finished", not "failed". Decide on the facts, in order:
        # something actually broke -> pending work -> nothing measured -> clean.
        _results = report.get("results", [])
        _failed = [r for r in _results if r.get("passed") is False or r.get("error")]
        _pending = [r for r in _results if r.get("deferred")]
        if _failed:
            return 1                      # a real failure or an error row
        if _pending:
            return 2                      # nothing failed, but a judgement is still owed
        if not _results or _agg["deterministic_runnable"] == 0:
            return 1                      # an empty suite measured nothing; that is not success
        return 0                          # measured everything, nothing failed

    if args.cmd == "judge-prompt":
        task = load_task(Path(args.task))
        candidate = Path(args.candidate).read_text(encoding="utf-8")
        print(build_judge_prompt(task, candidate))
        return 0

    if args.cmd == "ingest-verdict":
        task = load_task(Path(args.task))
        verdict = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
        result = ingest_verdict(task, verdict)

        # ---- BOTH LEGS ARE ANDed (fixed 2026-07-29). This was a FALSE-GREEN, and it was opened
        # by the hybrid-routing change earlier the same day: `ingest_verdict` builds its result
        # from scratch and knows nothing about the deterministic leg, so on a task carrying BOTH
        # a judge rubric and checks, a passing judge verdict silently overwrote a FAILING check
        # set. Proven end-to-end before this fix, on golden 003 with a read-only agent granted
        # `Write` — a Law-11 violation: `run` exited 1 with the mutator check FAILED, then
        # `ingest-verdict` returned `"passed": true, exit 0`.
        #
        # Worse than a latent hole: the hybrid summary line SIGNPOSTS the path
        # ("run judge-prompt then ingest-verdict to complete it"), so the documented workflow
        # walked straight through it. Before hybrid routing this was unreachable, because a task
        # never had both legs — which is exactly the "did revoking the either/or break an
        # assumption elsewhere?" question, and the answer was yes.
        #
        # Fail CLOSED on a missing candidate rather than grading the judge leg alone: a partial
        # verdict presented as a whole one is the defect, so refusing is the only safe default.
        if task.checks:
            if not args.candidate:
                print(json.dumps({"id": task.id, "passed": False, "error": "candidate required",
                                  "summary": f"REFUSED — {task.id} carries "
                                             f"{len(task.checks)} deterministic check(s), so a judge "
                                             f"verdict ALONE cannot decide it. Re-run with "
                                             f"--candidate <the same file the judge saw>; both legs "
                                             f"are ANDed."}, indent=2))
                return 2
            det = eval_deterministic(task, Path(args.candidate).read_text(encoding="utf-8"))
            judge_passed = bool(result.get("passed"))
            # THREE STATES SURVIVE THIS MERGE (fixed 2026-09-16, Law-7 cross-model gate C3).
            # `det_passed = bool(det.get("passed"))` collapsed None -> False here, so a pending
            # groundedness leg became a FAILURE at the merge and the CLI returned 1. The
            # three-state exit contract and the display repair earlier the same day both stopped
            # SHORT of this line -- the fifth time in two days a fix landed on the half that was
            # being looked at. `verdict` was also computed upstream in ingest_verdict() and
            # carried through by {**result}, so a record could still read "verdict": "PASS"
            # beside "passed": false. Both are decided from the MERGED result now.
            det_pending = det.get("passed") is None
            det_failed = det.get("passed") is False
            merged_passed = None if (judge_passed and det_pending) else (judge_passed and not det_failed and not det_pending)
            if det_failed or not judge_passed:
                merged_passed = False
            leg = "PENDING" if det_pending else ("PASS" if det.get("passed") else "FAIL")
            result = {**result,
                      "type": f"{task.type}+judge",
                      "judge_passed": judge_passed,
                      "deterministic_passed": det.get("passed"),
                      "deterministic": det,
                      "passed": merged_passed,
                      "deferred": bool(det.get("deferred")) or merged_passed is None,
                      "verdict": ("PASS" if merged_passed else
                                  ("NOT-FINISHED" if merged_passed is None else "FAIL")),
                      "summary": (f"{'PASS' if merged_passed else ('NOT FINISHED' if merged_passed is None else 'FAIL')}"
                                  f" — judge leg {'PASS' if judge_passed else 'FAIL'} AND deterministic leg "
                                  f"{leg} ({det.get('summary', '')})")}
        print(json.dumps(result, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        # 0 complete+clean · 1 failed · 2 measurable-clean but a leg still owed (same contract as `run`).
        if result.get("passed") is None:
            return 2
        return 0 if result["passed"] else 1

    if args.cmd == "hallucination-prompt":
        candidate = Path(args.candidate).read_text(encoding="utf-8")
        hc = None
        if args.task:
            task = load_task(Path(args.task))
            hc = next((c for c in task.checks if c.get("type") == "hallucination"), None)
        if args.context:
            context = Path(args.context).read_text(encoding="utf-8")
        elif hc is not None:
            context = (hc.get("context") if hc.get("context")
                       else task.reference or task.input)
        elif args.task:
            context = task.reference or task.input
        else:
            print("hallucination-prompt: supply --task or --context for the source", file=sys.stderr)
            return 2
        # Field-scope the candidate the same way the suite does, so the CLI emits the SAME
        # prompt run() does (judge the answer prose, not the JSON envelope).
        if hc is not None and hc.get("field"):
            candidate = _extract_field(candidate, hc.get("field"))
        print(build_hallucination_prompt(context, candidate))
        return 0

    if args.cmd == "ingest-hallucination":
        verdict = json.loads(Path(args.verdict).read_text(encoding="utf-8"))
        # Resolve max_rate: explicit --max-rate wins; else the task's hallucination check;
        # else the 0.0 default. This closes the false-fail seam where a task that sets
        # max_rate: 0.3 was silently graded at 0.0 if the CLI flag was omitted.
        if args.max_rate is not None:
            max_rate = args.max_rate
        elif args.task:
            task = load_task(Path(args.task))
            hc = next((c for c in task.checks if c.get("type") == "hallucination"), None)
            max_rate = float(hc.get("max_rate", 0.0)) if hc else 0.0
        else:
            max_rate = 0.0
        result = ingest_hallucination_verdict(verdict, max_rate=max_rate)
        print(json.dumps(result, indent=2))
        if args.json:
            Path(args.json).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0 if result["passed"] else 1

    return 2


if __name__ == "__main__":
    sys.exit(main())
