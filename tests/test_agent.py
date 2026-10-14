"""Deterministic runtime tests: the model is replaced by a scripted list of responses."""
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent.__main__ import interact
from agent.journal import ConflictError
from agent.policy import Policy
from agent.runtime import Agent, SimulatedCrash, UserError
from agent.store import Store

ROOT = Path(__file__).resolve().parent.parent
ORIGINAL = """def calculate_total(prices: list[float], discount_percent: float) -> float:
    return sum(prices) - discount_percent
"""
FIXED = """def calculate_total(prices: list[float], discount_percent: float) -> float:
    if not 0 <= discount_percent <= 100:
        raise ValueError("discount_percent must be between 0 and 100")
    return sum(prices) * (1 - discount_percent / 100)
"""
TESTS = "python3 -m unittest -v"
POLICY = """workspace: workspace
commands:
  allow: ["python3 -m unittest -v", "ls"]
  deny: ["rm", "python3 -c", "bash"]
directories:
  allow: ["."]
  deny: ["private"]
"""
BUDGET = {"model_requests": 30, "tool_calls": 60, "side_effects": 15, "output_chars": 200000}


def tc(cid, name, **args):
    return {"id": cid, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def calls(*tool_calls, content=""):
    return {"role": "assistant", "content": content, "tool_calls": list(tool_calls)}


def text(content):
    return {"role": "assistant", "content": content}


class ScriptedLLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def complete(self, messages, tools):
        self.requests.append(messages)
        if not self.responses:
            raise AssertionError("model called more times than scripted")
        return self.responses.pop(0)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.ws = self.tmp / "workspace"
        shutil.copytree(ROOT / "workspace", self.ws, ignore=shutil.ignore_patterns("__pycache__"))
        (self.ws / "order.py").write_text(ORIGINAL)  # independent of the agent having fixed the real workspace
        (self.ws / "private").mkdir()
        (self.ws / "private" / "notes.txt").write_text("secret")
        (self.tmp / "policy.yaml").write_text(POLICY)
        self.policy = Policy(str(self.tmp / "policy.yaml"))
        self.store = Store(str(self.tmp / "runs"))
        self.cfg = {"limits": {"bash_timeout": 30, "max_output": 8000}, "budget": dict(BUDGET)}
        self.order = self.ws / "order.py"
        self.test_hash = sha(self.ws / "test_order.py")

    def deps(self, llm, fault=None):
        return {"llm": llm, "store": self.store, "policy": self.policy, "cfg": self.cfg, "fault": fault,
                "log": lambda *_: None}

    def create(self, responses, mode="auto", plan=False, fault=None, task="fix order total"):
        self.llm = ScriptedLLM(responses)
        return Agent.create(task, mode=mode, plan_mode=plan, **self.deps(self.llm, fault))

    def reload(self, agent, responses=(), fault=None):
        """Simulates a process restart: fresh Agent from disk."""
        self.llm = ScriptedLLM(responses)
        return Agent.load(agent.state["run_id"], **self.deps(self.llm, fault))

    def result(self, agent, cid):
        return json.loads(agent.state["calls"][cid]["result"])


class LoopTests(Base):
    def test_control_task_fixed_and_verified_with_bash(self):
        agent = self.create([
            calls(tc("c1", "list_files")),
            calls(tc("c2", "read_file", path="order.py"), tc("c3", "read_file", path="test_order.py")),
            calls(tc("c4", "write_file", path="order.py", content=FIXED)),
            calls(tc("c5", "bash", command=TESTS, cwd=".")),
            text("Fixed calculate_total, tests pass."),
        ])
        self.assertEqual(agent.run(), "done")
        bash = self.result(agent, "c5")
        self.assertEqual(bash["exit_code"], 0, bash)
        self.assertIn("OK", bash["stderr"])
        self.assertEqual(sha(self.ws / "test_order.py"), self.test_hash)
        # tool results go back as role=tool with the original id
        tool_ids = [m["tool_call_id"] for m in agent.state["messages"] if m["role"] == "tool"]
        self.assertEqual(tool_ids, ["c1", "c2", "c3", "c4", "c5"])
        self.assertEqual(subprocess.run(["python3", "-m", "unittest"], cwd=self.ws, capture_output=True).returncode, 0)

    def test_tool_errors_do_not_crash_the_loop(self):
        agent = self.create([
            calls(tc("e1", "read_file", path="missing.py"), tc("e2", "nope"),
                  {"id": "e3", "type": "function", "function": {"name": "read_file", "arguments": "{bad"}},
                  tc("e4", "read_file", path="order.py", extra=1)),
            text("gave up"),
        ])
        self.assertEqual(agent.run(), "done")
        self.assertIn("file not found", self.result(agent, "e1")["error"])
        self.assertIn("unknown tool", self.result(agent, "e2")["error"])
        self.assertIn("not valid JSON", self.result(agent, "e3")["error"])
        self.assertIn("unexpected field", self.result(agent, "e4")["reason"])

    def test_final_answer_needs_verified_tests(self):
        self.cfg.update(require_verification=True, max_finish_nudges=2)
        agent = self.create([
            text("Change the return line to apply a percent discount."),       # code in text, nothing applied
            calls(tc("v1", "write_file", path="order.py", content=FIXED)),
            text("Done."),                                                     # changed but not tested
            calls(tc("v2", "bash", command=TESTS)),
            text("Fixed and tests pass."),
        ])
        self.assertEqual(agent.run(), "done")
        self.assertEqual(agent.state["finish_nudges"], 2)
        self.assertEqual(agent.state["final"], "Fixed and tests pass.")
        agent = self.create([text("a"), text("b"), text("c")])                 # gives up after max nudges
        self.assertEqual(agent.run(), "done")
        self.assertEqual(agent.state["final"], "c")


class PolicyTests(Base):
    def check(self, name, mode="auto", **args):
        return self.policy.check(name, args, mode)

    def test_commands(self):
        self.assertEqual(self.check("bash", command=TESTS).action, "allow")
        self.assertEqual(self.check("bash", command="  python3  -m unittest  -v ").action, "allow")
        self.assertIn("denied by policy rule 'rm'", self.check("bash", command="rm order.py").reason)
        self.assertIn("not in allow list", self.check("bash", command="python3 -m unittest").reason)
        for cmd in (f"{TESTS} | cat", f"{TESTS} > out", "ls; rm x", "echo $(id)", "bash -c 'ls'", "ls && ls"):
            self.assertEqual(self.check("bash", command=cmd).action, "deny", cmd)

    def test_allowed_command_in_denied_directory(self):
        d = self.check("bash", command="ls", cwd="private")
        self.assertEqual(d.action, "deny")
        self.assertIn("directory not allowed", d.reason)

    def test_file_paths(self):
        self.assertEqual(self.check("read_file", path="order.py").action, "allow")
        self.assertIn("directory not allowed", self.check("read_file", path="private/notes.txt").reason)
        self.assertIn("'..'", self.check("read_file", path="../policy.yaml").reason)
        self.assertIn("absolute", self.check("read_file", path=str(self.tmp / "policy.yaml")).reason)
        os.symlink(self.tmp / "policy.yaml", self.ws / "link.yaml")
        os.symlink(self.ws / "private", self.ws / "pub")
        self.assertIn("symlink", self.check("read_file", path="link.yaml").reason)
        self.assertIn("symlink", self.check("write_file", path="pub/notes.txt", content="x").reason)

    def test_modes(self):
        self.assertEqual(self.check("write_file", "safe", path="a.py", content="").action, "confirm")
        self.assertEqual(self.check("bash", "safe", command=TESTS).action, "confirm")
        self.assertEqual(self.check("write_file", "auto", path="a.py", content="").action, "allow")
        self.assertEqual(self.check("delete_file", "auto", path="order.py").action, "confirm")
        self.assertEqual(self.check("read_file", "safe", path="order.py").action, "allow")

    def test_policy_must_be_outside_workspace(self):
        (self.ws / "p.yaml").write_text("workspace: .\n")
        with self.assertRaises(ValueError):
            Policy(str(self.ws / "p.yaml"))

    def test_denied_call_reaches_model_with_reason(self):
        agent = self.create([calls(tc("d1", "read_file", path="private/notes.txt")), text("ok")])
        self.assertEqual(agent.run(), "done")
        self.assertEqual(agent.state["calls"]["d1"]["status"], "denied")
        self.assertIn("directory not allowed", self.result(agent, "d1")["reason"])


class ConfirmationTests(Base):
    def test_safe_mode_stops_and_resumes_after_approve(self):
        agent = self.create([calls(tc("w1", "write_file", path="order.py", content=FIXED))], mode="safe")
        self.assertEqual(agent.run(), "awaiting_confirmation")
        self.assertNotEqual(self.order.read_text(), FIXED)
        agent = self.reload(agent, [text("done")])
        agent.confirm(True)
        self.assertEqual(agent.run(), "done")
        self.assertEqual(self.order.read_text(), FIXED)

    def test_deny_returns_reason_to_model(self):
        agent = self.create([calls(tc("x1", "delete_file", path="order.py"))], mode="auto")
        self.assertEqual(agent.run(), "awaiting_confirmation")
        agent = self.reload(agent, [text("ok, not deleting")])
        agent.confirm(False, "keep the file")
        self.assertEqual(agent.run(), "done")
        self.assertTrue(self.order.exists())
        self.assertEqual(self.result(agent, "x1"), {"error": "denied_by_user", "reason": "keep the file"})


class InteractiveTests(Base):
    def test_terminal_loop_asks_until_done(self):
        agent = self.create([calls(tc("w1", "write_file", path="order.py", content="x = 1\n")),
                             calls(tc("w2", "write_file", path="order.py", content=FIXED)),
                             text("done")], mode="safe")
        agent.run()
        answers = iter(["maybe", "n", "wrong content", "y"])  # invalid answer is asked again
        with mock.patch("builtins.input", lambda _: next(answers)), mock.patch("builtins.print"):
            interact(agent)
        self.assertEqual(agent.state["status"], "done")
        self.assertEqual(self.result(agent, "w1"), {"error": "denied_by_user", "reason": "wrong content"})
        self.assertEqual(self.order.read_text(), FIXED)


class RecoveryTests(Base):
    def crash_write(self):
        agent = self.create([calls(tc("w1", "write_file", path="order.py", content=FIXED))],
                            fault="after_execute")
        with self.assertRaises(SimulatedCrash):
            agent.run()
        return agent

    def test_crash_between_execute_and_save_makes_call_uncertain(self):
        agent = self.reload(self.crash_write())
        self.assertEqual(agent.state["status"], "awaiting_resolution")
        self.assertEqual(agent.state["calls"]["w1"]["status"], "uncertain")
        self.assertEqual(agent.run(), "awaiting_resolution")  # never re-run silently
        with self.assertRaises(UserError):
            agent.resolve("retry")  # tree changed: user must decide attribution first
        with self.assertRaises(UserError):
            agent.rollback()

    def test_retry_is_new_attempt(self):
        agent = self.reload(self.crash_write(), [text("done")])
        self.assertEqual(agent.resolve("retry", attribute=True), ["order.py"])
        self.assertEqual(agent.run(), "done")
        c = agent.state["calls"]["w1"]
        self.assertEqual((c["status"], c["attempts"]), ("done", 2))
        self.assertEqual(agent.state["budget"]["used"]["side_effects"], 2)

    def test_fail_and_abort(self):
        agent = self.reload(self.crash_write(), [text("noted")])
        agent.resolve("fail", attribute=False)
        self.assertEqual(agent.run(), "done")
        self.assertEqual(self.result(agent, "w1")["error"], "uncertain_outcome")
        agent2 = self.create([calls(tc("b1", "bash", command=TESTS))], fault="after_execute")
        with self.assertRaises(SimulatedCrash):
            agent2.run()
        agent2 = self.reload(agent2)
        with self.assertRaises(UserError):
            agent2.resolve("retry")  # bash is not retryable
        agent2.resolve("abort")
        self.assertEqual(agent2.state["status"], "failed")

    def test_repeated_tool_call_id(self):
        agent = self.create([
            calls(tc("w1", "write_file", path="order.py", content=FIXED)),
            calls(tc("w1", "write_file", path="order.py", content=FIXED)),
            calls(tc("w1", "write_file", path="order.py", content="other")),
            text("done"),
        ])
        self.assertEqual(agent.run(), "done")
        self.assertEqual(agent.state["calls"]["w1"]["attempts"], 1)
        self.assertEqual(agent.state["budget"]["used"]["side_effects"], 1)
        self.assertEqual(self.order.read_text(), FIXED)
        tool_msgs = [json.loads(m["content"]) for m in agent.state["messages"] if m["role"] == "tool"]
        self.assertEqual(tool_msgs[0], tool_msgs[1])
        self.assertEqual(tool_msgs[2]["error"], "protocol_error")


PLAN = {
    "goal": "fix calculate_total so tests pass",
    "steps": [
        {"id": "t1", "description": "read code", "tools": ["read_file"], "depends_on": [],
         "verify": {"kind": "file_read", "target": "order.py"}},
        {"id": "t2", "description": "fix order.py", "tools": ["write_file"], "depends_on": ["t1"],
         "verify": {"kind": "file_changed", "target": "order.py"}},
        {"id": "t3", "description": "run tests", "tools": ["bash"], "depends_on": ["t2"],
         "verify": {"kind": "command_succeeded", "target": TESTS}},
    ],
    "risks": ["float rounding"], "assumptions": ["discount is a percent"], "done_criteria": "tests pass",
}


class PlanTests(Base):
    def planned(self, fault=None):
        return self.create([
            calls(tc("p1", "read_file", path="order.py"), tc("p2", "write_file", path="order.py", content="x"),
                  tc("p3", "bash", command=TESTS), tc("p4", "delete_file", path="order.py")),
            calls(tc("p5", "submit_plan", **PLAN)),
        ], plan=True, fault=fault)

    def test_no_side_effects_before_approval(self):
        before = self.order.read_text()
        agent = self.planned()
        self.assertEqual(agent.run(), "awaiting_plan_approval")
        s = agent.state
        self.assertEqual(self.order.read_text(), before)
        self.assertEqual(s["budget"]["used"]["side_effects"], 0)
        self.assertEqual([s["calls"][c]["status"] for c in ("p1", "p2", "p3", "p4")],
                         ["done", "denied", "denied", "denied"])
        self.assertEqual(s["journal"]["actions"], [])
        self.assertEqual([t["id"] for t in s["tasks"]], ["t1", "t2", "t3"])
        self.assertIsNone(s["calls"]["p1"]["task_id"])  # planning calls carry no task
        self.assertEqual(self.reload(agent).state["plan"]["goal"], PLAN["goal"])

    def test_reject_plan(self):
        agent = self.planned()
        agent.run()
        agent.decide_plan(False, "too risky")
        self.assertEqual(agent.state["status"], "plan_rejected")

    def test_tasks_with_dependencies_and_resume(self):
        agent = self.planned()
        agent.run()
        agent = self.reload(agent, [
            calls(tc("e1", "complete_task", task_id="t1")),         # no evidence yet -> rejected
            calls(tc("e2", "read_file", path="order.py")),
            calls(tc("e3", "complete_task", task_id="t1", summary="read")),
            calls(tc("e4", "write_file", path="order.py", content=FIXED)),
            calls(tc("e5", "complete_task", task_id="t3")),         # wrong task: t2 is current
            calls(tc("e6", "complete_task", task_id="t2")),
            calls(tc("e7", "bash", command=TESTS)),
        ], fault="after_execute")
        agent.decide_plan(True)
        agent.state["mode"] = "auto"
        with self.assertRaises(SimulatedCrash):  # crashes on the first side effect (e4)
            agent.run()
        agent = self.reload(agent, agent.llm.responses)  # same remaining script, no fault
        tasks = {t["id"]: t["status"] for t in agent.state["tasks"]}
        self.assertEqual(tasks, {"t1": "done", "t2": "in_progress", "t3": "pending"})
        agent.resolve("retry", attribute=True)
        agent.llm.responses += [calls(tc("e8", "complete_task", task_id="t3")), text("all done")]
        self.assertEqual(agent.run(), "done")
        s = agent.state
        self.assertEqual(self.result(agent, "e1")["error"], "verification_failed")
        self.assertIn("current task is t2", self.result(agent, "e5")["error"])
        self.assertEqual([t["status"] for t in s["tasks"]], ["done"] * 3)
        self.assertEqual([t["result"]["evidence_call"] for t in s["tasks"]], ["e2", "e4", "e7"])
        self.assertEqual([s["calls"][c]["task_id"] for c in ("e2", "e4", "e7")], ["t1", "t2", "t3"])
        self.assertEqual(s["calls"]["e2"]["attempts"], 0)  # read done before crash was not re-run

    def test_invalid_plan_dependencies(self):
        bad = dict(PLAN, steps=[dict(PLAN["steps"][0], depends_on=["t9"])])
        agent = self.create([calls(tc("p1", "submit_plan", **bad)), text("hm")], plan=True)
        agent.state["budget"]["limits"]["model_requests"] = 2
        self.assertEqual(agent.run(), "budget_exhausted")
        self.assertEqual(self.result(agent, "p1")["error"], "invalid_plan")


class BudgetTests(Base):
    def test_model_requests(self):
        self.cfg["budget"]["model_requests"] = 1
        agent = self.create([calls(tc("r1", "list_files"))])
        self.assertEqual(agent.run(), "budget_exhausted")
        self.assertIn("model_requests", agent.state["error"])

    def test_tool_calls_batch_all_or_nothing(self):
        self.cfg["budget"]["tool_calls"] = 2
        agent = self.create([calls(tc("a", "list_files"), tc("b", "read_file", path="order.py"),
                                   tc("c", "read_file", path="test_order.py"))])
        self.assertEqual(agent.run(), "budget_exhausted")
        self.assertEqual({c["status"] for c in agent.state["calls"].values()}, {"rejected"})
        self.assertEqual(agent.state["budget"]["used"]["tool_calls"], 0)

    def test_side_effects(self):
        self.cfg["budget"]["side_effects"] = 1
        agent = self.create([calls(tc("w1", "write_file", path="a.txt", content="1"),
                                   tc("w2", "write_file", path="b.txt", content="2"))])
        self.assertEqual(agent.run(), "budget_exhausted")
        self.assertFalse((self.ws / "a.txt").exists())

    def test_output_truncated_and_marked(self):
        self.cfg["budget"]["output_chars"] = 50
        agent = self.create([calls(tc("r1", "read_file", path="test_order.py")),
                             calls(tc("r2", "read_file", path="order.py"))])
        self.assertEqual(agent.run(), "budget_exhausted")
        r1 = self.result(agent, "r1")
        self.assertTrue(r1["truncated"])
        self.assertEqual(len(r1["partial"]), 50)
        self.assertEqual(agent.state["calls"]["r2"]["status"], "rejected")

    def test_counters_survive_resume_and_crash(self):
        agent = self.create([calls(tc("r1", "list_files")),
                             calls(tc("w1", "write_file", path="order.py", content=FIXED))],
                            mode="safe", fault="after_execute")
        agent.run()
        used = dict(agent.state["budget"]["used"])
        agent = self.reload(agent, fault="after_execute")
        self.assertEqual(agent.state["budget"]["used"], used)
        agent.confirm(True)
        with self.assertRaises(SimulatedCrash):  # charged, executed, crashed before saving the result
            agent.run()
        agent = self.reload(agent, [text("done")])
        self.assertEqual(agent.state["budget"]["used"]["side_effects"], 1)  # not refunded
        self.assertEqual(agent.state["budget"]["used"]["model_requests"], 2)
        agent.resolve("fail", attribute=True)
        agent.run()
        self.assertEqual(agent.state["budget"]["used"]["model_requests"], 3)


class JournalTests(Base):
    def fixed_run(self):
        agent = self.create([calls(tc("w1", "write_file", path="order.py", content=FIXED)),
                             calls(tc("w2", "write_file", path="new.py", content="x = 1\n")), text("done")])
        self.assertEqual(agent.run(), "done")
        return agent

    def test_diff_and_rollback_after_restart(self):
        original = self.order.read_text()
        agent = self.fixed_run()
        diff = agent.journal.diff()
        self.assertIn("-    return sum(prices) - discount_percent", diff)
        self.assertIn("+        raise ValueError", diff)
        self.assertIn("+++ b/new.py", diff)
        (self.ws / "untouched.txt").write_text("not ours")
        agent = self.reload(agent)
        self.assertEqual(sorted(agent.rollback()), ["new.py", "order.py"])
        self.assertEqual(self.order.read_text(), original)
        self.assertFalse((self.ws / "new.py").exists())
        self.assertEqual((self.ws / "untouched.txt").read_text(), "not ours")

    def test_rollback_conflict_writes_nothing(self):
        agent = self.fixed_run()
        (self.ws / "new.py").write_text("edited by human\n")
        with self.assertRaises(ConflictError):
            self.reload(agent).rollback()
        self.assertEqual(self.order.read_text(), FIXED)
        self.assertEqual((self.ws / "new.py").read_text(), "edited by human\n")

    def test_external_change_blocks_next_action(self):
        agent = self.create([calls(tc("w1", "write_file", path="order.py", content=FIXED))], mode="safe")
        agent.run()
        self.order.write_text("# human edit\n")
        agent = self.reload(agent, [text("stopped")])
        agent.confirm(True)
        agent.run()
        self.assertEqual(self.result(agent, "w1")["error"], "workspace_conflict")
        self.assertEqual(self.order.read_text(), "# human edit\n")


if __name__ == "__main__":
    unittest.main()
