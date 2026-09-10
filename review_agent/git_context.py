import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .config import Config, ReviewError
from .privacy import exclusion, redact


def git(repo: Path, *args: str, limit=4_000_000) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_LITERAL_PATHSPECS="1", LC_ALL="C")
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "diff.external=", "-C", str(repo), *args]
    if args[0] == "diff":
        # Git diff can invoke clean/process filters even with --no-ext-diff.
        # Disable every configured driver for this invocation only.
        names = git(repo, "config", "--null", "--name-only", "--list", limit=1_000_000)
        drivers = set()
        for key in names.decode("utf-8", errors="replace").split("\0"):
            match = re.fullmatch(r"filter\.(.+)\.(clean|process|required)", key)
            if match:
                drivers.add(match.group(1))
        overrides = []
        for driver in sorted(drivers):
            overrides.extend(["-c", f"filter.{driver}.clean=", "-c", f"filter.{driver}.process=",
                              "-c", f"filter.{driver}.required=false"])
        command[1:1] = overrides
    try:
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.run(command, stdout=out, stderr=err, env=env, timeout=20, check=False)
            if proc.returncode:
                # Raw stderr can contain credentials or repository content.
                raise ReviewError(f"Git {args[0]} failed. Check repository state, refs, and permissions.")
            if out.tell() > limit:
                raise ReviewError("Git output exceeds the review limit; narrow this change first.")
            out.seek(0)
            return out.read()
    except FileNotFoundError as exc:
        raise ReviewError("Git is required. Install Git and retry.") from exc
    except subprocess.TimeoutExpired as exc:
        raise ReviewError("Git operation timed out after 20 seconds.") from exc


def repo_root(path: str | Path) -> Path:
    path = Path(path).expanduser().resolve()
    if not path.is_dir():
        raise ReviewError("Repository directory does not exist.")
    return Path(os.fsdecode(git(path, "rev-parse", "--show-toplevel").strip())).resolve()


def commit(repo: Path, ref: str) -> str:
    if not isinstance(ref, str) or not ref or ref.startswith("-") or len(ref) > 256 or any(ord(c) < 32 for c in ref):
        raise ReviewError("Invalid Git reference.")
    return git(repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}").decode().strip()


def parse_patch(patch: str):
    added, removed, new_lines, old_lines = [], [], {}, {}
    old = new = None
    for line in patch.splitlines():
        match = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", line)
        if match:
            old, new = map(int, match.groups())
        elif old is not None and line.startswith("+"):
            added.append(new)
            new_lines[str(new)] = line[1:]
            new += 1
        elif old is not None and line.startswith("-"):
            removed.append(old)
            old_lines[str(old)] = line[1:]
            old += 1
        elif old is not None and line.startswith(" "):
            new_lines[str(new)] = old_lines[str(old)] = line[1:]
            old += 1
            new += 1
    return added, removed, new_lines, old_lines


def _regular_snapshot(repo, path, mode):
    """Reject symlinks/submodules in the compared snapshot, including deleted ones."""
    if mode == "base":
        entries = git(repo, "ls-tree", "-z", "HEAD", "--", path).split(b"\0")
    else:
        entries = git(repo, "ls-files", "--stage", "-z", "--", path).split(b"\0")
    for entry in entries:
        if not entry:
            continue
        if not entry.startswith((b"100644 ", b"100755 ")):
            return False
    if mode == "worktree":
        full = repo / path
        # Check every component; never follow a symlink to read untracked content.
        for parent in [full, *full.parents]:
            if parent == repo:
                break
            if parent.is_symlink():
                return False
        if full.exists() and not stat.S_ISREG(full.stat().st_mode):
            return False
    return True


def collect(repo_path: str | Path, cfg: Config, mode="worktree", base: str | None = None) -> dict:
    repo = repo_root(repo_path)
    if mode not in {"worktree", "staged", "base"}:
        raise ReviewError("mode must be worktree, staged, or base.")
    if base is not None and mode != "base":
        raise ReviewError("base is only valid with mode=base.")
    try:
        head = commit(repo, "HEAD")
    except ReviewError as exc:
        raise ReviewError("Repository needs an initial commit before review.") from exc
    if git(repo, "ls-files", "--unmerged", "-z"):
        raise ReviewError("Resolve merge conflicts before requesting a review.")
    merge_base = None
    if mode == "base":
        if not base:
            raise ReviewError("Supply --base for branch review, for example --base main.")
        base_id = commit(repo, base)
        merge_base = git(repo, "merge-base", base_id, head).decode().strip()
        comparison = [merge_base, head]
    elif mode == "staged":
        comparison = ["--cached", head]
    else:
        comparison = [head]
    flags = ["--no-ext-diff", "--no-textconv", "--no-renames", "--no-color"]
    changed = git(repo, "diff", *flags, "--name-only", "-z", *comparison, "--").split(b"\0")
    untracked = set()
    if mode == "worktree":
        untracked = set(git(repo, "ls-files", "--others", "--exclude-standard", "-z").split(b"\0")) - {b""}
    paths = sorted(set(changed) | untracked)
    paths = [x for x in paths if x]
    # Explicitly fail instead of partially enumerating a huge repository.
    if len(paths) > 2000:
        raise ReviewError("More than 2,000 changed files; narrow the change before reviewing.")
    files, skipped, used, redactions = [], [], 0, 0
    for raw in paths:
        try:
            path = raw.decode("utf-8")
        except UnicodeDecodeError:
            skipped.append({"path": "<non-UTF8 path>", "reason": "unsupported path encoding"})
            continue
        reason = exclusion(path, cfg.exclude)
        if not reason and len(files) >= cfg.max_files:
            reason = "file count budget"
        if not reason and not _regular_snapshot(repo, path, mode):
            reason = "symlink, submodule, or non-regular file"
        if reason:
            skipped.append({"path": path, "reason": reason})
            continue
        try:
            if raw in untracked:
                full = repo / path
                with full.open("rb") as stream:
                    data = stream.read(cfg.max_file_bytes + 1)
                if len(data) > cfg.max_file_bytes:
                    raise ReviewError("file size budget")
                if b"\0" in data:
                    raise ReviewError("binary file")
                text = data.decode("utf-8")
                patch = "".join(difflib.unified_diff([], text.splitlines(keepends=True), fromfile="/dev/null", tofile="b/" + path, n=8))
                status = "untracked"
            else:
                data = git(repo, "diff", *flags, "--unified=8", *comparison, "--", path,
                           limit=cfg.max_file_bytes)
                patch = data.decode("utf-8")
                if "GIT binary patch" in patch or re.search(r"^Binary files .* differ$", patch, re.M):
                    raise ReviewError("binary file")
                status = "deleted" if "deleted file mode" in patch else "added" if "new file mode" in patch else "modified"
            # Redact code while retaining patch markers and exact line locations.
            patch, count = redact(patch)
            added, removed, new_lines, old_lines = parse_patch(patch)
            entry = {"path": path, "status": status, "patch": patch, "added_lines": added,
                     "removed_lines": removed, "new_lines": new_lines, "old_lines": old_lines}
            size = len(json.dumps(entry, ensure_ascii=True))
            if used + size > cfg.max_context_chars:
                raise ReviewError("context character budget")
            used += size
            redactions += count
            files.append(entry)
        except UnicodeDecodeError:
            skipped.append({"path": path, "reason": "non-UTF8 text"})
        except (ReviewError, OSError) as exc:
            skipped.append({"path": path, "reason": str(exc) if isinstance(exc, ReviewError) else "file unavailable during review"})
    if commit(repo, "HEAD") != head:
        raise ReviewError("HEAD changed during collection. Retry with a stable repository.")
    digest = hashlib.sha256(json.dumps({"head": head, "mode": mode, "base": merge_base,
                                       "files": files, "skipped": skipped}, sort_keys=True).encode()).hexdigest()
    return {"snapshot_id": digest, "head": head, "mode": mode, "merge_base": merge_base,
            "files": files, "skipped": skipped, "changed_files": len(paths), "redactions": redactions,
            "context_chars": used, "scope": "Changed hunks plus 8 surrounding lines; no full-repository analysis."}
