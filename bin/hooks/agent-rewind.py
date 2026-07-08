#!/usr/bin/env python3
"""agent-rewind — PreToolUse SAFETY-NET hook (Do-no-harm, Law 1).

Snapshot IMMEDIATELY BEFORE a destructive action, then ALLOW it. One-click
restore via `--restore`. This is the "agent rewind" capability from the
2026-06-16 agent-security harvest (tasks/ai-failure-watch.md): every security
expert pairs an external policer with a *recovery/undo layer* — "assume breach,
snapshot before destructive, one-click restore." Our reversibility-class +
bin/snapshot.py gestured at it; this operationalizes it.

WHAT IT IS — and IS NOT:
  - A SAFETY NET, not a blocker. It NEVER denies and NEVER asks — on EVERY code
    path it emits the allow JSON ("{}"). Ask/deny on the same destructive set is
    owned by the `careful` skill ONLY WHEN that skill is explicitly loaded (it is
    a user-level skill, NOT a wired hook); in a default session this snapshot is
    the sole automated net, and the db/git-history classes below get neither a
    snapshot nor a gate (logged only). A net that blocks would be a worse `careful`.
  - DETERMINISTIC + Law-11-safe: no fetched-content execution, no shell=True, no
    secrets read, no network. The destructive command is matched lexically; the
    target path is resolved with realpath; the snapshot is taken by handing the
    resolved path as a *list argument* to bin/snapshot.py (NEVER interpolated
    into a shell string — that would re-introduce the very injection we guard).

PER-CLASS snapshot mechanism (a single tool can't cover all four — a DROP TABLE
or a force-push is not a file-content copy):
  - fs_path     rm / rm -rf, `> file` truncation, file overwrite/delete
                -> REAL snapshot via bin/snapshot.py, which SCREENS credential/
                   secret files OUT of the backup (Law 11 — never proliferate
                   secrets, even into the rewind store). Restore pointer = snap
                   dir. This is the core deliverable + the round-trip the
                   self-test proves (mutate -> restore actually restores).
  - git_worktree git reset --hard, git checkout ., git restore .
                -> `git stash create` mints a commit SHA capturing the uncommitted
                   work WITHOUT touching the tree or the user's stash stack
                   (verified). Restore = `git checkout <sha> -- .` (snapshot
                   semantic; `stash apply` is a 3-way MERGE and fails when the
                   tree diverged — wrong for a deterministic restore). HEAD also
                   recorded.
  - git_history  git push --force / -f
                -> record the local ref SHA about to overwrite the remote. Restore
                   is a re-push of that SHA (logged, manual — a hook can't re-push
                   safely). PARTIAL by nature.
  - db           DROP / TRUNCATE
                -> DETECT + LOG ONLY. A deterministic hook holds no DB
                   connection/credentials, so it CANNOT auto-snapshot a database.
                   Logging "no snapshot, reason=db-no-creds" is the honest move;
                   pretending otherwise is false safety (the dangerous failure).

FAIL-OPEN IS LOUD. "allowed with snapshot X" and "allowed with NO snapshot
(reason ...)" are distinct log lines. A net that silently fails-open gives false
security. Size-ceiling rejections, refuse_dangerous, unresolved $var targets,
db/history classes — all still ALLOW, but log the reason so the gap is visible.

HONEST v1 SCOPE (matches the trifecta-guard precedent of stating limits):
  - Bash-only matcher. The named destructive set all lives in Bash (rm, >, git,
    SQL). The Write/Edit-tool overwrite case is detectable but adds latency to
    every write; left as a documented future extension.
  - Variable-constructed targets (`p=$HOME; rm $p/x`) are NOT expanded — Law 11
    forbids executing the command to learn $p. We detect the class, see the arg
    holds an unexpanded $var/backtick/$(...), and log "target UNRESOLVED, no
    snapshot." We NEVER snapshot a bogus literal "$p/x". (Same honest scope
    trifecta-guard documents for var-constructed reads.)
  - DB + remote-history classes: detected + logged, not auto-snapshotted.
  - No GC/retention on the snapshot store — it grows one entry per fs/worktree
    destructive command. A cap is optional for v1 (note, not blocker).

Self-test:  python3 bin/hooks/agent-rewind.py --self-test
Restore:    python3 bin/hooks/agent-rewind.py --restore           # last pointer
            python3 bin/hooks/agent-rewind.py --restore <pointer> # named
            python3 bin/hooks/agent-rewind.py --list              # show log
"""
import sys, os, json, re, subprocess, datetime, shlex

HOME = os.path.expanduser("~")
# __file__ = <repo>/bin/hooks/agent-rewind.py -> three dirnames to the repo root
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SNAPSHOT_PY = os.path.join(REPO, "bin", "snapshot.py")
# Rewind log + the snapshot store the fs class writes into (separate from the
# generic .snapshots so a restore can find rewind-owned entries unambiguously).
REWIND_DIR = os.path.join(REPO, ".rewind")
REWIND_LOG = os.path.join(REWIND_DIR, "rewind-log.jsonl")
REWIND_SNAP_DEST = os.path.join(REWIND_DIR, "snapshots")

# Markers that mean "this argument is not a resolvable literal path" — we must NOT
# snapshot a literal "$p/x". Detect the class, log UNRESOLVED, take no snapshot.
DYNAMIC = ("$", "`", "~+", "*", "?", "[")  # var, backtick/$(), glob — non-literal

# Special device files are NEVER recoverable snapshot targets. A redirect to one
# (`2>/dev/null`, `cmd >/dev/null 2>&1`) is the standard discard idiom, not a
# destructive truncation of a real file — classifying it as destructive spams the
# rewind log with false-positive FAILED entries on nearly every command.
PURE_DISCARD_DEVICES = frozenset({
    "/dev/null", "/dev/zero", "/dev/stdout", "/dev/stderr",
    "/dev/tty", "/dev/random", "/dev/urandom",
})


def _device_class(tok):
    """Classify a redirect/path token's device nature:
      'discard' — /dev/null & friends: never a real file, skip silently.
      'fd'      — /dev/fd/N: backed by an unknown file descriptor that COULD be a
                  real file; never silently drop it — log it unresolved instead.
      None      — an ordinary path; handle normally.
    """
    if tok in PURE_DISCARD_DEVICES:
        return "discard"
    if tok == "/dev/fd" or tok.startswith("/dev/fd/"):
        return "fd"
    return None


# Credential / secret locations that must NEVER be copied into the rewind store
# (Law 11: do not proliferate secrets). Mirrors bin/hooks/trifecta-guard.py's set
# AND bin/snapshot.py's is_sensitive_path (keep all three in sync, incl. the
# path-tail credential files below). This predicate is LOAD-BEARING: snapshot.py
# honors an explicit single-file target (filter_files=False), so for a direct
# `rm ~/.claude/.credentials.json` THIS check is the only thing stopping that
# secret from being copied into the rewind store. A destructive op on a secret
# still PROCEEDS (fail-open net) but is logged, not copied.
_SECRET_DIRS = (".ssh", ".aws", ".gnupg", ".gcp", ".azure", ".kube", ".codex")
_SECRET_NAMES = (".netrc", ".git-credentials", ".npmrc", ".pypirc")
_SECRET_GLOBS = ("*.pem", "*.p12", "*.pfx", "*.key",
                 "id_rsa", "id_rsa.*", "id_ed25519", "id_ecdsa", "id_dsa")
# Multi-component credential files a bare-basename test can't express; anchor on
# the path TAIL so we don't blanket-skip all of .claude / .config. (.codex/auth.json
# is already covered by the _SECRET_DIRS component.)
_SECRET_PATH_TAILS = ((".claude", ".credentials.json"), (".config", "gh", "hosts.yml"))


def _is_sensitive_path(real):
    """True if a resolved path is a credential/secret location (path-anchored:
    a path component, a basename, a glob match, or a path-tail credential file)
    — mirrors trifecta-guard + snapshot.py. The .env family counts, but NOT
    .env.example/.sample/.template (templates)."""
    import fnmatch
    parts = real.split(os.sep)
    base = parts[-1] if parts else real
    if any(d in parts for d in _SECRET_DIRS):
        return True
    if base in _SECRET_NAMES:
        return True
    if base.startswith(".env") and not base.endswith((".example", ".sample", ".template")):
        return True
    if any(len(parts) >= len(tail) and tuple(parts[-len(tail):]) == tail
           for tail in _SECRET_PATH_TAILS):
        return True
    return any(fnmatch.fnmatch(base, g) for g in _SECRET_GLOBS)


# ----------------------------- logging -----------------------------
def _log(entry):
    """Append one JSONL line to the rewind log. Best-effort; never raises."""
    try:
        os.makedirs(REWIND_DIR, exist_ok=True)
        entry = dict(entry)
        entry.setdefault("ts", datetime.datetime.now().isoformat(timespec="seconds"))
        with open(REWIND_LOG, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never break the safety net's allow-path


# ----------------------- destructive classification -----------------------
def _segments(command):
    """Split a Bash command into simple sub-commands on ; | && || and newlines.
    Lexical only — we never execute. Mirrors trifecta-guard's segmenting."""
    return [s.strip() for s in re.split(r"[\n;]|&&|\|\||(?<!>)\|", command) if s.strip()]


def _lead_cmd_and_args(seg):
    """Return (cmd_basename, args[]) for a segment, skipping leading FOO=bar env
    assignments. Tokenize with shlex so quoting is handled; fall back to a naive
    split if shlex chokes on an unbalanced quote (still lexical, never executed)."""
    try:
        toks = shlex.split(seg, posix=True)
    except ValueError:
        toks = [t for t in re.split(r"\s+", seg) if t]
    i = 0
    while i < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[i]):
        i += 1
    if i >= len(toks):
        return None, []
    return os.path.basename(toks[i]), toks[i + 1:]


def _seg_is_dynamic(seg):
    """True if a SEGMENT contains command-substitution or a variable anywhere.
    When it does, shlex mis-splits the construct (e.g. `$(cat x)` -> `$(cat`,
    `x)`), so NO path token in that segment can be trusted as a literal target.
    We then mark all targets unresolved rather than snapshot a bogus fragment."""
    return bool(re.search(r"\$\(|\$\{|\$[A-Za-z_]|`", seg))


def _is_literal_path(arg):
    """A target we can safely realpath. False for $var / $(...) / glob / shell
    fragment / flag. Conservative: any of $ ` ( ) glob-char or a stray quote
    means 'not a clean literal' -> we won't snapshot it."""
    if not arg or arg.startswith("-"):
        return False
    bad = set(DYNAMIC) | {"(", ")", '"', "'"}
    return not any(m in arg for m in bad)


def _resolve(arg, cwd):
    """Expand ~ and make absolute against cwd, then realpath. arg is already
    known-literal (no $/glob). Returns realpath or None on failure."""
    try:
        p = os.path.expanduser(arg)
        if not os.path.isabs(p):
            p = os.path.join(cwd or os.getcwd(), p)
        return os.path.realpath(p)
    except Exception:
        return None


def classify(command, cwd):
    """Return a list of detected destructive ops:
        [{"class": str, "pattern": str, "targets": [literal_args],
          "unresolved": [dynamic_args]}]
    Lexical match against the careful/freeze destructive set, path-anchored.
    Pure; never executes the command."""
    if not isinstance(command, str) or not command.strip():
        return []
    ops = []
    lower = command.lower()

    # --- DB: DROP TABLE/DATABASE, TRUNCATE (whole-command, SQL is not seg-bound) ---
    if re.search(r"\bdrop\s+(table|database)\b", lower):
        ops.append({"class": "db", "pattern": "drop", "targets": [], "unresolved": []})
    # SQL TRUNCATE [TABLE] <name>. Require it NOT be the shell `truncate(1)` binary
    # (which truncates a FILE and is handled as fs_path below). Anchor on the SQL
    # form: `truncate` followed by an identifier or the word TABLE, not by a flag.
    if re.search(r"\btruncate\s+(table\b|[a-z_][a-z0-9_.\"`]*\s*;|[a-z_][a-z0-9_.\"`]*\s*$)", lower) \
       and not re.search(r"\btruncate\s+-", lower):
        ops.append({"class": "db", "pattern": "truncate", "targets": [], "unresolved": []})

    for seg in _segments(command):
        seg_lower = seg.lower()

        # --- git history: force-push ---
        if re.search(r"git\s+push\b", seg_lower) and re.search(r"(-f\b|--force(?:-with-lease)?\b)", seg_lower):
            ops.append({"class": "git_history", "pattern": "force_push", "targets": [], "unresolved": []})

        # --- git working-tree: reset --hard, checkout ., restore . ---
        if re.search(r"git\s+reset\s+--hard", seg_lower):
            ops.append({"class": "git_worktree", "pattern": "reset_hard", "targets": [], "unresolved": []})
        if re.search(r"git\s+(checkout|restore)\s+(\.|\s-- )", seg_lower) or \
           re.search(r"git\s+(checkout|restore)\s+\.", seg_lower):
            ops.append({"class": "git_worktree", "pattern": "discard", "targets": [], "unresolved": []})

        cmd, args = _lead_cmd_and_args(seg)
        seg_dynamic = _seg_is_dynamic(seg)  # shlex can't be trusted in this seg

        # --- fs: the shell `truncate(1)` binary shrinks/zeros a FILE ---
        if cmd == "truncate":
            path_args = [a for a in args if not a.startswith("-")]
            # `truncate -s 0 file` — the size value follows -s; drop a bare numeric
            path_args = [a for a in path_args if not re.fullmatch(r"[0-9KMGTkmgt+%-]+", a)]
            if path_args:
                if seg_dynamic:
                    lits, dyn = [], path_args
                else:
                    lits = [a for a in path_args if _is_literal_path(a)]
                    dyn = [a for a in path_args if not _is_literal_path(a)]
                ops.append({"class": "fs_path", "pattern": "truncate_file",
                            "targets": lits, "unresolved": dyn})

        # --- fs: rm -r / rm -rf / rm --recursive (recursive delete) ---
        if cmd == "rm":
            flags = [a for a in args if a.startswith("-")]
            recursive = any(("r" in f.lower() and not f.startswith("--")) or f == "--recursive"
                            for f in flags)
            # also catch plain `rm file` (single-file delete is destructive too)
            path_args = [a for a in args if not a.startswith("-")]
            if path_args:
                if seg_dynamic:
                    # segment has $(...) / $var — every token is suspect, snapshot none
                    lits, dyn = [], path_args
                else:
                    lits = [a for a in path_args if _is_literal_path(a)]
                    dyn = [a for a in path_args if not _is_literal_path(a)]
                ops.append({"class": "fs_path",
                            "pattern": "rm_recursive" if recursive else "rm",
                            "targets": lits, "unresolved": dyn})

        # --- fs: `> file` truncation / overwrite (NOT >>) ---
        # match a single > followed by a path token, not preceded by another >
        # `> file` and `>| file` (noclobber override) truncation/overwrite (NOT >>).
        # _segments() keeps `>|` intact (its split skips a `|` preceded by `>`).
        for m in re.finditer(r"(?<!>)>\|?(?!>)\s*([^\s;|&>]+)", seg):
            tok = m.group(1)
            dev = _device_class(tok)
            if dev == "discard":
                continue  # `>/dev/null`, `2>/dev/null` — discard idiom, not destructive
            if dev == "fd" or not (_is_literal_path(tok) and not seg_dynamic):
                ops.append({"class": "fs_path", "pattern": "truncate_redirect",
                            "targets": [], "unresolved": [tok]})
            else:
                ops.append({"class": "fs_path", "pattern": "truncate_redirect",
                            "targets": [tok], "unresolved": []})

    return ops


# ----------------------------- snapshotting -----------------------------
def _parse_sensitive_skips(stdout):
    """Finding 3: pull the credential/secret skip count snapshot.py prints as
    'snapshot: N credential/secret item(s) intentionally NOT backed up ...'.
    Returns an int (0 if absent). Swallowing this notice would make a restore
    look complete though a screened-out secret is gone."""
    m = re.search(r"snapshot:\s+(\d+)\s+credential/secret item\(s\) intentionally NOT",
                  stdout or "")
    return int(m.group(1)) if m else 0


def _snapshot_fs(target_real, cwd):
    """Snapshot a filesystem target via bin/snapshot.py (list-arg subprocess,
    NEVER shell). Returns (pointer_or_None, status_str, detail, n_sensitive)."""
    if not os.path.exists(target_real):
        return None, "SKIPPED", "target does not exist (nothing to snapshot)", 0
    try:
        os.makedirs(REWIND_SNAP_DEST, exist_ok=True)
        proc = subprocess.run(
            [sys.executable, SNAPSHOT_PY, target_real,
             "--dest", REWIND_SNAP_DEST, "--label", "rewind"],
            capture_output=True, text=True, timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None, "FAILED", "snapshot timed out (target too large?)", 0
    except Exception as e:
        return None, "FAILED", f"snapshot invocation error: {e}", 0
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        return None, "FAILED", reason[-1] if reason else f"snapshot exit {proc.returncode}", 0
    n_sensitive = _parse_sensitive_skips(proc.stdout)
    # parse the "snapshot: OK -> <dir>" line for the pointer
    for line in proc.stdout.splitlines():
        if line.startswith("snapshot: OK -> "):
            detail = "filesystem snapshot taken"
            if n_sensitive:
                detail += (f" ({n_sensitive} credential/secret item(s) screened out — "
                           f"NOT in this snapshot, Law 11)")
            return line[len("snapshot: OK -> "):].strip(), "OK", detail, n_sensitive
    return None, "FAILED", "snapshot ran but emitted no pointer", 0


def _git_root(cwd):
    try:
        p = subprocess.run(["git", "-C", cwd or os.getcwd(), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=10)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


def _snapshot_git_worktree(cwd):
    """Capture uncommitted work via `git stash create` (does NOT touch tree or
    stash stack). Returns (pointer_dict_or_None, status, detail)."""
    root = _git_root(cwd)
    if not root:
        return None, "SKIPPED", "not inside a git work tree"
    try:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        head_sha = head.stdout.strip() if head.returncode == 0 else None
        stash = subprocess.run(["git", "-C", root, "stash", "create", "agent-rewind"],
                               capture_output=True, text=True, timeout=30)
    except Exception as e:
        return None, "FAILED", f"git error: {e}"
    if stash.returncode != 0:
        return None, "FAILED", (stash.stderr or "git stash create failed").strip()
    stash_sha = stash.stdout.strip()
    if not stash_sha:
        # clean tree — nothing uncommitted to lose; HEAD pointer is still useful
        return {"git_root": root, "head": head_sha, "stash": None}, "OK", \
               "clean tree (no uncommitted changes); HEAD recorded"
    return {"git_root": root, "head": head_sha, "stash": stash_sha}, "OK", \
           "uncommitted work captured to stash commit"


def _snapshot_git_history(cwd):
    """Record the local HEAD/ref SHA a force-push would overwrite. Restore is a
    manual re-push (a hook must not re-push). Returns (pointer_dict, status, detail)."""
    root = _git_root(cwd)
    if not root:
        return None, "SKIPPED", "not inside a git work tree"
    try:
        head = subprocess.run(["git", "-C", root, "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        sha = head.stdout.strip() if head.returncode == 0 else None
    except Exception as e:
        return None, "FAILED", f"git error: {e}"
    if not sha:
        return None, "FAILED", "could not resolve HEAD"
    return {"git_root": root, "head": sha}, "OK", \
           "force-push detected; local HEAD recorded (restore = manual re-push)"


# ----------------------------- the hook core -----------------------------
def process(command, cwd):
    """Classify the command, snapshot each destructive op per-class, log each.
    Returns a list of log entries (also written). NEVER raises to the caller."""
    entries = []
    ops = classify(command, cwd)
    for op in ops:
        cls = op["class"]
        base = {"action": "pre-snapshot", "class": cls, "pattern": op["pattern"],
                "cwd": cwd, "command_preview": command[:200]}

        if cls == "fs_path":
            # snapshot each resolvable literal target; log unresolved separately
            for arg in op.get("targets", []):
                # special device (rm/truncate /dev/null) — not a recoverable file;
                # log honestly rather than emit a misleading FAILED snapshot.
                if _device_class(arg) == "discard":
                    e = dict(base, target=arg, snapshot="SKIPPED",
                             reason="special device — not a recoverable file")
                    _log(e); entries.append(e); continue
                real = _resolve(arg, cwd)
                if real is None:
                    e = dict(base, target=arg, snapshot="FAILED",
                             reason="could not resolve target path")
                elif _is_sensitive_path(real):
                    # Law 11: never copy a credential/secret into the rewind store.
                    # The op still proceeds (fail-open); we just don't snapshot it.
                    e = dict(base, target=real, snapshot="SKIPPED",
                             reason="sensitive/credential path — not snapshotted (Law 11)")
                else:
                    ptr, status, detail, n_sensitive = _snapshot_fs(real, cwd)
                    e = dict(base, target=real, snapshot=status,
                             pointer=ptr, reason=detail)
                    if n_sensitive:
                        # Finding 3: surface the screened-out-secret count so a
                        # later restore is not silently incomplete.
                        e["sensitive_skipped"] = n_sensitive
                _log(e); entries.append(e)
            for arg in op.get("unresolved", []):
                e = dict(base, target=arg, snapshot="SKIPPED",
                         reason="target is variable/glob/command-substitution — "
                                "not expanded (Law 11); destructive op detected, no snapshot")
                _log(e); entries.append(e)
            if not op.get("targets") and not op.get("unresolved"):
                e = dict(base, snapshot="SKIPPED", reason="no path argument found")
                _log(e); entries.append(e)

        elif cls == "git_worktree":
            ptr, status, detail = _snapshot_git_worktree(cwd)
            e = dict(base, snapshot=status, pointer=ptr, reason=detail)
            _log(e); entries.append(e)

        elif cls == "git_history":
            ptr, status, detail = _snapshot_git_history(cwd)
            e = dict(base, snapshot=status, pointer=ptr, reason=detail)
            _log(e); entries.append(e)

        elif cls == "db":
            e = dict(base, snapshot="SKIPPED",
                     reason="database class — a deterministic hook holds no DB "
                            "connection/credentials; cannot auto-snapshot. Detected + logged only.")
            _log(e); entries.append(e)

    return entries


def main():
    """PreToolUse entrypoint. Reads the hook JSON from stdin, snapshots if the
    Bash command is destructive, ALWAYS emits the allow JSON. Backstop fail-open."""
    try:
        data = json.loads(sys.stdin.read())
    except Exception:
        print("{}")            # fail-open on unparseable stdin
        return 0
    try:
        if isinstance(data, dict) and data.get("tool_name") == "Bash":
            ti = data.get("tool_input")
            if isinstance(ti, dict):
                cmd = ti.get("command", "")
                cwd = data.get("cwd") or os.getcwd()
                if isinstance(cmd, str) and cmd:
                    process(cmd, cwd)
    except Exception:
        pass                   # backstop: never let the net block the action
    print("{}")                # ALWAYS allow — this is a net, not a gate
    return 0


# ----------------------------- restore CLI -----------------------------
def _read_log():
    if not os.path.exists(REWIND_LOG):
        return []
    out = []
    with open(REWIND_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def cmd_list():
    rows = _read_log()
    if not rows:
        print("agent-rewind: no rewind log yet.")
        return 0
    print(f"agent-rewind: {len(rows)} logged op(s) (newest last):\n")
    for i, r in enumerate(rows):
        ptr = r.get("pointer")
        ptr_s = ptr if isinstance(ptr, str) else (json.dumps(ptr) if ptr else "-")
        print(f"  [{i}] {r.get('ts','?')}  {r.get('class','?')}/{r.get('pattern','?')}  "
              f"snapshot={r.get('snapshot','?')}")
        print(f"        target : {r.get('target', r.get('cwd','?'))}")
        print(f"        pointer: {ptr_s}")
        ns = r.get("sensitive_skipped")
        if ns:
            # Finding 3: a snapshot with screened-out secrets is INCOMPLETE by
            # design — the restore will not bring those credential files back.
            print(f"        WARNING: {ns} credential/secret item(s) were NOT "
                  f"snapshotted (Law 11) — restore will be incomplete for them")
        if r.get("snapshot") != "OK":
            print(f"        reason : {r.get('reason','')}")
    return 0


def _restore_fs(snap_dir):
    """Restore a filesystem snapshot by running its RESTORE.sh (which snapshot.py
    wrote + verified). List-arg subprocess; never shell-interpolated."""
    restore_sh = os.path.join(snap_dir, "RESTORE.sh")
    if not os.path.isfile(restore_sh):
        print(f"agent-rewind: no RESTORE.sh in {snap_dir}", file=sys.stderr)
        return 1
    print(f"agent-rewind: restoring filesystem snapshot {snap_dir}")
    proc = subprocess.run(["bash", restore_sh])
    if proc.returncode == 0:
        print("agent-rewind: filesystem restore OK.")
    return proc.returncode


def _restore_git_worktree(ptr):
    """Restore uncommitted work from a `git stash create` SHA via checkout
    (snapshot semantic, not a merge). Restores the WHOLE tree to the snapshot."""
    root = ptr.get("git_root")
    stash = ptr.get("stash")
    if not root:
        print("agent-rewind: git pointer missing git_root", file=sys.stderr)
        return 1
    if not stash:
        print("agent-rewind: snapshot was a clean tree (no uncommitted work to restore); "
              f"HEAD was {ptr.get('head')}. Nothing to do.")
        return 0
    print(f"agent-rewind: restoring uncommitted work from stash {stash[:12]} in {root}")
    proc = subprocess.run(["git", "-C", root, "checkout", stash, "--", "."])
    if proc.returncode == 0:
        print("agent-rewind: working-tree restore OK.")
    return proc.returncode


def cmd_restore(pointer_arg):
    rows = _read_log()
    # pick the entry: named pointer, an index like '@3', or the last OK snapshot
    entry = None
    if pointer_arg:
        if pointer_arg.startswith("@") and pointer_arg[1:].isdigit():
            idx = int(pointer_arg[1:])
            if 0 <= idx < len(rows):
                entry = rows[idx]
        else:
            for r in reversed(rows):
                p = r.get("pointer")
                if (isinstance(p, str) and p == pointer_arg) or \
                   (isinstance(p, dict) and pointer_arg in (p.get("stash"), p.get("head"))):
                    entry = r
                    break
            if entry is None and os.path.isdir(pointer_arg):
                # a raw snapshot dir path
                return _restore_fs(pointer_arg)
    else:
        for r in reversed(rows):
            if r.get("snapshot") == "OK" and r.get("pointer"):
                entry = r
                break
    if entry is None:
        print(f"agent-rewind: no restorable snapshot found "
              f"({'for ' + pointer_arg if pointer_arg else 'in log'}).", file=sys.stderr)
        print("agent-rewind: run --list to see logged pointers.", file=sys.stderr)
        return 1
    cls = entry.get("class")
    ptr = entry.get("pointer")
    if entry.get("snapshot") != "OK" or not ptr:
        print(f"agent-rewind: that entry has no usable snapshot "
              f"(snapshot={entry.get('snapshot')}, reason={entry.get('reason')}).", file=sys.stderr)
        return 1
    if cls == "fs_path":
        ns = entry.get("sensitive_skipped")
        if ns:
            # Finding 3: warn so a partial restore isn't mistaken for a full one.
            print(f"agent-rewind: WARNING — this snapshot screened out {ns} "
                  f"credential/secret item(s) (Law 11); they were NOT backed up and "
                  f"will NOT be restored. Restore is INCOMPLETE for those files.",
                  file=sys.stderr)
        return _restore_fs(ptr)
    if cls == "git_worktree":
        return _restore_git_worktree(ptr)
    if cls == "git_history":
        print(f"agent-rewind: git_history restore is MANUAL by design. The local "
              f"HEAD recorded before the force-push was {ptr.get('head')}.")
        print(f"  To undo the force-push, from {ptr.get('git_root')} run:")
        print(f"    git push --force-with-lease origin {ptr.get('head')}:<branch>")
        return 0
    print(f"agent-rewind: don't know how to restore class '{cls}'.", file=sys.stderr)
    return 1


# ----------------------------- self-test -----------------------------
def _self_test():
    import tempfile, shutil
    ok = True

    def check(name, cond, extra=""):
        nonlocal ok
        ok = ok and cond
        print(f"  [{'OK ' if cond else 'FAIL'}] {name}{('  -> ' + extra) if extra and not cond else ''}")

    print("agent-rewind: --self-test\n")
    cwd = REPO  # a real dir for resolution; no destructive op is actually run

    # ---- classification cases (no snapshot side effects) ----
    cases = [
        # (name, command, expected_classes_set)
        ("rm -rf dir",            "rm -rf /tmp/x",                {"fs_path"}),
        ("rm -r dir",             "rm -r build/out",              {"fs_path"}),
        ("rm single file",        "rm notes.txt",                 {"fs_path"}),
        ("rm --recursive",        "rm --recursive /tmp/y",        {"fs_path"}),
        ("> truncation",          "echo hi > config.json",        {"fs_path"}),
        ("redirect to /dev/null", "make 2>/dev/null",             set()),       # discard idiom, not destructive
        (">/dev/null 2>&1",       "run.sh >/dev/null 2>&1",       set()),       # both streams discarded
        ("real-file > caught",    "echo x > out.log",             {"fs_path"}), # a real file IS still snapshotted
        ("noclobber >| file",     "echo x >| out.log",            {"fs_path"}),  # noclobber truncation IS destructive
        ("noclobber >| /dev/null","printf x >| /dev/null",        set()),        # still the discard idiom
        ("redirect to /dev/fd/N", "cmd > /dev/fd/3",              {"fs_path"}),  # fd-backed: logged, not silently dropped
        ("DROP TABLE",            "psql -c 'DROP TABLE users;'",  {"db"}),
        ("DROP DATABASE",         "mysql -e 'drop database app'", {"db"}),
        ("TRUNCATE",              "psql -c 'TRUNCATE orders;'",   {"db"}),
        ("git push --force",      "git push --force origin main", {"git_history"}),
        ("git push -f",           "git push -f origin main",      {"git_history"}),
        ("git reset --hard",      "git reset --hard HEAD~2",      {"git_worktree"}),
        ("git checkout .",        "git checkout .",               {"git_worktree"}),
        ("git restore .",         "git restore .",                {"git_worktree"}),
        ("force-with-lease",      "git push --force-with-lease origin m", {"git_history"}),
        ("chained rm",            "cat x; rm -rf /tmp/y",         {"fs_path"}),
        ("shell truncate file",   "truncate -s 0 app.log",        {"fs_path"}),  # NOT db
        ("SQL truncate table",    "psql -c 'TRUNCATE TABLE t'",   {"db"}),
        # non-destructive -> empty
        ("plain ls",              "ls -la",                       set()),
        ("append >> not >",       "echo log >> file.txt",         set()),
        ("git status",            "git status --porcelain",       set()),
        ("git push (no force)",   "git push origin main",         set()),
        ("python run",            "python3 bin/audit.py",         set()),
        ("git checkout branch",   "git checkout main",            set()),  # branch switch, not '.'
        ("git restore --staged",  "git restore --staged f",       set()),  # index only, tree untouched
        ("grep mentions DROP",    "grep -r DROP src/",            set()),  # mention, not exec
        ("quoted rm string",      'echo "rm -rf /"',              set()),  # not a real rm
        ("rm of build artifact",  "rm -rf node_modules",          {"fs_path"}),  # still snapshots; careful skips
    ]
    for name, cmd, expect in cases:
        got = {op["class"] for op in classify(cmd, cwd)}
        check(f"classify: {name}", got == expect, f"got {got} exp {expect}")

    # ---- path-anchored: var-constructed target must NOT be snapshotted ----
    ops = classify("p=$HOME; rm $p/x", cwd)
    fs = [o for o in ops if o["class"] == "fs_path"]
    unres = fs and fs[0]["unresolved"] and not fs[0]["targets"]
    check("var-constructed `rm $p/x` -> UNRESOLVED, no literal target", bool(unres),
          f"ops={ops}")

    # backtick / command-substitution target -> unresolved
    ops2 = classify("rm -rf $(cat list.txt)", cwd)
    fs2 = [o for o in ops2 if o["class"] == "fs_path"]
    check("`rm -rf $(...)` -> unresolved (not snapshotted)",
          bool(fs2 and fs2[0]["unresolved"] and not fs2[0]["targets"]))

    # ---- the real round-trip: snapshot -> mutate -> restore actually restores ----
    work = tempfile.mkdtemp(prefix="rewind-selftest-")
    saved_log, saved_dir = REWIND_LOG, REWIND_DIR
    saved_snap = REWIND_SNAP_DEST
    try:
        # redirect the log + store into the temp area so the test is hermetic
        globals()["REWIND_DIR"] = os.path.join(work, ".rewind")
        globals()["REWIND_LOG"] = os.path.join(work, ".rewind", "rewind-log.jsonl")
        globals()["REWIND_SNAP_DEST"] = os.path.join(work, ".rewind", "snapshots")

        victim = os.path.join(work, "victim.txt")
        with open(victim, "w") as f:
            f.write("ORIGINAL CONTENT\n")

        # the hook fires on `rm -rf victim.txt` BEFORE it runs -> snapshot taken
        entries = process(f"rm -rf {victim}", work)
        fs_entry = next((e for e in entries if e.get("class") == "fs_path"), None)
        check("fs snapshot taken on rm", bool(fs_entry and fs_entry.get("snapshot") == "OK"),
              f"entries={entries}")
        check("restore pointer logged", bool(fs_entry and fs_entry.get("pointer")))

        # now SIMULATE the destructive action: delete the file
        os.remove(victim)
        check("victim deleted (disaster simulated)", not os.path.exists(victim))

        # restore via the CLI path (last OK pointer)
        rc = cmd_restore(None)
        check("--restore returned 0", rc == 0)
        check("victim file restored", os.path.exists(victim))
        if os.path.exists(victim):
            check("restored content matches original",
                  open(victim).read() == "ORIGINAL CONTENT\n")

        # mutate (not delete) then restore -> must overwrite back to original
        with open(victim, "w") as f:
            f.write("CORRUPTED BY DESTRUCTIVE OP\n")
        process(f"rm -rf {victim}", work)  # fresh snapshot of current... wait: re-snapshot original?
        # the second snapshot captured the CORRUPTED content; to prove "restore a
        # mutated file" we restore the FIRST snapshot explicitly by its pointer.
        first_ptr = fs_entry["pointer"]
        with open(victim, "w") as f:
            f.write("MUTATED AGAIN\n")
        rc2 = cmd_restore(first_ptr)
        check("--restore <named pointer> returned 0", rc2 == 0)
        check("named-pointer restore brings back ORIGINAL",
              os.path.exists(victim) and open(victim).read() == "ORIGINAL CONTENT\n")

        # collision fix (Finding 2): two real targets in ONE command must BOTH
        # snapshot -- same-second dir names must not collide and drop the 2nd.
        a2 = os.path.join(work, "a.txt"); b2 = os.path.join(work, "b.txt")
        with open(a2, "w") as f:
            f.write("A\n")
        with open(b2, "w") as f:
            f.write("B\n")
        multi = process(f"rm -rf {a2} {b2}", work)
        fs_multi = [e for e in multi if e.get("class") == "fs_path"]
        check("multi-target `rm a b`: BOTH snapshotted (no same-second collision)",
              len(fs_multi) == 2 and all(e.get("snapshot") == "OK" for e in fs_multi),
              f"entries={fs_multi}")

        # special-device consistency (Finding 5): `rm /dev/null` -> SKIPPED not FAILED
        dev_entries = process("rm -f /dev/null", work)
        dev_e = next((e for e in dev_entries if e.get("class") == "fs_path"), None)
        check("`rm /dev/null` -> SKIPPED special device (not a misleading FAILED)",
              bool(dev_e and dev_e.get("snapshot") == "SKIPPED"
                   and "device" in dev_e.get("reason", "")))

        # sensitive-path skip (Finding 1, Law 11): a credential path is NOT copied
        ssh_dir = os.path.join(work, ".ssh")
        os.makedirs(ssh_dir, exist_ok=True)
        key = os.path.join(ssh_dir, "id_rsa")
        with open(key, "w") as f:
            f.write("PRIVATE KEY\n")
        sens = process(f"rm -f {key}", work)
        sens_e = next((e for e in sens if e.get("class") == "fs_path"), None)
        check("credential path -> SKIPPED, not snapshotted (Law 11)",
              bool(sens_e and sens_e.get("snapshot") == "SKIPPED"
                   and "sensitive" in sens_e.get("reason", "")))

        # path-tail credential as a DIRECT target (Codex Finding 2): snapshot.py
        # honors explicit single-file targets, so THIS predicate is the only gate
        # for `rm ~/.claude/.credentials.json`. Must SKIP, not copy into the store.
        claude_dir = os.path.join(work, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        creds = os.path.join(claude_dir, ".credentials.json")
        with open(creds, "w") as f:
            f.write('{"token":"SECRET"}\n')
        tail = process(f"rm -f {creds}", work)
        tail_e = next((e for e in tail if e.get("class") == "fs_path"), None)
        check(".claude/.credentials.json (path-tail secret, direct rm) -> SKIPPED",
              bool(tail_e and tail_e.get("snapshot") == "SKIPPED"
                   and "sensitive" in tail_e.get("reason", "")))

        # NESTED sensitive skip (the HIGH the auditor caught): `rm -rf <dir>` with
        # a nested credential must snapshot the tree but NOT sweep the secret into
        # the rewind store. The direct-target test above did NOT cover this path.
        proj = os.path.join(work, "proj")
        os.makedirs(proj)
        with open(os.path.join(proj, "app.py"), "w") as f:
            f.write("real code\n")
        with open(os.path.join(proj, ".env"), "w") as f:
            f.write("API_KEY=nested-secret\n")
        nested = process(f"rm -rf {proj}", work)
        swept, kept = [], []
        for root, _d, files in os.walk(REWIND_SNAP_DEST):
            for fn in files:
                if fn == ".env":
                    swept.append(os.path.join(root, fn))
                if fn == "app.py":
                    kept.append(os.path.join(root, fn))
        check("nested secret NOT swept into the rewind store (the HIGH, Law 11)",
              not swept, f"swept={swept}")
        check("nested `rm -rf dir`: the real file WAS snapshotted",
              bool(kept), "app.py missing from the rewind store")
        # Codex Finding 3: the screened-out-secret count must surface on the log
        # entry (not be swallowed) so a later restore isn't silently incomplete.
        nested_e = next((e for e in nested if e.get("class") == "fs_path"
                         and e.get("snapshot") == "OK"), None)
        check("nested-secret snapshot surfaces sensitive_skipped count (Finding 3)",
              bool(nested_e and nested_e.get("sensitive_skipped", 0) >= 1),
              f"entry={nested_e}")

        # db class -> detected, logged, NO pointer
        db_entries = process("psql -c 'DROP TABLE t;'", work)
        db_e = next((e for e in db_entries if e.get("class") == "db"), None)
        check("db op detected + logged with no snapshot",
              bool(db_e and db_e.get("snapshot") == "SKIPPED" and not db_e.get("pointer")))

        # non-destructive -> no log entry produced
        none_entries = process("ls -la", work)
        check("non-destructive command -> no rewind entry", none_entries == [])

    finally:
        globals()["REWIND_DIR"] = saved_dir
        globals()["REWIND_LOG"] = saved_log
        globals()["REWIND_SNAP_DEST"] = saved_snap
        shutil.rmtree(work, ignore_errors=True)

    # ---- git working-tree round-trip in a throwaway repo ----
    grepo = tempfile.mkdtemp(prefix="rewind-git-")
    try:
        def g(*a):
            return subprocess.run(["git", "-C", grepo, *a], capture_output=True, text=True)
        g("init", "-q"); g("config", "user.email", "t@t.t"); g("config", "user.name", "t")
        fp = os.path.join(grepo, "f.txt")
        with open(fp, "w") as f:
            f.write("committed\n")
        g("add", "f.txt"); g("commit", "-qm", "init")
        with open(fp, "w") as f:
            f.write("uncommitted-work\n")  # this is what reset --hard would destroy
        ptr, status, _ = _snapshot_git_worktree(grepo)
        check("git_worktree snapshot OK", status == "OK" and ptr and ptr.get("stash"))
        # simulate `git reset --hard` destroying the uncommitted work
        g("checkout", "-q", "--", "f.txt")
        check("uncommitted work destroyed (reset simulated)",
              open(fp).read() == "committed\n")
        rc = _restore_git_worktree(ptr)
        check("git_worktree restore returned 0", rc == 0)
        check("uncommitted work restored", open(fp).read() == "uncommitted-work\n")
    finally:
        shutil.rmtree(grepo, ignore_errors=True)

    # ---- fail-open: malformed stdin / non-Bash tool -> always "{}" ----
    p = subprocess.run([sys.executable, os.path.abspath(__file__)],
                       input="not json", capture_output=True, text=True)
    check("fail-open on malformed stdin -> '{}'", p.stdout.strip() == "{}")
    p2 = subprocess.run([sys.executable, os.path.abspath(__file__)],
                        input=json.dumps({"tool_name": "Read",
                                          "tool_input": {"file_path": "/tmp/x"}}),
                        capture_output=True, text=True)
    check("non-Bash tool -> allow '{}'", p2.stdout.strip() == "{}")
    p3 = subprocess.run([sys.executable, os.path.abspath(__file__)],
                        input=json.dumps({"tool_name": "Bash",
                                          "tool_input": {"command": "ls -la"}}),
                        capture_output=True, text=True)
    check("Bash non-destructive -> allow '{}'", p3.stdout.strip() == "{}")

    print(f"\nSELF-TEST: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _usage():
    print(__doc__.split("\n\n")[0])
    print("\nUsage:")
    print("  (PreToolUse hook)   echo '<hook-json>' | python3 bin/hooks/agent-rewind.py")
    print("  python3 bin/hooks/agent-rewind.py --self-test")
    print("  python3 bin/hooks/agent-rewind.py --list")
    print("  python3 bin/hooks/agent-rewind.py --restore [pointer|@index]")


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(_self_test())
    if "--list" in sys.argv:
        sys.exit(cmd_list())
    if "--restore" in sys.argv:
        i = sys.argv.index("--restore")
        ptr = sys.argv[i + 1] if i + 1 < len(sys.argv) else None
        sys.exit(cmd_restore(ptr))
    if "--help" in sys.argv or "-h" in sys.argv:
        _usage()
        sys.exit(0)
    sys.exit(main())
