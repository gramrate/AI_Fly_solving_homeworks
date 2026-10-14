"""Tool registry (schemas the model sees) and the executor that actually runs them.

The executor never decides *whether* to run: it receives paths already canonicalized by the policy.
"""
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Generated files ignored by listing and by the change journal. They never hide sources or tests.
GENERATED_DIRS = {"__pycache__", ".pytest_cache"}
GENERATED_SUFFIXES = (".pyc",)

VERIFY_KINDS = ["command_succeeded", "file_changed", "file_read", "any_tool"]


@dataclass(frozen=True)
class Tool:
    name: str
    kind: str          # read | write | delete | bash | control
    description: str
    params: dict
    retryable: bool    # safe to re-run after a crash left the call uncertain

    @property
    def side_effect(self) -> bool:
        return self.kind in ("write", "delete", "bash")

    def spec(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description, "parameters": self.params}}


def obj(props: dict, required: list) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


S = {"type": "string"}
STRS = {"type": "array", "items": S}
STEP = obj({
    "id": S,
    "description": S,
    "tools": STRS,
    "depends_on": STRS,
    "verify": obj({"kind": {"type": "string", "enum": VERIFY_KINDS}, "target": S}, ["kind"]),
}, ["id", "description", "tools"])

# Retry rationale: write_file writes the full content, so a second run gives the same file;
# delete_file either deletes or reports "not found" without touching anything else;
# bash runs an arbitrary (if allowed) program whose effects we cannot reason about, so no blind retry.
TOOLS = {t.name: t for t in [
    Tool("list_files", "read", "Recursively list files under a workspace directory (default: workspace root).",
         obj({"path": S}, []), True),
    Tool("read_file", "read", "Read a text file. Path is relative to the workspace root.",
         obj({"path": S}, ["path"]), True),
    Tool("write_file", "write", "Create or overwrite a file with the full new content.",
         obj({"path": S, "content": S}, ["path", "content"]), True),
    Tool("delete_file", "delete", "Delete a file in the workspace.",
         obj({"path": S}, ["path"]), True),
    Tool("bash", "bash", "Run one allowed shell command in a workspace directory (cwd relative to the workspace, "
         "default '.'). Returns stdout, stderr and exit_code.",
         obj({"command": S, "cwd": S}, ["command"]), False),
    Tool("submit_plan", "control", "Plan mode only: submit the structured plan for user approval.",
         obj({"goal": S, "steps": {"type": "array", "items": STEP, "minItems": 1},
              "risks": STRS, "assumptions": STRS, "done_criteria": S},
             ["goal", "steps", "risks", "assumptions", "done_criteria"]), True),
    Tool("complete_task", "control", "Mark the current task finished. The runtime checks its verification evidence.",
         obj({"task_id": S, "summary": S}, ["task_id"]), True),
]}

TYPES = {"object": dict, "string": str, "array": list, "boolean": bool, "integer": int}


def validate(schema: dict, value, where: str = "arguments") -> None:
    """Minimal JSON Schema check for the subset used above. Raises ValueError."""
    t = schema.get("type")
    if t and (not isinstance(value, TYPES[t]) or (t == "integer" and isinstance(value, bool))):
        raise ValueError(f"{where}: expected {t}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where}: must be one of {schema['enum']}")
    if t == "object":
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                raise ValueError(f"{where}: missing required field '{key}'")
        for key, item in value.items():
            if key in props:
                validate(props[key], item, f"{where}.{key}")
            elif schema.get("additionalProperties") is False:
                raise ValueError(f"{where}: unexpected field '{key}'")
    if t == "array":
        if len(value) < schema.get("minItems", 0):
            raise ValueError(f"{where}: needs at least {schema['minItems']} item(s)")
        for i, item in enumerate(value):
            validate(schema["items"], item, f"{where}[{i}]")


def clip(text: str, limit: int) -> tuple[str, bool]:
    return (text, False) if len(text) <= limit else (text[:limit], True)


class Executor:
    def __init__(self, policy, bash_timeout: int):
        self.policy = policy
        self.bash_timeout = bash_timeout

    def execute(self, name: str, args: dict, paths: dict, max_output: int) -> dict:
        try:
            return getattr(self, name)(args, paths, max_output)
        except OSError as e:
            return {"error": f"{type(e).__name__}: {e}"}

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.policy.workspace)

    def list_files(self, args, paths, max_output):
        root = paths["path"]
        if not os.path.isdir(root):
            return {"error": f"not a directory: {args.get('path', '.')}"}
        files = []
        for d, dirs, names in os.walk(root):
            dirs[:] = sorted(x for x in dirs
                             if x not in GENERATED_DIRS and self.policy.allowed_dir(os.path.join(d, x)))
            for n in sorted(names):
                if n.endswith(GENERATED_SUFFIXES):
                    continue
                p = os.path.join(d, n)
                files.append(self.rel(p) + (" (symlink, not accessible)" if os.path.islink(p) else ""))
        listing, truncated = clip("\n".join(files), max_output)
        return {"files": listing, "truncated": truncated}

    def read_file(self, args, paths, max_output):
        p = paths["path"]
        if not os.path.isfile(p):
            return {"error": f"file not found: {args['path']}"}
        content, truncated = clip(Path(p).read_text(errors="replace"), max_output)
        return {"path": args["path"], "content": content, "truncated": truncated}

    def write_file(self, args, paths, max_output):
        p = Path(paths["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(args["content"])
        return {"path": args["path"], "written_chars": len(args["content"])}

    def delete_file(self, args, paths, max_output):
        p = paths["path"]
        if not os.path.isfile(p):
            return {"error": f"file not found: {args['path']}"}
        os.remove(p)
        return {"path": args["path"], "deleted": True}

    def bash(self, args, paths, max_output):
        env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
        try:
            proc = subprocess.run(["bash", "-c", args["command"]], cwd=paths["cwd"], env=env,
                                  capture_output=True, text=True, errors="replace", timeout=self.bash_timeout)
        except subprocess.TimeoutExpired:
            return {"error": f"timeout after {self.bash_timeout}s", "exit_code": None}
        stdout, cut_out = clip(proc.stdout, max_output)
        stderr, cut_err = clip(proc.stderr, max_output - len(stdout))
        return {"exit_code": proc.returncode, "stdout": stdout, "stderr": stderr, "truncated": cut_out or cut_err}
