"""Agent runtime: the loop between the model and the tools.

The model only *proposes* tool calls. Before every execution the runtime re-checks the policy, the mode,
the budget and the workspace journal, and persists state so the run can be resumed by run_id.

Run status: running | awaiting_confirmation | awaiting_plan_approval | awaiting_resolution
            | done | failed | budget_exhausted | plan_rejected
Call status: pending | awaiting_confirmation | started | uncertain | done | error | denied | failed | rejected
"""
import json
import os
import uuid
from datetime import datetime

from .journal import Journal
from .tools import TOOLS, Executor

FINAL = {"done", "error", "denied", "failed", "rejected"}
BUDGET_KEYS = ("model_requests", "tool_calls", "side_effects", "output_chars")

SYSTEM = """You are a coding agent. You act only through the provided tools inside a project workspace.
All paths are relative to the workspace root. The bash tool accepts only these exact commands: {commands}.
Start with list_files to discover the project; never guess file names. Read files before changing them. Apply every change with write_file: never paste code into your reply
instead of calling a tool. After a change, run the tests with bash. Do not modify tests.
The runtime may deny an action and tell you why; adapt instead of repeating it.
When the work is finished and verified, reply with a short final answer and no tool calls."""

PLAN = """PLAN MODE. Until the user approves a plan you may only use list_files and read_file.
Study the project, then call submit_plan once with: goal, steps (in order), risks, assumptions, done_criteria.
Each step: id (e.g. "t1"), description, tools it needs, depends_on (ids of earlier steps) and
verify = {{"kind": one of {kinds}, "target": command or path}}. Prefer verify kinds that prove the result,
e.g. {{"kind": "command_succeeded", "target": "python3 -m unittest -v"}}."""

TASK = """Plan approved. Current task {id}: {description}
Verification required: {verify}. Done tasks: {done}.
Work only on this task. When it is done and verified, call complete_task with task_id="{id}"."""


class SimulatedCrash(BaseException):
    """Raised at a configured fault point to emulate a process crash (tests / AGENT_FAULT)."""


class UserError(Exception):
    pass


def new_run_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def norm_cmd(cmd: str) -> str:
    return " ".join(cmd.split())


class Agent:
    def __init__(self, state, *, llm, store, policy, cfg, fault=None, log=print):
        self.state, self.llm, self.store, self.policy, self.cfg = state, llm, store, policy, cfg
        self.fault = fault if fault is not None else os.environ.get("AGENT_FAULT")
        self.log = log
        self.executor = Executor(policy, cfg["limits"]["bash_timeout"])
        self.journal = Journal(state, store, policy.workspace)

    # ---------- lifecycle ----------

    @classmethod
    def create(cls, task: str, *, mode: str, plan_mode: bool, **deps) -> "Agent":
        budget = deps["cfg"]["budget"]
        state = {
            "run_id": new_run_id(), "task": task, "mode": mode, "status": "running",
            "phase": "plan" if plan_mode else "execute",
            "messages": [{"role": "user", "content": task}],
            "calls": {}, "pending": [], "plan": None, "plan_status": None, "tasks": [],
            "budget": {"limits": {k: budget[k] for k in BUDGET_KEYS}, "used": {k: 0 for k in BUDGET_KEYS}},
            "decisions": [], "final": None, "error": None,
        }
        agent = cls(state, **deps)
        agent.journal.init()
        agent.save()
        return agent

    @classmethod
    def load(cls, run_id: str, **deps) -> "Agent":
        state = deps["store"].load(run_id)
        agent = cls(state, **deps)
        # A side effect that was started but never got its result saved: outcome unknown, never re-run silently.
        stale = [c for c in state["calls"].values() if c["status"] == "started"]
        for c in stale:
            c["status"] = "uncertain"
        if stale:
            state["status"] = "awaiting_resolution"
            agent.save()
        return agent

    def save(self) -> None:
        self.store.save(self.state)

    def run(self) -> str:
        s = self.state
        while s["status"] == "running":
            if s["pending"]:
                if not self._process_pending():
                    break
            elif s["plan_status"] == "pending":
                s["status"] = "awaiting_plan_approval"
            else:
                self._model_turn()
        self.save()
        return s["status"]

    def _stop(self, status: str, error: str | None = None) -> None:
        self.state["status"], self.state["error"] = status, error
        self.log(f"[stop] {status}" + (f": {error}" if error else ""))

    def _fault(self, point: str) -> None:
        if self.fault == point:
            raise SimulatedCrash(point)

    # ---------- budget ----------

    def remaining(self, key: str) -> int:
        b = self.state["budget"]
        return b["limits"][key] - b["used"][key]

    def charge(self, key: str, n: int = 1) -> None:
        self.state["budget"]["used"][key] += n

    # ---------- model ----------

    def _model_turn(self) -> None:
        s = self.state
        if self.remaining("model_requests") <= 0:
            return self._stop("budget_exhausted", "model_requests limit reached")
        self.charge("model_requests")
        self.save()  # charged before the request: a crash during it does not refund
        msg = self._clean(self.llm.complete(self._context(), [t.spec() for t in TOOLS.values()]))
        s["messages"].append(msg)
        if msg["content"]:
            self.log(f"[model] {msg['content'][:500]}")
        if msg.get("tool_calls"):
            self._accept_batch(msg["tool_calls"])
        else:
            self._on_text(msg["content"])
        self.save()

    def _clean(self, msg: dict) -> dict:
        out = {"role": "assistant", "content": msg.get("content") or ""}
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function") or {}
            args = fn.get("arguments", "")
            calls.append({"id": tc.get("id") or f"call_{uuid.uuid4().hex[:8]}", "type": "function",
                          "function": {"name": fn.get("name", ""),
                                       "arguments": args if isinstance(args, str) else json.dumps(args)}})
        if calls:
            out["tool_calls"] = calls
        return out

    def _context(self) -> list[dict]:
        s = self.state
        system = SYSTEM.format(commands=self.policy.allow_cmds)
        if s["phase"] == "plan":
            system += "\n\n" + PLAN.format(kinds=" | ".join(["command_succeeded", "file_changed", "file_read",
                                                              "any_tool"]))
        msgs = [{"role": "system", "content": system}] + s["messages"]
        task = self._current_task() if s["phase"] == "execute" else None
        if task:
            done = [t["id"] for t in s["tasks"] if t["status"] == "done"] or "none"
            msgs.append({"role": "system", "content": TASK.format(
                id=task["id"], description=task["description"], verify=task["verify"], done=done)})
        return msgs

    def _on_text(self, content: str) -> None:
        s = self.state
        if s["phase"] == "plan":
            nudge = "You are in plan mode: explore with read tools and then call submit_plan."
        elif s["tasks"] and self._current_task():
            nudge = f"Task {self._current_task()['id']} is not verified yet. Continue it or call complete_task."
        elif not self._verified() and s.setdefault("finish_nudges", 0) < self.cfg.get("max_finish_nudges", 2):
            s["finish_nudges"] += 1
            nudge = ("The runtime has not seen a successful bash run of the tests after your last file change. "
                     "If the task requires changing code: do not ask the user for files, find them with list_files, "
                     "apply the change with write_file (not in text), then run the tests with bash. "
                     "If the task needs no file changes or you refuse it, do not change any files: "
                     "just repeat your final answer.")
        else:
            s["status"], s["final"] = "done", content
            return
        s["messages"].append({"role": "user", "content": nudge})

    def _verified(self) -> bool:
        """Final answer gate: a successful bash call after the last file change (off unless configured)."""
        if not self.cfg.get("require_verification"):
            return True
        verified = False
        for c in self.state["calls"].values():  # insertion order == execution order
            if c["changes"]:
                verified = False
            if c["name"] == "bash" and c["ok"]:
                verified = True
        return verified

    # ---------- tool calls ----------

    def _accept_batch(self, tool_calls: list[dict]) -> None:
        s = self.state
        fresh = []
        for tc in tool_calls:
            cid, name, raw = tc["id"], tc["function"]["name"], tc["function"]["arguments"]
            try:
                args = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                args = None
            old = s["calls"].get(cid)
            if old:
                if old["name"] == name and old["args"] == args and old["status"] in FINAL:
                    self._tool_msg(cid, old["result"])  # replay: same call already executed, never run twice
                else:
                    self._tool_msg(cid, json.dumps({"error": "protocol_error", "detail":
                                   f"tool_call_id {cid} was already used with a different tool or arguments"}))
                continue
            s["calls"][cid] = {"id": cid, "name": name, "args": args, "raw_args": raw, "status": "pending",
                               "phase": s["phase"], "task_id": None, "decision": None, "reason": None,
                               "attempts": 0, "result": None, "ok": False, "changes": []}
            fresh.append(cid)
        if not fresh:
            return
        side = sum(1 for cid in fresh if s["phase"] == "execute" and s["calls"][cid]["name"] in TOOLS
                   and TOOLS[s["calls"][cid]["name"]].side_effect)
        short = [k for k, n in (("tool_calls", len(fresh)), ("side_effects", side), ("output_chars", 1))
                 if self.remaining(k) < n]
        if short:  # not enough for the whole batch: none of it runs
            for cid in fresh:
                self._finish(s["calls"][cid], "rejected", {"error": "budget_exhausted", "limits": short})
            return self._stop("budget_exhausted", f"not enough budget for batch of {len(fresh)}: {short}")
        self.charge("tool_calls", len(fresh))
        s["pending"] = fresh

    def _process_pending(self) -> bool:
        s = self.state
        for cid in s["pending"]:
            c = s["calls"][cid]
            if c["status"] not in FINAL and not self._handle(c):
                return False
        s["pending"] = []
        self.save()
        return True

    def _handle(self, c: dict) -> bool:
        """Run one call through the gates. Returns False when the run must wait for the user."""
        s = self.state
        tool = TOOLS.get(c["name"])
        if c["args"] is None:
            return self._finish(c, "error", {"error": f"arguments are not valid JSON: {c['raw_args'][:200]}"})
        if tool is None:
            return self._finish(c, "error", {"error": f"unknown tool {c['name']!r}", "available": sorted(TOOLS)})
        if s["phase"] == "plan" and tool.kind != "read" and tool.name != "submit_plan":
            return self._finish(c, "denied", {"error": "plan_mode", "reason":
                                "only list_files, read_file and submit_plan are allowed until the plan is approved"})
        if s["phase"] == "execute" and tool.name == "submit_plan":
            return self._finish(c, "error", {"error": "submit_plan is only available in plan mode"})
        if s["phase"] == "execute" and s["tasks"]:
            task = self._current_task()
            c["task_id"] = task and task["id"]
            if task:
                task["status"] = "in_progress"

        decision = self.policy.check(c["name"], c["args"], s["mode"])
        if decision.action == "deny":
            return self._finish(c, "denied", {"error": "denied_by_policy", "reason": decision.reason})
        if decision.action == "confirm":
            if c["decision"] is None:
                c["status"], s["status"] = "awaiting_confirmation", "awaiting_confirmation"
                self.log(f"[confirm] {c['name']} {c['raw_args'][:300]} ({decision.reason})")
                self.save()
                return False
            if c["decision"] == "deny":
                return self._finish(c, "denied", {"error": "denied_by_user", "reason": c["reason"] or "rejected"})

        if tool.kind == "control":
            return self._control(c)
        cap = min(self.cfg["limits"]["max_output"], self.remaining("output_chars"))
        if not tool.side_effect:
            return self._finish(c, "done", self.executor.execute(c["name"], c["args"], decision.paths, cap), True)

        conflict = self.journal.conflict()
        if conflict:
            return self._finish(c, "error", {"error": "workspace_conflict", "reason":
                                "files changed outside the agent since its last action", "paths": conflict})
        if self.remaining("side_effects") <= 0:
            return self._finish(c, "rejected", {"error": "budget_exhausted", "limits": ["side_effects"]})
        # Write-ahead record: budget charge and "started" status are saved together, atomically.
        c["status"] = "started"
        c["attempts"] += 1
        c["pre_snapshot"] = self.state["journal"]["last_snapshot"]
        self.charge("side_effects")
        self.save()
        self.log(f"[exec] {c['name']} {c['raw_args'][:300]}")
        result = self.executor.execute(c["name"], c["args"], decision.paths, cap)
        self._fault("after_execute")
        c["changes"] = sorted(set(c["changes"]) | set(self.journal.record(c["id"])))  # keep attributed changes on retry
        return self._finish(c, "done", result, True)

    def _finish(self, c: dict, status: str, result: dict, charge_output: bool = False) -> bool:
        text = json.dumps(result, ensure_ascii=False)
        if charge_output:
            avail = max(self.remaining("output_chars"), 0)
            if len(text) > avail:
                text = json.dumps({"truncated": True, "note": "output budget exhausted, result is incomplete",
                                   "partial": text[:avail]}, ensure_ascii=False)
                self.charge("output_chars", avail)
            else:
                self.charge("output_chars", len(text))
        c["status"], c["result"] = status, text
        c["ok"] = status == "done" and "error" not in result and result.get("exit_code", 0) == 0
        self._tool_msg(c["id"], text)
        self.log(f"[tool] {c['name']} -> {status}: {text[:300]}")
        self.save()
        return True

    def _tool_msg(self, cid: str, text: str) -> None:
        self.state["messages"].append({"role": "tool", "tool_call_id": cid, "content": text})

    # ---------- plan and tasks ----------

    def _control(self, c: dict) -> bool:
        s, args = self.state, c["args"]
        if c["name"] == "submit_plan":
            errors, seen = [], []
            for step in args["steps"]:
                if step["id"] in seen:
                    errors.append(f"duplicate step id {step['id']}")
                errors += [f"{step['id']}: depends_on {d!r} must be an earlier step"
                           for d in step.get("depends_on", []) if d not in seen]
                verify = step.get("verify") or {"kind": "any_tool"}
                if verify["kind"] != "any_tool" and not verify.get("target"):
                    errors.append(f"{step['id']}: verify kind {verify['kind']} needs a target")
                seen.append(step["id"])
            if errors:
                return self._finish(c, "error", {"error": "invalid_plan", "problems": errors})
            s["plan"], s["plan_status"] = args, "pending"
            s["tasks"] = [{"id": st["id"], "description": st["description"], "tools": st["tools"],
                           "depends_on": st.get("depends_on", []),
                           "verify": st.get("verify") or {"kind": "any_tool"},
                           "status": "pending", "result": None} for st in args["steps"]]
            return self._finish(c, "done", {"ok": True, "message": "plan saved, waiting for user approval"})

        task = self._current_task()
        if task is None:
            return self._finish(c, "error", {"error": "no open task"})
        if args["task_id"] != task["id"]:
            return self._finish(c, "error", {"error": f"current task is {task['id']}, not {args['task_id']}"})
        evidence = self._evidence(task)
        if evidence is None:
            return self._finish(c, "error", {"error": "verification_failed", "required": task["verify"],
                                             "hint": "perform the verifying action within this task first"})
        task["status"] = "done"
        task["result"] = {"summary": args.get("summary", ""), "evidence_call": evidence}
        nxt = self._current_task()
        return self._finish(c, "done", {"ok": True, "completed": task["id"],
                                        "next_task": nxt and {"id": nxt["id"], "description": nxt["description"]}})

    def _current_task(self) -> dict | None:
        done = {t["id"] for t in self.state["tasks"] if t["status"] == "done"}
        return next((t for t in self.state["tasks"]
                     if t["status"] != "done" and all(d in done for d in t["depends_on"])), None)

    def _evidence(self, task: dict) -> str | None:
        """Runtime-confirmed result: a successful call of this task that matches the step's verify rule."""
        kind, target = task["verify"]["kind"], task["verify"].get("target") or ""
        for c in self.state["calls"].values():
            if c["task_id"] != task["id"] or not c["ok"] or c["name"] == "complete_task":
                continue
            a = c["args"]
            if (kind == "any_tool"
                    or kind == "command_succeeded" and c["name"] == "bash"
                    and norm_cmd(a["command"]) == norm_cmd(target)
                    or kind == "file_changed" and os.path.normpath(target) in c["changes"]
                    or kind == "file_read" and c["name"] == "read_file"
                    and os.path.normpath(a["path"]) == os.path.normpath(target)):
                return c["id"]
        return None

    # ---------- user decisions ----------

    def _call_with(self, status: str) -> dict:
        found = [c for c in self.state["calls"].values() if c["status"] == status]
        if not found:
            raise UserError(f"no tool call is {status} (run status: {self.state['status']})")
        return found[0]

    def confirm(self, approve: bool, reason: str | None = None) -> None:
        c = self._call_with("awaiting_confirmation")
        c["decision"], c["reason"], c["status"] = ("approve" if approve else "deny"), reason, "pending"
        self.state["decisions"].append({"type": "confirm", "call": c["id"], "approve": approve, "reason": reason})
        self.state["status"] = "running"
        self.save()

    def decide_plan(self, approve: bool, reason: str | None = None) -> None:
        s = self.state
        if s["status"] != "awaiting_plan_approval":
            raise UserError(f"run is {s['status']}, not awaiting_plan_approval")
        s["decisions"].append({"type": "plan", "approve": approve, "reason": reason})
        if approve:
            s["plan_status"], s["phase"], s["status"] = "approved", "execute", "running"
            s["messages"].append({"role": "user", "content": "The plan is approved. Execute it task by task."})
        else:
            s["plan_status"] = "rejected"
            self._stop("plan_rejected", reason)
        self.save()

    def resolve(self, choice: str, attribute: bool | None = None) -> list[str]:
        """Settle a call left uncertain by a crash: retry | fail | abort."""
        s = self.state
        c = self._call_with("uncertain")
        changes = self.journal.conflict()
        if changes and attribute is None:
            raise UserError(f"workspace changed since the last snapshot: {changes}. "
                            "Decide whether these changes belong to this run (--attribute yes|no).")
        if choice == "retry" and not TOOLS[c["name"]].retryable:
            raise UserError(f"{c['name']} is not safe to retry; choose fail or abort")
        if changes and attribute:
            c["changes"] = sorted(set(c["changes"]) | set(self.journal.record(c["id"])))
        s["decisions"].append({"type": "resolve", "call": c["id"], "choice": choice,
                               "changes": changes, "attributed": attribute})
        if choice == "retry":  # new attempt of the same tool call; attempts is bumped when it starts
            c["status"], s["status"] = "pending", "running"
        elif choice == "fail":
            s["status"] = "running"
            self._finish(c, "failed", {"error": "uncertain_outcome",
                                       "reason": "process crashed during this call; user marked it as failed"})
        elif choice == "abort":
            c["status"] = "failed"
            self._stop("failed", f"aborted by user after uncertain call {c['id']}")
        else:
            raise UserError(f"unknown choice {choice!r}")
        self.save()
        return changes

    def rollback(self) -> list[str]:
        if any(c["status"] == "uncertain" for c in self.state["calls"].values()):
            raise UserError("resolve the uncertain tool call first (resolve retry|fail|abort)")
        restored = self.journal.rollback(self.policy)
        self.state["decisions"].append({"type": "rollback", "paths": restored})
        self.save()
        return restored
