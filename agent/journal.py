"""Change journal: workspace snapshots, per-action diffs, hashes and safe rollback."""
import base64
import difflib
import hashlib
import os
from pathlib import Path

from .policy import PolicyError
from .tools import GENERATED_DIRS, GENERATED_SUFFIXES


class ConflictError(Exception):
    pass


def sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


def scan(root: str) -> dict[str, bytes]:
    """All project files (generated files and symlinks excluded) -> content."""
    files = {}
    for d, dirs, names in os.walk(root):
        dirs[:] = [x for x in dirs if x not in GENERATED_DIRS]
        for n in names:
            p = os.path.join(d, n)
            if n.endswith(GENERATED_SUFFIXES) or os.path.islink(p):
                continue
            files[os.path.relpath(p, root)] = Path(p).read_bytes()
    return files


class Journal:
    def __init__(self, state: dict, store, workspace: str):
        self.state, self.store, self.workspace = state, store, workspace

    @property
    def j(self) -> dict:
        return self.state["journal"]

    def init(self) -> None:
        files = scan(self.workspace)
        self.state["journal"] = {"snapshots": 0, "last_snapshot": None, "last": {}, "files": {}, "actions": [],
                                 "rolled_back": False}
        self.j["last_snapshot"] = self._snapshot(files)
        self.j["last"] = {p: sha(b) for p, b in files.items()}

    def _snapshot(self, files: dict[str, bytes]) -> int:
        n = self.j["snapshots"]
        self.store.put_blob(self.state["run_id"], f"snapshots/{n}.json",
                            {p: base64.b64encode(b).decode() for p, b in files.items()})
        self.j["snapshots"] = n + 1
        return n

    def _content(self, snapshot: int, path: str) -> bytes | None:
        data = self.store.get_blob(self.state["run_id"], f"snapshots/{snapshot}.json").get(path)
        return None if data is None else base64.b64decode(data)

    def changes(self) -> tuple[dict[str, bytes], list[str]]:
        cur = scan(self.workspace)
        hashes = {p: sha(b) for p, b in cur.items()}
        last = self.j["last"]
        return cur, sorted(p for p in set(last) | set(hashes) if last.get(p) != hashes.get(p))

    def conflict(self) -> list[str]:
        """Paths changed since the last snapshot (i.e. by someone other than the agent)."""
        return self.changes()[1]

    def record(self, call_id: str) -> list[str]:
        """After an action: store the new snapshot, diff and hashes. Returns changed paths."""
        cur, changed = self.changes()
        if not changed:
            return []
        pre = self.j["last_snapshot"]
        post = self._snapshot(cur)
        diff = []
        for p in changed:
            entry = self.j["files"].setdefault(p, {"original_hash": self.j["last"].get(p), "original_snapshot": pre})
            entry["final_hash"] = sha(cur.get(p))
            entry["final_snapshot"] = post
            diff.append(self._diff(p, self._content(pre, p), cur.get(p)))
        self.j["actions"].append({"call_id": call_id, "pre_snapshot": pre, "post_snapshot": post,
                                  "paths": changed, "diff": "".join(diff)})
        self.j["last"] = {p: sha(b) for p, b in cur.items()}
        self.j["last_snapshot"] = post
        return changed

    @staticmethod
    def _diff(path: str, old: bytes | None, new: bytes | None) -> str:
        a = [] if old is None else old.decode(errors="replace").splitlines(keepends=True)
        b = [] if new is None else new.decode(errors="replace").splitlines(keepends=True)
        return "".join(difflib.unified_diff(a, b, "/dev/null" if old is None else f"a/{path}",
                                            "/dev/null" if new is None else f"b/{path}"))

    def diff(self) -> str:
        """Original (before first change) vs final (after last change) for every file the run touched."""
        out = []
        for p, e in sorted(self.j["files"].items()):
            if e["original_hash"] == e["final_hash"]:
                continue
            out.append(self._diff(p, self._content(e["original_snapshot"], p), self._content(e["final_snapshot"], p)))
        return "".join(out)

    def rollback(self, policy) -> list[str]:
        """Restore only this run's files. All-or-nothing: any external edit aborts before writing."""
        problems, plan = [], []
        for p, e in sorted(self.j["files"].items()):
            try:
                real = policy.resolve(p)
            except PolicyError as err:
                problems.append(f"{p}: {err}")
                continue
            current = Path(real).read_bytes() if os.path.isfile(real) else None
            if sha(current) != e["final_hash"]:
                problems.append(f"{p}: changed outside the agent after its last action")
            plan.append((real, self._content(e["original_snapshot"], p)))
        if problems:
            raise ConflictError("rollback aborted, nothing was written:\n  " + "\n  ".join(problems))
        for real, original in plan:
            if original is None:
                if os.path.exists(real):
                    os.remove(real)
            else:
                os.makedirs(os.path.dirname(real), exist_ok=True)
                Path(real).write_bytes(original)
        cur = scan(self.workspace)
        self.j["last_snapshot"] = self._snapshot(cur)
        self.j["last"] = {p: sha(b) for p, b in cur.items()}
        self.j["rolled_back"] = True
        return [p for p, _ in sorted(self.j["files"].items())]
