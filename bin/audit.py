#!/usr/bin/env python3
"""Deterministic integrity audit of the claude-os groundwork + resources.

Read-only. Finds things that can't be fudged: broken skills, stale index,
missing agents/files, uncommitted state, missing LICENSE, unattended
dangerous-skip automation, and GUIDE-vs-STATE stage drift. The
claude-os-auditor agent runs this, then adds judgment (coverage,
consistency, blindspots, professional bar). Run: python3 bin/audit.py
"""
import os, re, subprocess, sys, plistlib, json

HOME = os.path.expanduser("~")
OS_DIR = os.path.join(HOME, "claude-os")
SKILLS = os.path.join(HOME, ".claude", "skills")
AGENTS = os.path.join(HOME, ".claude", "agents")
INDEX = os.path.join(OS_DIR, "skills-index.tsv")

findings = []  # (severity, category, message)
def add(sev, cat, msg): findings.append((sev, cat, msg))

# 1. Broken skill SKILL.md symlinks
broken = 0
for entry in os.listdir(SKILLS) if os.path.isdir(SKILLS) else []:
    sm = os.path.join(SKILLS, entry, "SKILL.md")
    if os.path.islink(sm) and not os.path.exists(sm):
        broken += 1
add("HIGH" if broken else "OK", "skills", f"{broken} broken SKILL.md symlinks")

# 1b. Skill format invariant (owner rule 2026-06-10: malformed skill docs are never ignored —
#     they get fixed; this check makes that self-enforcing). Every top-level skill dir (non _-prefixed)
#     must have SKILL.md with frontmatter containing name: + description: (<=1024 chars).
import re as _re
fmt_bad = []
for entry in (os.listdir(SKILLS) if os.path.isdir(SKILLS) else []):
    if entry.startswith(("_", ".")): continue
    d = os.path.join(SKILLS, entry)
    if not os.path.isdir(d): continue
    smp = os.path.join(d, "SKILL.md")
    if not os.path.isfile(smp):
        if not os.listdir(d): fmt_bad.append(f"{entry} (empty husk)")
        elif any(os.path.isfile(os.path.join(dp, "SKILL.md")) for dp, dn, fn in os.walk(d) for _ in [0] if "SKILL.md" in fn): pass  # collection container
        else: fmt_bad.append(f"{entry} (no SKILL.md)")
        continue
    try: txt = open(smp, encoding="utf-8", errors="replace").read()
    except OSError: fmt_bad.append(f"{entry} (unreadable)"); continue
    m = _re.match(r"^---\n(.*?)\n---", txt, _re.S)
    if not m: fmt_bad.append(f"{entry} (no frontmatter)"); continue
    fm = m.group(1)
    if not _re.search(r"^name\s*:", fm, _re.M): fmt_bad.append(f"{entry} (no name)")
    dm = _re.search(r"^description\s*:\s*(.+)$", fm, _re.M)
    if not dm: fmt_bad.append(f"{entry} (no description)")
    elif len(dm.group(1)) > 1024: fmt_bad.append(f"{entry} (description {len(dm.group(1))} chars > 1024)")
add("MED" if fmt_bad else "OK", "skills",
    (f"{len(fmt_bad)} skill format defect(s): {fmt_bad[:8]}{'...' if len(fmt_bad) > 8 else ''} — fix per the never-ignore-malformed rule"
     if fmt_bad else "all top-level skills carry valid frontmatter (name + description <=1024)"))

# 2. UNIQUE skill names on disk vs index (staleness — dedup by name to match the index)
names = set()
for dp, dn, fn in os.walk(SKILLS, followlinks=True):
    if dp[len(SKILLS):].count(os.sep) > 3:
        dn[:] = []; continue
    if "SKILL.md" in fn: names.add(os.path.basename(dp))
disk = len(names)
if os.path.exists(INDEX):
    with open(INDEX) as f: idx = sum(1 for _ in f) - 1
    drift = abs(disk - idx)
    add("MED" if drift > 30 else "OK", "index",
        f"index {idx} rows vs {disk} unique skill names on disk (drift {drift}) — regenerate via bin/build-skills-index.py if large")
else:
    add("HIGH", "index", "skills-index.tsv MISSING — selector cannot filter; run bin/build-skills-index.py")

# 3. Expected agents present.
# NATIVE = claude-os's own deliverables: must exist in BOTH the repo source
#   (~/claude-os/agents/, so a fresh clone carries them) AND installed (~/.claude/agents/).
#   A native agent missing from the repo source is the one thing that silently breaks
#   portability — the audit must catch it (ADR-0017).
#   The 5 AI-Council agents were EXTERNAL (installed-only substrate) until 2026-06-10; the
#   owner ordered ALL agents grouped into the repo (like the skills library), so they are
#   repo-vendored + reworked native agents now. Their shared skills live in
#   library/council-*; check 12 keeps their special exact-toolset rule.
COUNCIL_AGENTS = {"idea-sharpener", "user-pain-validator", "build-realist",
                  "market-strategist", "devils-advocate"}
NATIVE_AGENTS = ["project-advisor", "claude-os-auditor", "acceptance-gate", "discovery-scout", "guidelines-monitor",
                 # core specialist brigade (TOOLKITS.md v2 — built 2026-06-09):
                 "research-specialist", "planning-specialist", "frontend-specialist", "backend-specialist",
                 "security-specialist", "evals-specialist", "writing-specialist", "data-specialist",
                 "ai-engineering-specialist", "opportunity-finder",
                 # self-evolution heartbeat (NORTH-STAR organ 14 — built 2026-06-24):
                 "utilization-coordinator",
                 # unknown-unknowns mapper (built 2026-07-05 from the owner-supplied "Field Guide to
                 # Fable" essay; the find-unknowns skill's scanning leg):
                 "blindspot-scout",
                 # cost-routing lane agents (vendored 2026-07-03 from DannyMac180/fable-advisor, MIT;
                 # the § Top-model-final mechanism — MODEL-ROUTING § Session-model-first records their
                 # model-pin exception):
                 "frontier-advisor", "implementer",
                 # loop-design judge (built 2026-07-06 from the official "Getting started with loops"
                 # article, claude.com/blog; the loop-designer skill's maker≠judge counterpart):
                 "loop-auditor"] + sorted(COUNCIL_AGENTS)
REPO_AGENTS = os.path.join(OS_DIR, "agents")
_agents_missing = 0
for a in NATIVE_AGENTS:
    if not os.path.exists(os.path.join(REPO_AGENTS, a + ".md")):
        add("HIGH", "agents", f"NATIVE agent missing from repo source agents/{a}.md — breaks portability (a fresh clone loses it)"); _agents_missing += 1
    if not os.path.exists(os.path.join(AGENTS, a + ".md")):
        add("HIGH", "agents", f"NATIVE agent not installed: ~/.claude/agents/{a}.md — install via cp from agents/"); _agents_missing += 1
if not _agents_missing:   # the OK must not print alongside a HIGH (gate nit 2026-06-10)
    add("OK", "agents", f"checked {len(NATIVE_AGENTS)} native agents, repo+installed (incl. the 5 council agents — repo-vendored + reworked 2026-06-10, owner-ordered grouping)")

# 3a-rev. Reverse-presence (organ-14, 2026-06-24): catch a native that exists ON DISK but is ABSENT
#     from NATIVE_AGENTS. The forward loop above proves the listed agents exist; this proves nothing
#     UNLISTED slips through — an unregistered agents/*.md is still installed by sync.sh and runnable,
#     yet skips the tool-scope (mutator/quarantine) + parity checks entirely. That enforcement gap is
#     exactly how session-learner sat unregistered among the skills until organ-14 caught it by hand;
#     "build the check, not another rule" (lessons.md), so the class can't recur silently.
_unregistered_agents = sorted(
    f[:-3] for f in os.listdir(REPO_AGENTS)
    if f.endswith(".md") and f != "README.md" and f[:-3] not in NATIVE_AGENTS
) if os.path.isdir(REPO_AGENTS) else []
if _unregistered_agents:
    add("HIGH", "agents", f"on-disk native agent(s) NOT in NATIVE_AGENTS: {_unregistered_agents} — installed by sync.sh + runnable but SKIPPING tool-scope + parity enforcement; add to bin/audit.py NATIVE_AGENTS (+ ALLOWED_TOOLS) or remove the file(s)")
else:
    add("OK", "agents", "no unregistered native agents (every agents/*.md is in NATIVE_AGENTS) — reverse-presence check")

# 4. Key groundwork files present
need = ["START-HERE.md", "GUIDE.md", "MANIFEST.md", "STATE.md",
        "docs/NORTH-STAR.md", "docs/EVAL-RUBRIC.md", "docs/V1-SCOPE-FREEZE.md",
        "docs/adr/0001-record-architecture-decisions.md",
        "tasks/lessons.md", "tasks/todo.md", "bin/build-skills-index.py",
        "knowledge/capability-registry.md", "bin/registry-check.py",
        "bin/hooks/usage-telemetry.py", "bin/telemetry-summary.py"]
for n in need:
    if not os.path.exists(os.path.join(OS_DIR, n)):
        add("HIGH", "files", f"missing groundwork file: {n}")

# 5. Uncommitted changes in claude-os (is the base durable?). Fail-closed: a nonzero `git status`
#    must NOT be read as "0 changes / OK" (Codex review 2026-06-08) — check the return code.
try:
    r = subprocess.run(["git", "-C", OS_DIR, "status", "--porcelain"],
                       capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        add("HIGH", "git", f"`git status` failed (exit {r.returncode}) — cannot determine durability: {r.stderr.strip()}")
    else:
        n = len([l for l in r.stdout.splitlines() if l.strip()])
        add("MED" if n else "OK", "git", f"{n} uncommitted change(s) in claude-os" + (" — commit to make durable" if n else ""))
except Exception as e:
    # git missing / timeout / spawn failure => durability UNDETERMINED => fail-closed HIGH (Codex 2026-06-08)
    add("HIGH", "git", f"could not run `git status` ({e}) — durability of the base is UNKNOWN (fail-closed)")

# 6. Lessons file is non-trivial (the compounding asset exists) AND not past its degradation
#    band: ERL (arXiv 2603.24639, adopted in LOOP-PROTOCOL 2026-06-10) finds lesson-store gains
#    peak ~40-60 entries then degrade. The system's own doctrine ("build the check, not another
#    rule") demands this be enforced here, not remembered (2026-06-10 audit blindspot).
les = os.path.join(OS_DIR, "tasks", "lessons.md")
if os.path.exists(les):
    nlines = sum(1 for l in open(les) if l.strip().startswith("- "))
    if nlines < 5:
        add("MED", "lessons", f"lessons.md has only {nlines} lesson lines — the compounding asset is thin")
    elif nlines > 60:
        add("MED", "lessons", f"lessons.md has {nlines} lesson lines — past the ~40-60 ERL degradation band; run the distill task (tasks/todo.md): merge/generalize/kill-dead, don't just accrete")
    else:
        add("OK", "lessons", f"lessons.md has {nlines} lesson lines (within the ERL band)")

def _read(rel):
    p = os.path.join(OS_DIR, rel)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""

# 3b. Ledger-sync: agent-COUNT claims in the living entry docs must match NATIVE_AGENTS.
#     Today's worst doc drift (8-vs-15-vs-20 across the prescribed reading order) was mechanically
#     detectable; lessons.md's own rule is "build the check, not another rule" (2026-06-10 audit).
#     Scope: the living docs only — STATE's header (before "## Done": Done entries are history),
#     START-HERE, MANIFEST, GUIDE.
_count_re = re.compile(r"(\d+)\s+(?:claude-os-)?native\s+(?:sub)?agents?|[Nn]ative agents?\*?\*?\s*\((\d+)\b|(\d+)\s+claude-os-native\b")
_ledger_bad = []
for rel in ("STATE.md", "START-HERE.md", "MANIFEST.md", "GUIDE.md"):
    txt = _read(rel)
    if rel == "STATE.md":
        txt = txt.split("\n## Done", 1)[0]
    for m in _count_re.finditer(txt):
        n = int(m.group(1) or m.group(2) or m.group(3))
        if n != len(NATIVE_AGENTS):
            _ledger_bad.append(f"{rel}: claims {n}")
if _ledger_bad:
    add("MED", "agents", f"agent-count claims out of sync with NATIVE_AGENTS ({len(NATIVE_AGENTS)}): {', '.join(_ledger_bad)} — fix the doc(s), the registry is authoritative")
else:
    add("OK", "agents", f"living-doc agent counts match NATIVE_AGENTS ({len(NATIVE_AGENTS)}) — ledger-sync check")

# 7. LICENSE present (the repo is public — added 2026-06-08 audit). Public + no license
#    = "all rights reserved" by default, which blocks the intended open use.
add("OK" if os.path.exists(os.path.join(OS_DIR, "LICENSE")) or
    os.path.exists(os.path.join(OS_DIR, "LICENSE.md")) else "HIGH",
    "license", "LICENSE present" if os.path.exists(os.path.join(OS_DIR, "LICENSE"))
    else "no LICENSE file — required for a public repo (add one)")

def _sh_exec(text):
    """Executable shell text only — drop full-line + inline `#` comments (Codex review 2026-06-08:
    an audit must judge what RUNS, not strings sitting in comments). This script has no `#` inside
    quotes, so a simple strip is safe + far less brittle than a real shell parse."""
    out = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("#") or s.startswith("#!"):
            continue
        out.append(re.sub(r"\s#.*$", "", ln))
    return "\n".join(out)

# 8. Unattended dangerous-skip scripts. Two distinct risks, reported separately:
#    (a) a claudeos launch agent actually LOADED (real) — always check;
#    (b) a skip-permissions flag in an EXECUTABLE line (comments that merely mention the flag don't
#    count). Path B is now attended-only (no skip-permissions at all); #15 owns its full assessment.
BIN = os.path.join(OS_DIR, "bin")
# Inspect actual launchd plist CONTENTS (Codex 2026-06-08): a job under any label/filename that
# invokes discovery-weekly.sh or this repo is what matters, not whether the filename says "claudeos".
la = os.path.expanduser("~/Library/LaunchAgents")
loaded = []
for f in (os.listdir(la) if os.path.isdir(la) else []):
    fp = os.path.join(la, f)
    ref = "claudeos" in f.lower()  # filename hint (belt-and-suspenders)
    if f.endswith(".plist"):
        try:
            with open(fp, "rb") as fh:
                pl = plistlib.load(fh)
            blob = " ".join(map(str, pl.get("ProgramArguments", []) or [])) + " " + str(pl.get("Label", ""))
            if "discovery-weekly.sh" in blob or "claude-os" in blob or "claudeos" in blob.lower():
                ref = True
        except Exception:
            pass  # unparseable plist: fall back to the filename hint
    if ref:
        loaded.append(f)
skip_scripts = []
for fn in (os.listdir(BIN) if os.path.isdir(BIN) else []):
    if fn.endswith(".sh"):
        if "--dangerously-skip-permissions" in _sh_exec(_read(os.path.join("bin", fn))):
            skip_scripts.append(fn)
_decl = set()
_ast = os.path.join(OS_DIR, "bin", "automation.status")
if os.path.isfile(_ast):
    _decl = {l.strip() for l in open(_ast) if l.strip().endswith(".plist")}
_undeclared = [h for h in loaded if h not in _decl]
if _undeclared:
    add("HIGH", "automation", f"UNDECLARED claudeos launch agent(s) installed: {_undeclared} — not attested in bin/automation.status; verify intent (fail-closed)")
elif loaded:
    add("MED", "automation", f"{len(loaded)} claudeos launch agent(s) installed, all owner-attested in bin/automation.status (declared — the audit cannot verify the sanction itself, so never a green OK)")
if skip_scripts:
    add("MED", "automation", f"{len(skip_scripts)} script(s) run --dangerously-skip-permissions ({', '.join(skip_scripts)}) — high blast radius on a public repo; keep parked/uninstalled or remove the flag")
if not loaded and not skip_scripts:
    add("OK", "automation", "no loaded claudeos launch agents; no script runs --dangerously-skip-permissions")

# 8d. Timer-freshness watchdog (added 2026-07-07 — loop-auditor's first live sweep: three sanctioned
#     timers sat silently UNLOADED for ~3 weeks; nothing noticed). Every deterministic tick writes a
#     dated line to ~/Library/Logs/claude-<job>.log on EVERY fire (monthly-audit logs its "not the
#     first Monday — skipped" line too; all four plists fire weekly, Weekday=1), so log silence past
#     the cadence window = dead trigger. The check lives HERE because audit.py runs ATTENDED at every
#     session stop (loop-verify hook) — a watchdog inside the dead loop itself would be circular.
#     MED, never HIGH: a dead timer breaks currency, not correctness; repair = owner re-bootstraps
#     the plist (already-sanctioned automation — see bin/automation.status).
import datetime as _dt
_TIMER_LOGS = ["claude-discovery-sweep.log", "claude-model-refresh.log",
               "claude-monthly-audit.log", "claude-video-harvest.log"]  # all weekly-fire (Weekday=1)
_TIMER_WINDOW_DAYS = 8
_stale_timers, _fresh_timers = [], []
_tdate_re = re.compile(r"^(\d{4}-\d{2}-\d{2})")
for _lg in _TIMER_LOGS:
    _lp = os.path.expanduser("~/Library/Logs/" + _lg)
    if not os.path.isfile(_lp):
        continue  # substrate absent (fresh machine) — the MANIFEST prerequisites own that story
    _last = None
    try:
        for _ln in reversed(open(_lp, errors="replace").read().splitlines()):
            _m = _tdate_re.match(_ln.strip())
            if _m:
                _last = _m.group(1)
                break
        _age = (_dt.date.today() - _dt.date.fromisoformat(_last)).days if _last else None
    except (ValueError, OSError):
        continue  # unparseable log: freshness unknowable here, not a finding
    _job = _lg.replace("claude-", "").replace(".log", "")
    if _age is None:
        continue
    if _age > _TIMER_WINDOW_DAYS:
        _stale_timers.append(f"{_job} (last fire {_last}, {_age}d ago)")
    else:
        _fresh_timers.append(_job)
if _stale_timers:
    add("MED", "automation", f"timer-freshness: {len(_stale_timers)} sanctioned timer(s) SILENT past the {_TIMER_WINDOW_DAYS}d cadence window: {'; '.join(_stale_timers)} — the tick logs on every fire, so silence = dead trigger (unloaded/failed); owner re-bootstraps the plist(s) (repair of attested automation, not a new install)")
elif _fresh_timers:
    add("OK", "automation", f"timer-freshness: {len(_fresh_timers)} timer log(s) show a fire within {_TIMER_WINDOW_DAYS}d ({', '.join(_fresh_timers)})")

# 8c. Native-only doctrine (owner 2026-06-16): NO bin/ script may invoke headless Claude — the Agent
#     SDK / `claude -p` / external API is declined; all agents/subagents run natively in-session.
#     The regex requires a prompt indicator after -p ("|$) so it matches a real INVOCATION, not the
#     comments/prose that now mention "claude -p" (e.g. "Was: headless claude -p").
import glob as _glob, re as _re
_sdk_re = _re.compile(r'claude["\']?\s+(?:-p|--print)\s+["$]')
_sdk_hits = []
for _f in sorted(_glob.glob(os.path.join(OS_DIR, "bin", "*.sh"))):
    for _i, _ln in enumerate(open(_f, errors="replace"), 1):
        if _ln.lstrip().startswith("#"):
            continue
        if _sdk_re.search(_ln):
            _sdk_hits.append(f"{os.path.basename(_f)}:{_i}")
if _sdk_hits:
    add("HIGH", "native-only", f"headless `claude -p`/SDK invocation in bin/ ({', '.join(_sdk_hits)}) — violates the native-only doctrine (owner 2026-06-16: all agents/subagents run natively in-session; no Agent SDK / external API / programmatic credit). Reorient to a deterministic due-marker → tasks/scheduled-due.md (pattern: bin/discovery-sweep.sh).")
else:
    add("OK", "native-only", "no headless `claude -p`/SDK invocation in bin/ scripts — native-only doctrine holds (scheduled work stamps DUE; execution is in-session)")

# 9. Doc-vs-reality drift: GUIDE / tutorial stage must match STATE's declared stage (Law 12).
state_txt, guide_txt = _read("STATE.md"), _read("GUIDE.md")
ms = re.search(r"\*\*Stage:\*\*\s*(\d)", state_txt)
state_stage = ms.group(1) if ms else None
# the tutorial's "today the system is at **Stage N**" line
mg = re.search(r"today the system is at \*\*Stage\s*(\d)", guide_txt)
guide_stage = mg.group(1) if mg else None
if state_stage and guide_stage and state_stage != guide_stage:
    add("MED", "drift", f"GUIDE tutorial says Stage {guide_stage} but STATE says Stage {state_stage} — Law 12 living-doc drift; refresh GUIDE")
else:
    add("OK", "drift", f"GUIDE/STATE stage aligned" + (f" (Stage {state_stage})" if state_stage else ""))

# 9b. Pillar-2 legibility presence (Law 12 at runtime): the plain-English box must not be
#     silently dropped from STATE.md, and the canonical standard must exist. Presence-only —
#     deterministic, never judges prose quality (that's not mechanizable; see STAGE-NARRATION.md).
legib_ok = "In plain English" in state_txt and os.path.exists(os.path.join(OS_DIR, "docs", "STAGE-NARRATION.md"))
add("OK" if legib_ok else "MED", "legibility",
    "STATE plain-English box + docs/STAGE-NARRATION.md present (Pillar-2 legibility layer)" if legib_ok
    else "Pillar-2 legibility layer degraded — STATE.md 'In plain English' box and/or docs/STAGE-NARRATION.md missing; restore (the box must stay current alongside the ledger)")

# 9c. Global-doctrine substrate presence (per MANIFEST § Global doctrine — canonical homes). The
#     global ~/.claude/CLAUDE.md is machine-local/unversioned; on a fresh machine it must be re-applied
#     from the repo's canonical homes. Substrate gap = MED (never HIGH — it's owner-installed), and the
#     audit surfaces it rather than silently tolerating it (the MANIFEST model). Markers, not full text.
_gcm = _read(os.path.join(HOME, ".claude", "CLAUDE.md"))  # _read returns "" if absent
_doc_markers = ["Multi-Source Mandate", "Stage awareness", "Legibility"]
_missing_doc = [m for m in _doc_markers if m not in _gcm]
if not _gcm:
    add("MED", "doctrine", "global ~/.claude/CLAUDE.md absent — fresh-machine bootstrap gap; re-apply the doctrine blocks per MANIFEST § Global doctrine — canonical homes")
elif _missing_doc:
    add("MED", "doctrine", f"global ~/.claude/CLAUDE.md present but missing doctrine marker(s) {_missing_doc} — re-apply from MANIFEST § Global doctrine — canonical homes")
else:
    add("OK", "doctrine", "global ~/.claude/CLAUDE.md carries the doctrine markers (sourcing · stage-awareness · legibility)")

# 9f. Project-entry routing pointer (E1 #2, owner-approved 2026-06-24). The global ~/.claude/CLAUDE.md
#     must tell ANY session the two project doors exist — `/claude-os <idea>` (front door: triage + settle
#     the goal) and `/enhance` (existing-project re-run -> ranked forward-proposals) — else a cloner has to
#     already know the system to use it (the W1.b #1 / E1 gap). Machine-local & owner-placed like the
#     doctrine substrate (self-modification gate), so MED not HIGH; surfaced, never silently tolerated.
_entry_ok = bool(_gcm) and ("two doors" in _gcm) and ("/enhance" in _gcm)
add("OK" if _entry_ok else "MED", "doctrine",
    "global ~/.claude/CLAUDE.md carries the project-entry routing pointer (/claude-os + /enhance doors)" if _entry_ok
    else "global ~/.claude/CLAUDE.md missing the project-entry routing pointer (/claude-os <idea> front door + /enhance existing-project re-run) — owner-place per MANIFEST § Global doctrine — canonical homes; without it existing projects won't self-route to /enhance")

# 9e. Anti-AI check-and-balance presence (dedicated organ, NORTH-STAR §4e, owner 2026-06-16). The
#     dedicated guard against AI's own failure modes (anti-slop + cross-model gate + reward-hack-resistant
#     verify + the AI-failure watch) must not silently erode. Presence-only (deterministic); never judges
#     whether the checks are "good", only that the organ is named + the watch ledger exists.
_ns_txt = _read(os.path.join(OS_DIR, "docs", "NORTH-STAR.md"))
_antiai_ok = "anti-AI check-and-balance" in _ns_txt and os.path.exists(os.path.join(OS_DIR, "tasks", "ai-failure-watch.md"))
add("OK" if _antiai_ok else "MED", "anti-ai",
    "dedicated anti-AI check-and-balance organ present (NORTH-STAR §4e + tasks/ai-failure-watch.md)" if _antiai_ok
    else "dedicated anti-AI check-and-balance degraded — NORTH-STAR §4e marker and/or tasks/ai-failure-watch.md missing; restore (owner-mandated dedicated organ, 2026-06-16)")

# 9d. Model-pin drift (added 2026-06-16). The session model is the owner's dial (Domain A,
#     MODEL-ROUTING § two regimes), so this is MED not HIGH. But a pinned context-variant
#     ([1m]) is brittle: its availability blips propagate into every inheriting subagent
#     (the 2026-06-16 opus[1m] failure that broke subagent launches). No pin = OK (follows
#     the picker, the recommended adaptive state); a stable seat = OK.
_gset = _read(os.path.join(HOME, ".claude", "settings.json"))
try:
    _pinned_model = json.loads(_gset).get("model") if _gset else None
except Exception:
    _pinned_model = None
if _pinned_model is None:
    add("OK", "model-pin", "global settings.json pins no session model — follows the picker/default (the recommended adaptive state)")
elif "[" in str(_pinned_model):
    add("MED", "model-pin", f"global settings.json pins a context-variant model '{_pinned_model}' — brittle: its availability blips propagate to every inheriting subagent (the 2026-06-16 opus[1m] failure). Prefer the stable base seat, or omit the key to follow the picker.")
else:
    add("OK", "model-pin", f"global settings.json pins '{_pinned_model}' (a stable seat, not a context-variant)")

# 10. Law 11 trifecta-guard hook present + wired (built 2026-06-08, live-verified).
#     "Present" != "functional on this machine" -> MED if missing, with a re-verify nudge.
#     Wiring detection PARSES the settings JSON and checks actual hook commands — a bare
#     substring test would false-OK on a $comment that merely names the hook (acceptance-gate
#     finding 2026-06-10).
def _hook_wired(settings_text, py_name, event):
    """True iff settings JSON has a hooks[event] entry whose command invokes py_name."""
    try:
        cfg = json.loads(settings_text) if settings_text else {}
    except Exception:
        return False                      # unparseable settings can't prove wiring (fail-closed)
    for entry in (cfg.get("hooks", {}).get(event) or []):
        for h in (entry.get("hooks") or []) if isinstance(entry, dict) else []:
            if not (isinstance(h, dict) and h.get("type") == "command"):
                continue
            # the script must be actually INVOKED, not merely mentioned in a comment
            # (Codex gate HIGH: `command: "true # trifecta-guard.py"` falsely reported wired)
            cmd_exec = re.sub(r"\s#.*$", "", str(h.get("command", "")))
            if py_name in cmd_exec:
                return True
    return False

hook_py = os.path.join(OS_DIR, "bin", "hooks", "trifecta-guard.py")
settings = _read(os.path.join(".claude", "settings.json"))
hook_present = os.path.exists(hook_py)
hook_wired = _hook_wired(settings, "trifecta-guard.py", "PreToolUse")
if hook_present and hook_wired:
    add("OK", "law11", "trifecta-guard hook present + wired (run --self-test to confirm it still blocks)")
else:
    miss = [] if hook_present else ["bin/hooks/trifecta-guard.py missing"]
    if not hook_wired:
        miss.append(".claude/settings.json not wiring the PreToolUse hook")
    add("MED", "law11", "Law 11 enforcement gap — " + "; ".join(miss) + " (the credential-read denial is unenforced)")

# 10a2. Law 11 leg-c: webfetch-egress-guard (built + self-tested 2026-06-10, design (c) council-
#       scoped allowlist). The .py is the deliverable; the settings.json WIRING is classifier-gated
#       self-modification => an OWNER ACTION (tasks/flags.md). Report each honestly.
eg_py = os.path.exists(os.path.join(OS_DIR, "bin", "hooks", "webfetch-egress-guard.py"))
eg_wired = _hook_wired(settings, "webfetch-egress-guard.py", "PreToolUse")
if eg_py and eg_wired:
    add("OK", "law11", "webfetch-egress-guard present + wired (council WebFetch allowlist + write confinement; run --self-test to confirm)")
elif eg_py:
    add("MED", "law11", "webfetch-egress-guard BUILT + self-tested but NOT WIRED in .claude/settings.json — wiring is a pending OWNER ACTION (classifier-gated self-modification; exact block in tasks/flags.md). Until wired, council leg-c is constrained by prompt + manual/advisory-use only")
else:
    add("MED", "law11", "webfetch-egress-guard missing (bin/hooks/webfetch-egress-guard.py) — Law 11 leg-c close not built")

# 10b. Verify-loop Stop hook present + wired (docs/LOOP-PROTOCOL.md, 2026-06-10).
#      Closes the verify-fix-retry loop so "done" can't be declared while checks fail.
lv_py = os.path.exists(os.path.join(OS_DIR, "bin", "hooks", "loop-verify.py"))
lv_wired = _hook_wired(settings, "loop-verify.py", "Stop")
if lv_py and lv_wired:
    add("OK", "loop", "verify-loop Stop hook present + wired (run --self-test to confirm cap/no-op logic)")
else:
    m2 = [] if lv_py else ["bin/hooks/loop-verify.py missing"]
    if not lv_wired:
        m2.append(".claude/settings.json not wiring the Stop hook")
    add("MED", "loop", "verify-loop gap — " + "; ".join(m2) + " (verify-relentlessly is prose, not enforced, here)")

# 11. Agent tool-scoping invariant (encoded as data, not eyeballed).
#     Each NATIVE agent must carry an explicit `tools:` allowlist (no `tools:` => Claude Code
#     grants ALL tools incl. Edit/Write/Bash) AND stay within its scoped set. Two categories:
#     (a) OVERSIGHT / READERS — read-only-by-toolset: NO Edit/Write/NotebookEdit (Law 11 quarantine /
#         maker≠judge). Bash only where it's needed for read-only ops (advisor/auditor: ls/grep/run
#         audit.py; security/evals: scanners/harnesses). The gate carries no Bash (a judge that can run can mutate).
#     (b) BUILDER specialists — legitimately carry mutators (producing artifacts IS their job); still
#         pinned to an exact scoped toolset and parity-enforced. Exempt from the mutator-prohibition only.
ALLOWED_TOOLS = {
    # (a) oversight / readers — mutator-free:
    "project-advisor":     {"Read", "Grep", "Glob", "Bash"},
    "claude-os-auditor":   {"Read", "Grep", "Glob", "Bash", "Skill"},
    "utilization-coordinator": {"Read", "Grep", "Glob", "Bash", "Skill"},  # read-only detector (organ 14); Bash = run audit.py/registry-check.py, read-only
    "acceptance-gate":     {"Read", "Grep", "Glob", "Skill"},   # no Bash (a judge that can run can mutate); Skill is non-mutating
    "loop-auditor":        {"Read", "Grep", "Glob", "Skill"},   # loop-design judge (2026-07-06): same no-Bash rationale as acceptance-gate
    "discovery-scout":     {"Read", "Grep", "Glob", "WebSearch", "WebFetch"},
    "guidelines-monitor":  {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill"},
    "research-specialist": {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill"},  # quarantined topical reader
    "blindspot-scout":     {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill"},  # quarantined unknowns mapper (2026-07-05)
    "opportunity-finder":  {"Read", "Grep", "Glob", "WebSearch", "WebFetch", "Skill"},  # quarantined opportunity reader
    "security-specialist": {"Read", "Grep", "Glob", "Bash", "Skill"},                   # auditor: scans + reports, no Write
    "evals-specialist":    {"Read", "Grep", "Glob", "Bash", "Skill"},                   # builds/runs evals + reports, no Write
    "frontier-advisor":    {"Read", "Grep", "Glob"},                                    # commitment-boundary advisor: advises only, never implements (no Bash — same rationale as acceptance-gate)
    # (b) builder specialists — mutators are their job:
    "planning-specialist": {"Read", "Grep", "Glob", "Write", "Edit", "Skill"},
    "frontend-specialist": {"Read", "Grep", "Glob", "Write", "Edit", "Bash", "Skill"},
    "backend-specialist":  {"Read", "Grep", "Glob", "Write", "Edit", "Bash", "Skill"},
    "writing-specialist":  {"Read", "Grep", "Glob", "Write", "Edit", "Skill"},
    "data-specialist":     {"Read", "Grep", "Glob", "Bash", "Write", "Skill"},
    "ai-engineering-specialist": {"Read", "Grep", "Glob", "Write", "Edit", "Bash", "Skill"},
    "implementer":         {"Read", "Grep", "Glob", "Write", "Edit", "Bash"},           # spec-executor lane: mutators are its job; output frontier-evaluated (top-model-final)
    # (c) council advisors (repo-vendored 2026-06-10) — Write/Edit is scoped by PROMPT to their own
    #     shared_reasoning.md section (the deliberation protocol); web is mandated by
    #     council-evidence-discipline. Check 12 additionally enforces EXACT set equality on these.
    "idea-sharpener":      {"Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch", "Skill"},
    "user-pain-validator": {"Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch", "Skill"},
    "build-realist":       {"Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch", "Skill"},
    "market-strategist":   {"Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch", "Skill"},
    "devils-advocate":     {"Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch", "Skill"},
}
MUTATORS = {"Edit", "Write", "NotebookEdit"}
# Builder specialists are EXEMPT from the mutator-prohibition (producing files IS their job); council
# advisors carry Write/Edit for the shared_reasoning.md protocol (prompt-scoped; residual disclosed in
# the law11 finding below). Every oversight/reader agent above is NOT exempt — a judge/reader that can
# Edit/Write breaks maker≠judge / quarantine.
BUILDER_AGENTS = {"planning-specialist", "frontend-specialist", "backend-specialist",
                  "writing-specialist", "data-specialist", "ai-engineering-specialist",
                  "implementer"}  # cost-routing spec-executor lane (2026-07-03): mutators are its job; frontier-evaluated per top-model-final
MUTATOR_EXEMPT = BUILDER_AGENTS | COUNCIL_AGENTS

# Registry-unification guard (Codex review 2026-06-08): the scope/parity checks iterate
# ALLOWED_TOOLS, while presence (check 3) iterates NATIVE_AGENTS. If a native agent were added to
# one but not the other it would silently skip tool-scope/parity. Assert the two registries agree.
_scope_missing = set(NATIVE_AGENTS) - set(ALLOWED_TOOLS)
if _scope_missing:
    add("HIGH", "agent-tools", f"native agent(s) {sorted(_scope_missing)} listed in NATIVE_AGENTS but absent from ALLOWED_TOOLS — they would skip tool-scope + parity enforcement; add an allowlist entry")
_scope_stale = set(ALLOWED_TOOLS) - set(NATIVE_AGENTS)
if _scope_stale:
    add("MED", "agent-tools", f"ALLOWED_TOOLS has entr(y/ies) {sorted(_scope_stale)} not in NATIVE_AGENTS — stale; remove or align")

def _agent_tools(path):
    """Parse an agent .md's LEADING YAML frontmatter only.
    Returns (state, tools):
      state True  -> a `tools:` line is present (tools = its allowlist set)
      state False -> frontmatter present but NO `tools:` line (Claude Code grants ALL tools)
      state None  -> no valid leading frontmatter (malformed; cannot verify scope)
    Never scans the body — a `tools:` shown in a usage example must not be read as a runtime grant
    (Codex review 2026-06-08). Handles inline (`tools: Read, Grep`), inline-flow (`tools: [Read]`),
    YAML block-list (`tools:\\n  - Read`), quoted values, and trailing `# comments`."""
    txt = open(path, encoding="utf-8").read()
    m = re.match(r"﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", txt, re.S)  # MUST be at file start
    if not m:
        return (None, set())
    lines = m.group(1).splitlines()
    for i, ln in enumerate(lines):
        tm = re.match(r"^tools:[ \t]*(.*)$", ln)
        if not tm:
            continue
        val = re.sub(r"\s+#.*$", "", tm.group(1)).strip().strip("'\"").strip()  # drop comment + quotes
        if val and val not in ("[]", "~", "null"):
            val = val.strip("[]")  # inline flow list -> same comma split
            return (True, {t.strip().strip("'\"") for t in val.split(",") if t.strip().strip("'\"")})
        tools = set()  # YAML block list on the following indented "- Tool" lines
        for ln2 in lines[i + 1:]:
            bm = re.match(r"^[ \t]+-[ \t]*(.+?)[ \t]*(?:#.*)?$", ln2)
            if bm:
                tools.add(bm.group(1).strip().strip("'\""))
            elif ln2.strip() == "" or ln2.lstrip().startswith("#"):
                continue
            else:
                break
        return (True, tools) if tools else (False, set())
    return (False, set())

scope_clean = True
for a, allowed in ALLOWED_TOOLS.items():
    p = os.path.join(REPO_AGENTS, a + ".md")
    if not os.path.exists(p):
        continue  # absence already HIGH in check 3
    state, tools = _agent_tools(p)
    if state is None:
        add("HIGH", "agent-tools", f"native agent {a}.md has no valid leading YAML frontmatter — cannot verify its tool scope; fix the frontmatter"); scope_clean = False
        continue
    if state is False:
        add("HIGH", "agent-tools", f"native agent {a}.md has NO tools: allowlist — inherits ALL tools (incl. Edit/Write/Bash); add an allowlist"); scope_clean = False
        continue
    muts = tools & MUTATORS
    if muts and a not in MUTATOR_EXEMPT:
        add("HIGH", "agent-tools", f"read-only agent {a} carries mutator tool(s) {sorted(muts)} — judges/readers must not Edit/Write/NotebookEdit"); scope_clean = False
    extra = tools - allowed   # Codex gate HIGH: do NOT subtract MUTATORS — that masked an
    #                           unallowed mutator (e.g. NotebookEdit on a builder). The mutator
    #                           prohibition for non-exempt agents is the separate `muts` check above.
    if extra:
        add("HIGH", "agent-tools", f"agent {a} holds tool(s) beyond its scoped allowlist: {sorted(extra)} (allowed: {sorted(allowed)}) — e.g. Bash on a read-only agent defeats the quarantine"); scope_clean = False
if scope_clean:
    add("OK", "agent-tools", f"all {len(ALLOWED_TOOLS)} native agents within scoped allowlists (judges/readers: no Edit/Write; Bash residual accepted on advisor/auditor/coordinator/security/evals for read-only ops; builders + council scoped to their mutators)")

# 11d. Skill-declaration dead-letter guard (2026-06-11, claude-os-auditor finding). An agent that
#      DECLARES skills (a `skills:` frontmatter block) but lacks the `Skill` tool can't invoke them —
#      a dead letter (the exact bug fixed for the 7 specialists 2026-06-10, then recurred in the 5
#      council agents). Mechanize it so the class can't return: skills-declared ⇒ Skill tool required.
_skill_deadletters = []
for a in NATIVE_AGENTS:
    p = os.path.join(REPO_AGENTS, a + ".md")
    if not os.path.exists(p):
        continue
    txt = open(p, encoding="utf-8").read()
    m = re.match(r"﻿?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n", txt, re.S)
    if not m:
        continue
    fm = m.group(1)
    sm = re.search(r"^skills:[ \t]*(.*)$", fm, re.M)
    if not sm:
        continue
    # Codex gate MED: treat null/~/[]/comment-only as NO skills (don't false-fail); allow
    # comment/blank lines between `skills:` and the first block entry.
    inline = re.sub(r"\s#.*$", "", sm.group(1).strip()).strip()
    inline = "" if inline.lower() in ("[]", "~", "null", "none") else inline.strip("[]").strip()
    block = re.search(r"^skills:[ \t]*(?:#.*)?\r?\n(?:[ \t]*(?:#.*)?\r?\n)*[ \t]+-[ \t]*\S", fm, re.M)
    has_entries = bool(inline) or bool(block)
    if has_entries:
        _, tools = _agent_tools(p)
        if "Skill" not in tools:
            _skill_deadletters.append(a)
if _skill_deadletters:
    add("HIGH", "agent-tools", f"agent(s) declare skills: but lack the Skill tool (dead-letter — can't invoke them): {_skill_deadletters} — add Skill to tools: or remove the skills: block")
else:
    add("OK", "agent-tools", "no skill-declaration dead-letters (every skills-declaring agent carries the Skill tool)")

# 11b. Repo==installed parity. The scope check (11) reads the REPO source, but the INSTALLED
#      ~/.claude/agents/ copy is what actually RUNS — if they drift (e.g. Bash re-added to the
#      installed gate), the audit would pass the blueprint while reality differs. Parity makes the
#      audited source authoritative over the running copy (catches ANY drift, not just tools).
parity_clean = True
for a in ALLOWED_TOOLS:
    rp, ip = os.path.join(REPO_AGENTS, a + ".md"), os.path.join(AGENTS, a + ".md")
    if os.path.exists(rp) and os.path.exists(ip):
        try:
            if open(rp, encoding="utf-8").read() != open(ip, encoding="utf-8").read():
                add("HIGH", "agent-tools", f"native agent {a}: installed ~/.claude/agents/{a}.md DIFFERS from repo source — the installed copy is what runs; reinstall (cp agents/{a}.md ~/.claude/agents/) so the audited source matches reality"); parity_clean = False
        except Exception as e:
            add("MED", "agent-tools", f"could not compare repo vs installed {a}.md: {e}")
if parity_clean:
    add("OK", "agent-tools", "installed native agents match their repo source (the running copy is the audited one)")

# 11c. Parity for native runtime SKILLS too (Codex review 2026-06-08): the /claude-os entry skill
#      is a primary invocation path. A stale or missing installed copy means /claude-os runs
#      something other than the audited source — the same drift risk as the agents.
NATIVE_SKILLS = ["claude-os", "weigh", "enhance", "intent", "discovery-capture", "autopilot", "source-registry", "eval-runner", "session-learner", "ingest", "self-improve", "find-unknowns", "loop-designer"]  # repo skills/<n>/SKILL.md  ->  installed ~/.claude/skills/<n>/SKILL.md  (session-learner registered 2026-06-24 — organ-14's first catch: it was present in skills/ but unlisted, so it skipped parity enforcement; ingest registered 2026-06-25 — the source-ingestion router, owner-requested absorb from the Headroom squeeze; self-improve registered 2026-06-28 — the self-improvement-engine executor runbook, owner-authorized 'two gates permission granted'; find-unknowns registered 2026-07-05 — the four-quadrant unknowns router from the owner-supplied Field Guide to Fable essay, caught unregistered by the acceptance-gate in the SAME change that created it; loop-designer registered 2026-07-06 — the loop-spec maker from the official Getting-started-with-loops article, registered in the SAME change that created it)
skill_parity_clean = True
for s in NATIVE_SKILLS:
    rp = os.path.join(OS_DIR, "skills", s, "SKILL.md")
    ip = os.path.join(SKILLS, s, "SKILL.md")
    if not os.path.exists(rp):
        continue  # not a repo deliverable on this branch
    if not os.path.exists(ip):
        add("HIGH", "skills", f"native entry skill skills/{s}/SKILL.md is NOT installed at ~/.claude/skills/{s}/ — /{s} won't resolve; install it"); skill_parity_clean = False
    elif open(rp, encoding="utf-8").read() != open(ip, encoding="utf-8").read():
        add("HIGH", "skills", f"installed ~/.claude/skills/{s}/SKILL.md DIFFERS from repo source — the installed copy is what /{s} runs; reinstall (cp)"); skill_parity_clean = False
if skill_parity_clean and NATIVE_SKILLS:
    add("OK", "skills", f"native entry skill(s) {NATIVE_SKILLS} installed copy matches repo source")

# 11c-rev. Reverse-presence for native skills (organ-14, 2026-06-24): a SKILL.md in repo skills/ that
#     is absent from NATIVE_SKILLS skips the parity check above — precisely how session-learner sat
#     unregistered + unaudited until organ-14's first sweep flagged it. MED (not HIGH): native skills
#     carry no tool-scope, so the stakes are parity-drift, not a quarantine breach.
_repo_skills_dir = os.path.join(OS_DIR, "skills")
_unregistered_skills = sorted(
    d for d in os.listdir(_repo_skills_dir)
    if os.path.exists(os.path.join(_repo_skills_dir, d, "SKILL.md")) and d not in NATIVE_SKILLS
) if os.path.isdir(_repo_skills_dir) else []
if _unregistered_skills:
    add("MED", "skills", f"repo skills/ has SKILL.md dir(s) NOT in NATIVE_SKILLS: {_unregistered_skills} — they skip repo↔installed parity enforcement; register in bin/audit.py NATIVE_SKILLS or remove")
else:
    add("OK", "skills", "no unregistered native skills (every skills/*/SKILL.md is in NATIVE_SKILLS) — reverse-presence check")

# 11d. Source-registry Multi-Source floor (added 2026-06-13 — the AI-failure watch's first finding,
#      code-enforced). The registry exists to enforce "draw from many sources, never one"; so each
#      domain file must itself carry >=3 source markers. A file that degraded below the floor is the
#      single-source stub the Mandate forbids — caught deterministically here (the ~80%-reliable
#      instruction becomes a ~99% check at the registry/substrate level; per-task use stays advisory).
reg_dir = os.path.join(OS_DIR, "knowledge", "source-registry")
if os.path.isdir(reg_dir):
    thin = []
    for fn in sorted(os.listdir(reg_dir)):
        if not fn.endswith(".md") or fn in ("README.md", "_schema.md"):
            continue
        txt = open(os.path.join(reg_dir, fn), encoding="utf-8").read()
        n = txt.count("[✓]") + txt.count("[~]") + txt.count("[✓✓]")  # ✓ / ~ / ✓✓ markers
        if n < 3:
            thin.append(f"{fn} ({n})")
    if thin:
        add("MED", "source-registry", f"domain file(s) below the Multi-Source floor (>=3 sources): {', '.join(thin)} — grow via /discovery-capture or merge")
    else:
        add("OK", "source-registry", "all source-registry domain files meet the >=3-source Multi-Source floor")

# 11g. Resource-routing coverage (the "employed-or-referred, never idle" guarantee — docs/RESOURCE-ROUTING.md,
#      owner 2026-06-20). Two deterministic substrate legs: if a routing PATH silently breaks, resources go
#      idle-by-oversight unnoticed — this catches that over time. (Skills→index and sources→floor are covered
#      above; this adds map-is-wired + KBs-are-navigable.) MED, not HIGH: idle != broken, never blocks the loop.
_rr = os.path.join(OS_DIR, "docs", "RESOURCE-ROUTING.md")
_pa = os.path.join(OS_DIR, "agents", "project-advisor.md")
_rr_ok = os.path.isfile(_rr)
_rr_wired = _rr_ok and os.path.isfile(_pa) and "RESOURCE-ROUTING" in open(_pa, encoding="utf-8").read()
if not _rr_ok:
    add("MED", "resource-routing", "docs/RESOURCE-ROUTING.md MISSING — the resource-employment map (where each class is reached + what's skippable) is gone; the 'never idle' guarantee is unenforced")
elif not _rr_wired:
    add("MED", "resource-routing", "docs/RESOURCE-ROUTING.md exists but is NOT cited by agents/project-advisor.md — the map is orphaned from the router; rewire §3.5 so every project consults it")
else:
    add("OK", "resource-routing", "resource-routing map present + referenced by project-advisor (employed-or-referred wiring in place; not yet exercised end-to-end)")
_kdir = os.path.join(OS_DIR, "knowledge")
try:
    _kbs = sorted(n for n in os.listdir(_kdir) if n.endswith("_kb") and os.path.isdir(os.path.join(_kdir, n)))
except OSError:
    _kbs = []
def _kb_navigable(kb):
    base = os.path.join(_kdir, kb)
    if any(os.path.isfile(os.path.join(base, f)) for f in ("CLAUDE.md", "README.md", "INDEX.md")):
        return True
    return any("INDEX.md" in files for _root, _dirs, files in os.walk(base))
_idle_kbs = [n for n in _kbs if not _kb_navigable(n)]
if _kbs and _idle_kbs:
    add("MED", "resource-routing", f"KB(s) with no navigational entry (CLAUDE/README/INDEX): {', '.join(_idle_kbs)} — not consultable, so it sits idle; add an index or wire it")
elif _kbs:
    add("OK", "resource-routing", f"all {len(_kbs)} knowledge/*_kb navigable (have an index/README) — reachable; consultation tracked via telemetry, not asserted here")

# 11h. settings.json <-> CLAUDE.md hook-parity (sweep D2, 2026-06-24). The repo CLAUDE.md states how many
#      hooks settings.json wires; that count silently drifted (said "four" while six were wired) with no guard.
#      Count distinct hook scripts in settings.json, confirm CLAUDE.md's stated number matches. MED (doc-drift).
_num_words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
try:
    _sj = json.load(open(os.path.join(OS_DIR, ".claude", "settings.json"), encoding="utf-8"))
    _hook_scripts = set()
    for _groups in _sj.get("hooks", {}).values():
        for _g in _groups:
            for _h in _g.get("hooks", []):
                _m = _re.search(r"hooks/([\w-]+)\.py", _h.get("command", ""))
                if _m:
                    _hook_scripts.add(_m.group(1))
    _nh = len(_hook_scripts)
    _cm = _read(os.path.join(OS_DIR, "CLAUDE.md"))
    _word = _num_words.get(_nh, str(_nh))
    if f"wires {_word} hooks" in _cm or f"wires {_nh} hooks" in _cm:
        add("OK", "doc-drift", f"repo CLAUDE.md hook count matches settings.json ({_nh} hooks: {', '.join(sorted(_hook_scripts))})")
    else:
        add("MED", "doc-drift", f"repo CLAUDE.md hook count drifted — settings.json wires {_nh} ({', '.join(sorted(_hook_scripts))}); CLAUDE.md must read 'wires {_word} hooks'")
except (OSError, ValueError) as _e:
    add("MED", "doc-drift", f"hook-parity check (settings.json<->CLAUDE.md) could not run: {_e}")

# 12. Council-agent least-privilege (hardened 2026-06-08; repo-vendored 2026-06-10). The 5 council
#     agents now live in the repo (agents/*.md, owner-ordered grouping) with installed parity via
#     check 11b — so this check audits the REPO source (the authoritative copy). Each must carry
#     an explicit `tools:` allowlist that EXCLUDES execution / fan-out tools (Bash, Task/Agent,
#     NotebookEdit). Policy: council agents are approved for MANUAL / advisory deliberation only;
#     they are NOT approved for autonomous, write-capable-pipeline, or multi-user flows. Severity is
#     honest: inherit-all or a dangerous tool present is MED for the current manual/advisory use, and
#     must be treated as HIGH before any automated/autonomous use (see tasks/flags.md).
#     The check enforces an EXACT approved set (Codex review 2026-06-08): not merely "no Bash" but
#     "tools == the approved set", so ANY creep (a new tool, Bash returning) is caught. The approved
#     set keeps Write/Edit (the shared_reasoning.md protocol) + WebSearch/WebFetch (council-evidence-
#     discipline MANDATES live search — so web cannot be stripped without breaking the council) and
#     excludes Bash/Task/Agent/NotebookEdit (execution + fan-out). The set itself is still a residual
#     Law 11 trifecta surface — flagged separately + honestly below (not hidden under a green OK).
COUNCIL_ALLOWED = {"Read", "Grep", "Glob", "Write", "Edit", "WebSearch", "WebFetch", "Skill"}
COUNCIL_EXEC = {"Bash", "Task", "Agent"}  # the worst creep: arbitrary execution / uncontrolled fan-out
council_inherit, council_extra, council_missing, council_exec = [], [], [], False
for a in sorted(COUNCIL_AGENTS):
    p = os.path.join(REPO_AGENTS, a + ".md")
    if not os.path.exists(p):
        continue
    state, tools = _agent_tools(p)
    if state is not True:                      # None (malformed) or False (no tools: line) => inherits ALL
        council_inherit.append(a)
        continue
    extra = sorted(tools - COUNCIL_ALLOWED)     # EXACT equality: catch BOTH extra AND missing (Codex 2026-06-08)
    missing = sorted(COUNCIL_ALLOWED - tools)
    if extra:
        council_extra.append(f"{a}{extra}")
        if tools & COUNCIL_EXEC:
            council_exec = True
    if missing:
        council_missing.append(f"{a} missing {missing}")
if council_inherit:
    add("MED", "agent-tools",
        f"{len(council_inherit)} council agent(s) inherit ALL tools (no tools: allowlist): {', '.join(council_inherit)} — unscoped trust boundary. "
        f"They ARE owner-editable regular files, so pin the approved set {sorted(COUNCIL_ALLOWED)}. "
        "MED ceiling ONLY for MANUAL/advisory use; HIGH before any autonomous/write-capable/multi-user use (tasks/flags.md).")
if council_extra:
    add("HIGH" if council_exec else "MED", "agent-tools",
        f"council agent(s) hold tool(s) BEYOND the approved set: {', '.join(council_extra)} — remove. Approved set: {sorted(COUNCIL_ALLOWED)}. "
        + ("Includes Bash/Task/Agent = arbitrary execution / uncontrolled fan-out." if council_exec else "HIGH before autonomous use."))
if council_missing:
    add("MED", "agent-tools",
        f"council agent(s) MISSING approved tool(s): {', '.join(council_missing)} — the toolset is not the exact approved set; either the agent's documented method is now broken (e.g. no Write = can't post to shared_reasoning.md; no WebFetch = can't satisfy council-evidence-discipline) or the approved set drifted. Reconcile.")
if not council_inherit and not council_extra and not council_missing:
    add("OK", "agent-tools", f"all {len(COUNCIL_AGENTS)} council agents pinned to the EXACT approved set {sorted(COUNCIL_ALLOWED)} (exact equality — no extra, no missing)")
# Standing honest residual (only meaningful once they're scoped): the approved set is itself a Law 11
# trifecta surface — disclosed as a visible MED rather than hidden under the agent-tools OK.
# 2026-06-10: design (c) webfetch-egress-guard BUILT (10a2 above tracks built/wired); the standing
# text below reflects the current narrowing honestly instead of the old "PLANNED" wording.
if not council_inherit:
    if eg_py and eg_wired:
        add("MED", "law11",
            "Council trifecta NARROWED by webfetch-egress-guard (wired): council WebFetch is allowlist-only "
            "(crafted-URL exfil denied) and council writes are confined to shared_reasoning.md. Remaining accepted "
            "residuals: exfil via an ALLOWLISTED host's readable channels, redirect chains, and the orchestrating "
            "main session itself. Manual/advisory-only posture unchanged; owner ratification of the design recorded "
            "in tasks/flags.md. Treat as HIGH before autonomous/multi-user.")
    else:
        add("MED", "law11",
            "Council agents' approved toolset (Read+Write+Edit+WebSearch+WebFetch) is a RESIDUAL lethal-trifecta surface: "
            "(a) Read reaches owner/project context, (b) WebFetch ingests untrusted web, (c) WebFetch can exfil to a crafted URL. "
            "NOT closeable by stripping web — council-evidence-discipline mandates live search. The close is BUILT "
            "(bin/hooks/webfetch-egress-guard.py, design (c) council-scoped allowlist + write confinement) but NOT WIRED — "
            "owner action pending (tasks/flags.md). Mitigated now by the evidence-discipline prompt + trifecta-guard "
            "(credential reads) + manual/advisory-only use; treat as HIGH before autonomous/multi-user.")

# 13. Broken local references: docs that claim a claude-os DELIVERABLE exists when it does not.
#     Precision-first but widened (Codex review 2026-06-08): matches the concrete-artifact path
#     shapes (scripts / hooks / native agents / native skills) wherever they appear — inline code,
#     fenced command blocks, or a command like `python3 bin/foo.py` — including ~/claude-os/, ./,
#     and $CLAUDE_PROJECT_DIR/ prefixes. Scoped to bin/agents/skills shapes only (NOT docs/ or
#     tasks/, which include per-project template paths like docs/claude/HANDOFF.md that legitimately
#     don't exist HERE). The path token excludes glob/<> chars by construction ([\w-] only), and the
#     boundary guards prevent matching a substring of a longer/foreign path.
REF_RE = re.compile(
    r"(?<![\w/.$~-])"                                              # left boundary
    r"(?:~/claude-os/|\$CLAUDE_PROJECT_DIR/|\$CLAUDE_OS/|\./)?"    # optional repo-root prefix (stripped)
    r"(bin/[\w-]+\.(?:py|sh)|bin/hooks/[\w-]+\.py|agents/[\w-]+\.md|skills/[\w-]+/SKILL\.md)"
    r"(?![\w])"                                                    # right boundary (not mid-token)
)
md_files = []
for dp, dn, fn in os.walk(OS_DIR):
    # Skip non-authoritative trees: .git/.snapshots/node_modules (machinery), library
    # (symlinked skill source — scanned via its own paths), _local (gitignored quarantine clones).
    dn[:] = [d for d in dn if d not in ('.git', '.snapshots', 'library', 'node_modules', '_local')]
    md_files += [os.path.join(dp, f) for f in fn if f.endswith(".md")]
broken_refs = {}
for mf in md_files:
    try:
        body = open(mf, encoding="utf-8").read()
    except Exception:
        continue
    for ref in set(REF_RE.findall(body)):  # ref is the normalized repo-relative path (prefix dropped)
        if not os.path.exists(os.path.join(OS_DIR, ref)):
            broken_refs.setdefault(ref, set()).add(os.path.relpath(mf, OS_DIR))
if broken_refs:
    for ref, where in sorted(broken_refs.items()):
        add("HIGH", "doc-drift", f"doc(s) reference `{ref}` which does NOT exist — claimed in: {', '.join(sorted(where))}")
else:
    add("OK", "doc-drift", f"no broken deliverable references (scanned {len(md_files)} md files for bin/agents/skills/hooks paths, incl. prefixed + in-command forms)")

# 13b. A4 mapping-staleness guard: KB/registry/doc consult-pointers inside agents/*.md must resolve on
#      disk. Check #13 scans bin/agents/skills/hooks paths but NOT knowledge/ KB pointers, so a renamed
#      or moved KB/registry file rots an agent's consult-pointer SILENTLY (the evals-specialist->evals_kb
#      pattern depends on the path being live). registry-check.py owns the logic (--check-pointers,
#      template-aware); the audit surfaces it MED — a broken consult-pointer misroutes, it does not
#      corrupt (RESOURCE-ROUTING: "routing-path break = MED", never blocks the loop).
_rc_path = os.path.join(OS_DIR, "bin", "registry-check.py")
if os.path.isfile(_rc_path):
    try:
        _rc = subprocess.run([sys.executable, _rc_path, "--check-pointers"],
                             capture_output=True, text=True, timeout=60)
        _ptr_out = (_rc.stdout or "").strip()
        _stale_lines = [l.strip() for l in _ptr_out.splitlines() if "->" in l]
        if _rc.returncode == 0:
            _hdr = _ptr_out.splitlines()[0] if _ptr_out else "pointers resolve"
            add("OK", "doc-drift", f"agent consult-pointers all resolve on disk — {_hdr} (A4 staleness guard)")
        elif _stale_lines:
            add("MED", "doc-drift", "stale agent consult-pointer(s) (A4 guard) — a KB/registry/doc file was "
                "renamed/moved/deleted but an agent still cites it: " + "; ".join(_stale_lines))
        else:
            add("MED", "doc-drift", f"registry-check.py --check-pointers exited {_rc.returncode} with no parseable "
                f"findings (stderr: {(_rc.stderr or '').strip()[:200]})")
    except Exception as e:
        add("MED", "doc-drift", f"could not run the A4 mapping-staleness guard (registry-check.py --check-pointers): {e}")
else:
    add("MED", "files", "bin/registry-check.py missing — the A4 mapping-staleness guard cannot run")

# 14. Public/private contradiction: STATE declares the repo PUBLIC (2026-06-08); flag any doc still
#     asserting it is private (a stale-exposure claim is the dangerous direction to get wrong).
PRIV_RE = re.compile(r"\brepo is (?:now )?private\b", re.I)
priv_hits = []
for mf in md_files:
    try:
        for i, line in enumerate(open(mf, encoding="utf-8"), 1):
            if PRIV_RE.search(line):
                priv_hits.append(f"{os.path.relpath(mf, OS_DIR)}:{i}")
    except Exception:
        pass
if priv_hits:
    add("MED", "exposure", f"doc(s) assert 'repo is private' but STATE declares it PUBLIC (2026-06-08): {', '.join(priv_hits)} — fix the stale exposure claim")
else:
    add("OK", "exposure", "no doc asserts the repo is private (consistent with the PUBLIC declaration)")

# 15. Discovery-weekly auto-push safety: the parked local sweep must not push or skip-permissions
#     unconditionally. Flag if either appears ungated by an env-var guard.
dw = os.path.join("bin", "discovery-weekly.sh")
dw_exec = _sh_exec(_read(dw))
if dw_exec:
    # Path B must be REVIEW-FIRST BY CONSTRUCTION: an autopush can't be made safe (an unattended,
    # push-capable run lets the Claude process itself push mid-run, and `git push` publishes the whole
    # ahead-range, not just what a wrapper guard validated — Codex review 2026-06-09). So the only
    # safe Path B has NO `git push` and NO `--dangerously-skip-permissions` in its executable lines.
    probs = []
    if re.search(r"\bgit\s+push\b", dw_exec):
        probs.append("contains an executable `git push` — Path B must be commit-only (a human reviews + pushes / opens a PR); for unattended automation use the PR-gated Path A")
    if "--dangerously-skip-permissions" in dw_exec:
        probs.append("runs --dangerously-skip-permissions — an injected/erroneous push would not be permission-prompted; Path B must stay attended")
    if probs:
        add("MED", "automation", "discovery-weekly.sh (Path B): " + "; ".join(probs))
    else:
        add("OK", "automation", "discovery-weekly.sh (Path B) is review-first BY CONSTRUCTION: no executable `git push`, no --dangerously-skip-permissions (attended, commit-only — a human pushes)")

# 16. Discovery Path A (the LIVE remote routine) publish posture. Path A is owner cloud infra the
#     repo can't reconfigure, so its posture is tracked in bin/discovery-path-a.status and audited
#     here — making "an AI digest can reach the PUBLIC repo without human review" visible + clearable
#     rather than buried as "future work". (First non-comment line is the status token.)
pa = os.path.join("bin", "discovery-path-a.status")
pa_txt = _read(pa)
if not pa_txt:
    # Fail closed: an undeclared publish posture must NOT pass silently (Codex review 2026-06-08).
    add("HIGH", "automation", "bin/discovery-path-a.status MISSING — Path A (remote routine) publish posture is undeclared; cannot assert it won't push unreviewed AI content to the public main. Create it and set disabled|pr-gated|acknowledged-risk")
else:
    status = next((l.strip() for l in pa_txt.splitlines() if l.strip() and not l.lstrip().startswith("#")), "")
    if status in ("disabled", "pr-gated"):
        # HONESTY (Codex review 2026-06-08): this marker is an owner DECLARATION. The offline audit
        # cannot verify the live RemoteTrigger config / branch protection — so it reports the declared
        # safe posture as a MED "declared, audit-unverified", never a green OK that asserts safety.
        add("MED", "automation", f"Path A (remote routine) DECLARED '{status}' in bin/discovery-path-a.status — owner attestation that no unreviewed AI push reaches public main (Law 4). The audit CANNOT verify the live cloud state (offline/read-only); confirm with `gh api .../branches/main/protection` or the RemoteTrigger console if you need assurance. Not a green OK by design.")
    elif status == "acknowledged-risk":
        add("MED", "automation", "Path A (remote routine): owner EXPLICITLY accepts the unreviewed-publish risk — it can push an AI draft digest to public main before human review; visible + owner-owned, revisit before multi-user/higher-stakes use (bin/discovery-path-a.status)")
    else:
        # unreviewed-publish OR an unrecognized token => FAIL CLOSED (HIGH). An AI push to a public
        # repo without human review violates Law 4; the audit must not exit 0 while that's possible.
        add("HIGH", "automation", f"Path A (remote routine) posture is '{status or 'unset'}' — it can push an AI-generated digest to the PUBLIC main WITHOUT human review (Law 4 violation). FAIL-CLOSED. Resolve to: disabled | pr-gated (branch protection / draft-branch+PR) | acknowledged-risk (explicit owner accept). See bin/discovery-path-a.status, tasks/flags.md.")

# 17. Governance-coverage ledger freshness (2026-06-27, self-improvement-engine Phase 0). The
#     coverage ledger (docs/GOVERNANCE-COVERAGE.md) answers "is every rule mechanically enforced or
#     trusted from memory?" — but a ledger nothing enforces drifts silently (exactly the failure the
#     ledger exists to expose). bin/coverage-ledger.py --check validates the curated rule->mechanism map
#     against the LIVE inventory: every Law in NORTH-STAR mapped, every referenced audit check still
#     present. Drift = MED (the map misrepresents coverage; it misleads, it doesn't corrupt — same class
#     as #13b). "Build the check, not another rule" (lessons.md).
_cl_path = os.path.join(OS_DIR, "bin", "coverage-ledger.py")
_cl_doc = os.path.join(OS_DIR, "docs", "GOVERNANCE-COVERAGE.md")
if os.path.isfile(_cl_path):
    if not os.path.isfile(_cl_doc):
        add("MED", "doc-drift", "docs/GOVERNANCE-COVERAGE.md MISSING — regenerate: python3 bin/coverage-ledger.py")
    try:
        _cl = subprocess.run([sys.executable, _cl_path, "--check"],
                             capture_output=True, text=True, timeout=60)
        _cl_out = (_cl.stdout or "").strip()
        if _cl.returncode == 0:
            add("OK", "doc-drift", f"governance-coverage ledger map matches live inventory — {_cl_out.splitlines()[0] if _cl_out else 'no drift'} (coverage-ledger #17)")
        else:
            add("MED", "doc-drift", "governance-coverage ledger DRIFT (coverage-ledger #17) — a Law was added without a "
                "rule->mechanism mapping, or a referenced audit check was removed: " + "; ".join(
                    l.strip("- ").strip() for l in _cl_out.splitlines() if l.strip().startswith("-")))
    except Exception as _e:
        add("MED", "doc-drift", f"coverage-ledger.py --check failed to run ({type(_e).__name__}: {str(_e)[:120]})")

# ---- 18. Model-vocabulary fence (2026-07-01 model-agnostic rework) -------------------
#     Anthropic model names live in exactly ONE dated file: docs/MODEL-ROUTING.md. Anywhere
#     else in LIVE doctrine they go stale on a model switch (the failure the rework killed).
#     Allowlisted as history/dated-by-design: docs/research/, docs/adr/, STATE-ARCHIVE*,
#     STATE.md + tasks/ (dated ledgers/artifacts — route tags are dated by convention),
#     memory/archive/ + codex-model-effort.md (cross-vendor seat). Scan is glob-scoped, so
#     history dirs are simply never globbed.
import glob as _glob
_mv_pats = [
    re.compile(r"claude-(?:fable|opus|sonnet|haiku|mythos)[0-9.\-]*", re.I),
    re.compile(r"\b(?:Fable|Mythos)\s*5(?:\.\d+)?\b"),
    re.compile(r"\bOpus\s*4\.\d\b"),
    re.compile(r"\bSonnet\s*(?:4\.\d|5)\b"),
    re.compile(r"\bHaiku\s*4\.5\b"),
    re.compile(r"`(?:fable|opus|sonnet|haiku)`"),   # the override-token idiom outside the lineup
]
_mv_files = [p for p in _glob.glob(os.path.join(OS_DIR, "docs", "*.md"))
             if "STATE-ARCHIVE" not in p and not p.endswith("MODEL-ROUTING.md")]
_mv_files += _glob.glob(os.path.join(OS_DIR, "docs", "portable", "*.md"))
_mv_files += _glob.glob(os.path.join(OS_DIR, "agents", "*.md"))
_mv_files += _glob.glob(os.path.join(OS_DIR, "skills", "*", "SKILL.md"))
_mv_files += [os.path.join(OS_DIR, x) for x in ("CLAUDE.md", "GUIDE.md", "MANIFEST.md", "START-HERE.md")]
_mv_files += [os.path.join(OS_DIR, "library", "CLAUDE.md"), os.path.join(HOME, ".claude", "CLAUDE.md")]
_mv_mem = os.path.join(HOME, ".claude", "projects", "-example-claude-os-project", "memory")
if os.path.isdir(_mv_mem):
    _mv_files += [os.path.join(_mv_mem, f) for f in os.listdir(_mv_mem)
                  if f.endswith(".md") and f != "codex-model-effort.md"]
_mv_hits = []
for _p in _mv_files:
    if not os.path.isfile(_p):
        continue
    try:
        _s = open(_p, encoding="utf-8", errors="replace").read()
    except OSError:
        continue
    _n = sum(len(_pat.findall(_s)) for _pat in _mv_pats)
    if _n:
        _mv_hits.append(f"{os.path.relpath(_p, OS_DIR) if _p.startswith(OS_DIR) else _p} ({_n})")
if _mv_hits:
    add("HIGH", "model-fence", "Anthropic model name(s) in live doctrine OUTSIDE docs/MODEL-ROUTING.md — "
        "stale-on-model-switch hazard (2026-07-01 rework invariant). De-model to seat/tier words + a lineup "
        "pointer, or move dated content to an allowlisted history home: " + "; ".join(_mv_hits[:8]))
else:
    add("OK", "model-fence", f"model vocabulary confined to docs/MODEL-ROUTING.md (scanned {len(_mv_files)} live-doctrine files)")

# ---- 19. Always-loaded size budgets (2026-07-01 rework) ------------------------------
#     The always-loaded surface taxes EVERY session; rules without checks regrow (STATE.md
#     reached 115KB under its own 'lean by design' header). Budgets in BYTES; breach = MED,
#     >=1.5x = HIGH. Absence is check 9c's business, never a size finding.
_budgets = [
    (os.path.join(HOME, ".claude", "CLAUDE.md"), 8000, "global ~/.claude/CLAUDE.md"),
    (os.path.join(OS_DIR, "CLAUDE.md"), 2000, "repo CLAUDE.md"),
    (os.path.join(OS_DIR, "library", "CLAUDE.md"), 6000, "library/CLAUDE.md"),
    (os.path.join(_mv_mem, "MEMORY.md"), 11000, "memory index MEMORY.md"),
    (os.path.join(OS_DIR, "STATE.md"), 16000, "STATE.md"),
]
for _p, _cap, _label in _budgets:
    if not os.path.isfile(_p):
        continue
    _sz = os.path.getsize(_p)
    if _sz > int(_cap * 1.5):
        add("HIGH", "size-budget", f"{_label} is {_sz}B — ≥1.5× its {_cap}B always-loaded budget; archive/compress per its own lean rule")
    elif _sz > _cap:
        add("MED", "size-budget", f"{_label} is {_sz}B — over its {_cap}B always-loaded budget; trim or archive")
    else:
        add("OK", "size-budget", f"{_label} {_sz}B within its {_cap}B budget")

# ---- report ----
order = {"HIGH": 0, "MED": 1, "OK": 2}
findings.sort(key=lambda f: order.get(f[0], 3))
high = sum(1 for f in findings if f[0] == "HIGH")
med = sum(1 for f in findings if f[0] == "MED")
print("=== claude-os integrity audit ===")
for sev, cat, msg in findings:
    mark = {"HIGH": "🔴", "MED": "🟠", "OK": "🟢"}.get(sev, "•")
    print(f"{mark} [{sev:4}] {cat:8} {msg}")
verdict = "FAIL — fix HIGH findings" if high else ("CONCERNS — review MED" if med else "PASS")
print(f"\nVERDICT: {verdict}  ({high} HIGH, {med} MED)")
sys.exit(1 if high else 0)
