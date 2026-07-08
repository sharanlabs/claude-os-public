#!/usr/bin/env python3
"""trifecta-guard — PreToolUse hook enforcing Law 11 (NORTH-STAR §11).

Breaks Willison's "lethal trifecta" by hard-denying the **private-data leg**:
deny tool calls that read a credential *location*. Remove leg (a) and an
injected, exfil-capable reader has nothing worth leaking. Legs (b) untrusted-
input and (c) WebFetch-exfil remain by design; the trifecta needs all three.

HONEST SCOPE (cross-model reviewed, Codex GPT-5.5, 2026-06-08):
  - Hard-denies the **path-tools** (Read/Edit/Write/NotebookEdit/Grep/Glob) and
    WebFetch `file://` — these screen reliably (path is in the tool input).
  - Bash is **best-effort only**: a read-command (cat/less/cp/dd/…) targeting a
    credential path is caught, but arbitrary Bash can construct a path the
    scanner never sees (`p=$HOME/.aws/c; cat $p`). Bash is NOT a hard boundary
    here. The real enforcement for the quarantined readers is that they have
    **no Bash at all** (their `tools:` allowlist); this hook is defence-in-depth
    for the maker agents that do have Bash. A full close needs per-agent Bash
    denial or OS-level read-deny on protected paths (tracked: tasks/todo.md).
  - Denylist is PATH-ANCHORED to credential locations, never the word "secret".
  - FAIL-OPEN on unparseable input (a crashing security hook must not block all
    work); matched-tool inputs are type-guarded so logic errors aren't masked.

Self-test:  python3 bin/hooks/trifecta-guard.py --self-test
"""
import sys, os, json, fnmatch, re

HOME = os.path.expanduser("~")
CASEFOLD = sys.platform == "darwin" or os.name == "nt"  # case-insensitive FS

# --- Credential LOCATIONS (anchored to paths, not words) ---
SECRET_DIRS = [os.path.join(HOME, d) for d in
               (".ssh", ".aws", ".gnupg", ".gcp", ".azure", ".kube")]
SECRET_FILES = [os.path.join(HOME, *p) for p in (
    (".codex", "auth.json"), (".claude", ".credentials.json"),
    (".netrc",), (".git-credentials",), (".npmrc",), (".pypirc",),
    (".config", "gh", "hosts.yml"))]
SECRET_GLOBS = ["*.pem", "*.p12", "*.pfx", "*.key", "id_rsa", "id_rsa.*",
                "id_ed25519", "id_ecdsa", "id_dsa", ".env", ".env.*"]
# templates / fixtures are safe even though they match a secret glob
ALLOW_GLOBS = ["*.example", "*.sample", "*.template", "*.dist",
               ".env.example", ".env.sample", ".env.template",
               "example*", "sample*", "fixture*", "dummy*", "test_*", "*_test.*"]

# The untrusted-web-content readers (Law 11): they must stay strictly read-only.
# Their `tools:` allowlist already omits these, but enforce it at the hook too so a
# future toolset misconfiguration can't silently re-open the trifecta (uses the
# `agent_type` field the hook receives inside a subagent call).
QUARANTINED_READERS = {"discovery-scout", "guidelines-monitor"}
READER_FORBIDDEN = {"Bash", "Write", "Edit", "NotebookEdit"}

PATH_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}   # tool_input.file_path
ROOT_TOOLS = {"Grep", "Glob"}                            # tool_input.path (search root)
# Bash read/copy/encode commands that actually surface file contents
READ_CMDS = {"cat", "less", "more", "head", "tail", "bat", "nl", "tac",
             "cp", "mv", "dd", "xxd", "od", "strings", "base64", "base32",
             "tar", "zip", "scp", "rsync", "sftp", "openssl", "gpg",
             "sed", "awk", "grep", "rg", "vi", "vim", "nano", "open"}


def _casefold(p):
    return p.lower() if CASEFOLD else p


def _resolve(p, cwd):
    """Expand ~, make absolute against cwd, and resolve symlinks (realpath).
    Returns BOTH the lexical-abs and the realpath so either can be screened."""
    if not p or not isinstance(p, str):
        return []
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(cwd or os.getcwd(), p)
    lex = os.path.normpath(p)
    out = [lex]
    try:
        rp = os.path.realpath(p)            # follows symlinks incl. parents
        if rp != lex:
            out.append(rp)
    except Exception:
        pass
    return out


def _is_secret(ap):
    """True if a single absolute path is a credential location."""
    apf = _casefold(os.path.normpath(ap))
    base = os.path.basename(apf)
    if any(fnmatch.fnmatch(base, _casefold(g)) for g in ALLOW_GLOBS):
        return False
    for d in SECRET_DIRS:
        df = _casefold(d)
        if apf == df or apf.startswith(df + os.sep):
            return True
    if apf in (_casefold(f) for f in SECRET_FILES):
        return True
    if any(fnmatch.fnmatch(base, _casefold(g)) for g in SECRET_GLOBS):
        return True
    return False


def is_secret_path(p, cwd=""):
    """True if any resolution of `p` (lexical or realpath) is a secret location."""
    return any(_is_secret(ap) for ap in _resolve(p, cwd))


def root_hits_secret(root, cwd):
    """For Grep/Glob: deny if the search root IS a secret location OR an
    ANCESTOR that contains one (e.g. Grep rooted at $HOME descends into ~/.aws)."""
    for ap in _resolve(root, cwd):
        if _is_secret(ap):
            return True
        apf = _casefold(os.path.normpath(ap))
        for d in SECRET_DIRS:
            df = _casefold(d)
            if df == apf or df.startswith(apf + os.sep):   # secret dir under root
                return True
        for f in SECRET_FILES:
            if _casefold(f).startswith(apf + os.sep):
                return True
    return False


def glob_pattern_hits_secret(pattern):
    """A Glob/Grep pattern that explicitly descends into a credential dir/file."""
    if not isinstance(pattern, str):
        return False
    pl = _casefold(pattern)
    needles = [".ssh/", ".aws/", ".gnupg/", ".gcp/", ".azure/", ".kube/",
               ".codex/auth", ".git-credentials", ".netrc", "id_rsa",
               "id_ed25519", ".npmrc", ".pypirc"]
    return any(n in pl for n in needles)


def bash_reads_secret(command, cwd):
    """Best-effort: a recognized READ command with a secret path as an argument.
    Narrow on purpose — a command that merely *mentions* a path (e.g. an echo /
    a forwarded prompt) is NOT blocked, only an actual read/copy/encode of it."""
    if not isinstance(command, str) or not command:
        return False
    # split into simple sub-commands on ; | && || newlines
    for seg in re.split(r"[\n;|]|&&|\|\|", command):
        toks = [t for t in re.split(r"\s+", seg.strip()) if t]
        if not toks:
            continue
        # find the leading command word (skip env assignments like FOO=bar)
        ci = 0
        while ci < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[ci]):
            ci += 1
        if ci >= len(toks):
            continue
        cmd = os.path.basename(toks[ci])
        if cmd not in READ_CMDS:
            continue
        for arg in toks[ci + 1:]:
            arg = arg.strip("'\"")
            if arg.startswith("-"):
                continue
            if (arg.startswith("~") or arg.startswith("/") or arg.startswith(".")
                    or os.sep in arg) and is_secret_path(arg, cwd):
                return True
            if is_secret_path(arg, cwd):     # bare basename like id_rsa
                return True
    return False


def decide(data):
    """Return (deny: bool, reason: str). Pure; type-guarded (no exception-masking)."""
    if not isinstance(data, dict):
        return False, ""
    tool = data.get("tool_name", "")
    # Quarantine: a known untrusted-web reader may not run code or write, period.
    if data.get("agent_type") in QUARANTINED_READERS and tool in READER_FORBIDDEN:
        return True, f"Law 11: quarantined reader '{data.get('agent_type')}' may not use {tool} (read-only)"
    ti = data.get("tool_input")
    if not isinstance(ti, dict):
        return False, ""        # malformed/absent input for a matched tool -> allow (fail-open)
    cwd = data.get("cwd") or os.getcwd()

    if tool in PATH_TOOLS:
        if is_secret_path(ti.get("file_path", ""), cwd):
            return True, f"Law 11: blocked {tool} of a credential location"
    elif tool in ROOT_TOOLS:
        if root_hits_secret(ti.get("path", ""), cwd):
            return True, f"Law 11: blocked {tool} rooted at/over a credential location"
        if glob_pattern_hits_secret(ti.get("pattern", "")) and tool == "Glob":
            return True, "Law 11: blocked Glob pattern targeting a credential location"
    elif tool == "WebFetch":
        url = ti.get("url", "")
        if isinstance(url, str) and url.lower().startswith("file:"):
            local = re.sub(r"^file://(localhost)?", "", url, flags=re.I)
            if is_secret_path(local, cwd) or local:    # any file:// is suspect for a web tool
                return True, "Law 11: blocked WebFetch of a local file:// URL"
    elif tool == "Bash":
        if bash_reads_secret(ti.get("command", ""), cwd):
            return True, "Law 11: blocked a Bash read of a credential location (best-effort)"
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
        ctx = data.get("agent_type") if isinstance(data, dict) else None
        if ctx:
            reason += f" [agent: {ctx}]"
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason}}))
        return 0
    print("{}")
    return 0


# --------------------------- self-test ---------------------------
def _self_test():
    repo = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    ssh_key = os.path.join(HOME, ".ssh", "id_rsa")
    cases = [
        # --- must DENY ---
        ("read codex auth", {"tool_name": "Read",
            "tool_input": {"file_path": os.path.join(HOME, ".codex", "auth.json")}}, True),
        ("read ssh key", {"tool_name": "Read", "tool_input": {"file_path": ssh_key}}, True),
        ("read a .pem in /tmp", {"tool_name": "Read",
            "tool_input": {"file_path": "/tmp/x.pem"}}, True),
        ("bash cat ssh key", {"tool_name": "Bash",
            "tool_input": {"command": "cat ~/.ssh/id_rsa | head"}}, True),
        ("bash var-free read aws", {"tool_name": "Bash",
            "tool_input": {"command": "base64 ~/.aws/credentials"}}, True),
        ("grep rooted in .ssh", {"tool_name": "Grep",
            "tool_input": {"pattern": "x", "path": os.path.join(HOME, ".ssh")}}, True),
        ("grep rooted at $HOME (ancestor)", {"tool_name": "Grep",
            "tool_input": {"pattern": "x", "path": HOME}}, True),
        ("glob pattern into .aws", {"tool_name": "Glob",
            "tool_input": {"pattern": "**/.aws/credentials"}}, True),
        ("webfetch file:// secret", {"tool_name": "WebFetch",
            "tool_input": {"url": "file:///Users/x/.ssh/id_rsa"}}, True),
        ("case variant .AWS", {"tool_name": "Read",
            "tool_input": {"file_path": os.path.join(HOME, ".AWS", "credentials")}}, CASEFOLD),
        ("quarantined reader tries Bash", {"tool_name": "Bash", "agent_type": "discovery-scout",
            "tool_input": {"command": "ls"}}, True),
        ("quarantined reader tries Write", {"tool_name": "Write", "agent_type": "guidelines-monitor",
            "tool_input": {"file_path": "/tmp/x.txt"}}, True),
        # --- must ALLOW (no false positives) ---
        ("read repo lessons (word 'secret')", {"tool_name": "Read",
            "tool_input": {"file_path": os.path.join(repo, "tasks", "lessons.md")}}, False),
        ("grep 'secret' across repo", {"tool_name": "Grep",
            "tool_input": {"pattern": "secret", "path": repo}}, False),
        ("read .env.example", {"tool_name": "Read",
            "tool_input": {"file_path": os.path.join(repo, ".env.example")}}, False),
        ("read example.pem fixture", {"tool_name": "Read",
            "tool_input": {"file_path": os.path.join(repo, "docs", "example.pem")}}, False),
        ("bash that MENTIONS a path but doesn't read it", {"tool_name": "Bash",
            "tool_input": {"command": "echo 'review the ~/.ssh and *.pem handling'"}}, False),
        ("quarantined reader Read of a repo file (allowed)", {"tool_name": "Read",
            "agent_type": "discovery-scout",
            "tool_input": {"file_path": os.path.join(repo, "tasks", "lessons.md")}}, False),
        ("normal git status", {"tool_name": "Bash",
            "tool_input": {"command": "git status --porcelain"}}, False),
        ("normal python run", {"tool_name": "Bash",
            "tool_input": {"command": "python3 bin/audit.py"}}, False),
        ("malformed tool_input (list)", {"tool_name": "Read", "tool_input": []}, False),
    ]
    ok = True
    for name, payload, expect in cases:
        got, reason = decide(payload)
        if got != expect:
            ok = False
        print(f"  [{'OK ' if got == expect else 'FAIL'}] {name}: deny={got} (exp {expect})"
              f"{'  -> '+reason if got else ''}")
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
