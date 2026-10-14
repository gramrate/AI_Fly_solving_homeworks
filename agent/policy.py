"""Security policy: decides allow / deny / confirm for every tool call, independent of the agent loop."""
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .tools import TOOLS, validate

# Any of these means shell syntax beyond a simple command (pipes, redirects, substitution, globs, quoting...).
SHELL_META = set("|&;<>()$`\\\"'*?[]{}~!#=\n\r\t")
MODES = ("safe", "auto")


class PolicyError(Exception):
    pass


@dataclass
class Decision:
    action: str               # allow | deny | confirm
    reason: str = ""
    paths: dict = field(default_factory=dict)


def inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root + os.sep)


class Policy:
    def __init__(self, path: str):
        self.path = os.path.realpath(path)
        data = yaml.safe_load(Path(self.path).read_text()) or {}
        base = os.path.dirname(self.path)
        self.workspace = os.path.realpath(os.path.join(base, data["workspace"]))
        if inside(self.path, self.workspace):
            raise ValueError("policy file must live outside the workspace")
        commands, dirs = data.get("commands", {}), data.get("directories", {})
        self.allow_cmds = [" ".join(c.split()) for c in commands.get("allow", [])]
        self.deny_cmds = [c.split() for c in commands.get("deny", [])]
        self.allow_dirs = [self._dir(d) for d in dirs.get("allow", [])]
        self.deny_dirs = [self._dir(d) for d in dirs.get("deny", [])]

    def _dir(self, d: str) -> str:
        real = os.path.realpath(os.path.join(self.workspace, d))
        if not inside(real, self.workspace):
            raise ValueError(f"policy directory {d!r} is outside the workspace")
        return real

    def allowed_dir(self, real: str) -> bool:
        return (not any(inside(real, d) for d in self.deny_dirs)
                and any(inside(real, d) for d in self.allow_dirs))

    def resolve(self, rel: str) -> str:
        """Canonical absolute path for a workspace-relative path, or PolicyError."""
        if not rel:
            raise PolicyError("empty path")
        if os.path.isabs(rel):
            raise PolicyError(f"absolute paths are not allowed: {rel}")
        if ".." in Path(rel).parts:
            raise PolicyError(f"'..' is not allowed in paths: {rel}")
        candidate = os.path.normpath(os.path.join(self.workspace, rel))
        real = os.path.realpath(candidate)
        if real != candidate:
            raise PolicyError(f"path goes through a symlink: {rel}")
        if real == self.path:
            raise PolicyError("the policy file is not accessible")
        if not self.allowed_dir(real):
            raise PolicyError(f"directory not allowed by policy: {rel}")
        return real

    def check_command(self, command: str, cwd: str) -> str:
        bad = sorted(set(command) & SHELL_META)
        if bad:
            raise PolicyError(f"shell syntax is not allowed (found {''.join(bad)!r}); use one simple command")
        tokens = shlex.split(command)
        if not tokens:
            raise PolicyError("empty command")
        for deny in self.deny_cmds:
            if tokens[:len(deny)] == deny:
                raise PolicyError(f"command denied by policy rule {' '.join(deny)!r}")
        if " ".join(tokens) not in self.allow_cmds:
            raise PolicyError(f"command not in allow list: {' '.join(tokens)!r}; allowed: {self.allow_cmds}")
        real_cwd = self.resolve(cwd)
        if not os.path.isdir(real_cwd):
            raise PolicyError(f"cwd is not a directory: {cwd}")
        return real_cwd

    def check(self, name: str, args, mode: str) -> Decision:
        """Re-run before every execution: schema, command, canonical paths, cwd and mode."""
        tool = TOOLS.get(name)
        if tool is None:
            return Decision("deny", f"unknown tool {name!r}; available: {sorted(TOOLS)}")
        if mode not in MODES:
            return Decision("deny", f"unknown mode {mode!r}")
        try:
            validate(tool.params, args)
            paths = {}
            if name == "bash":
                paths["cwd"] = self.check_command(args["command"], args.get("cwd", "."))
            elif "path" in tool.params["properties"]:
                paths["path"] = self.resolve(args.get("path", "."))
        except (PolicyError, ValueError) as e:
            return Decision("deny", str(e))
        if tool.kind == "delete" or (mode == "safe" and tool.kind in ("write", "bash")):
            return Decision("confirm", f"{tool.kind} in {mode} mode needs user confirmation", paths)
        return Decision("allow", "", paths)
