"""CLI: python -m agent <command> ..."""
import argparse
import json
import sys

from .config import load_config
from .llm import ChatClient, LLMError
from .policy import Policy
from .runtime import Agent, SimulatedCrash, UserError
from .journal import ConflictError
from .store import Store


def deps(cfg: dict) -> dict:
    return {"llm": ChatClient(cfg["llm"]), "store": Store(cfg["runs_dir"]), "policy": Policy(cfg["policy"]),
            "cfg": cfg}


def report(agent: Agent) -> None:
    s = agent.state
    print(f"\nrun_id: {s['run_id']}  status: {s['status']}  mode: {s['mode']}  phase: {s['phase']}")
    used, limits = s["budget"]["used"], s["budget"]["limits"]
    print("budget: " + ", ".join(f"{k} {used[k]}/{limits[k]}" for k in limits))
    rid = s["run_id"]
    if s["status"] == "awaiting_confirmation":
        c = next(c for c in s["calls"].values() if c["status"] == "awaiting_confirmation")
        print(f"\nwaiting for confirmation: {c['name']} {c['raw_args']}")
        print(f"  approve: python -m agent approve {rid}\n  deny:    python -m agent deny {rid} --reason '...'")
    elif s["status"] == "awaiting_plan_approval":
        print("\nplan:\n" + json.dumps(s["plan"], ensure_ascii=False, indent=2))
        print(f"  approve: python -m agent plan-approve {rid}\n  reject:  python -m agent plan-reject {rid}")
    elif s["status"] == "awaiting_resolution":
        c = next(c for c in s["calls"].values() if c["status"] == "uncertain")
        print(f"\nuncertain call after crash: {c['name']} {c['raw_args']} (attempts: {c['attempts']})")
        changes = agent.journal.conflict()
        if changes:
            print(f"  workspace changed since last snapshot: {changes}")
        print(f"  python -m agent resolve {rid} retry|fail|abort [--attribute yes|no]")
    elif s["status"] == "done":
        print(f"\nfinal answer:\n{s['final']}")
    elif s["error"]:
        print(f"error: {s['error']}")


def show_tasks(s: dict) -> None:
    if not s["tasks"]:
        print("no tasks (run was started without --plan or plan not submitted yet)")
    for t in s["tasks"]:
        deps_ = ",".join(t["depends_on"]) or "-"
        print(f"{t['id']:>6}  {t['status']:<12} deps={deps_:<10} verify={t['verify']}  {t['description']}")
        if t["result"]:
            print(f"        result: {t['result']}")


WAITING = ("awaiting_confirmation", "awaiting_plan_approval", "awaiting_resolution")


def ask(prompt: str, choices: tuple) -> str:
    while True:
        answer = input(prompt).strip().lower()
        if answer in choices:
            return answer
        print("answer one of: " + "/".join(choices))


def interact(agent: Agent) -> None:
    """Chat-style loop: ask the user in the terminal at every pause until the run finishes."""
    s = agent.state
    while s["status"] in WAITING:
        report(agent)
        try:
            if s["status"] == "awaiting_confirmation":
                if ask("approve? [y/n] ", ("y", "n")) == "y":
                    agent.confirm(True)
                else:
                    agent.confirm(False, input("reason: ").strip() or None)
            elif s["status"] == "awaiting_plan_approval":
                if ask("approve plan? [y/n] ", ("y", "n")) == "y":
                    agent.decide_plan(True)
                else:
                    agent.decide_plan(False, input("reason: ").strip() or None)
            else:
                choice = ask("retry/fail/abort? ", ("retry", "fail", "abort"))
                attribute = None
                if agent.journal.conflict():
                    attribute = ask("do these changes belong to this run? [y/n] ", ("y", "n")) == "y"
                agent.resolve(choice, attribute)
        except UserError as e:
            print(f"error: {e}")
            continue
        agent.run()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="python -m agent", description="Coding agent with tool calling")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="start a new run")
    r.add_argument("task")
    r.add_argument("--mode", choices=["safe", "auto"])
    r.add_argument("--plan", action="store_true", help="start in plan mode")
    for name in ("resume", "approve", "deny", "plan-approve", "plan-reject", "status", "plan", "tasks", "diff",
                 "rollback", "resolve"):
        sp = sub.add_parser(name)
        sp.add_argument("run_id")
        if name in ("deny", "plan-reject"):
            sp.add_argument("--reason")
        if name == "resolve":
            sp.add_argument("choice", choices=["retry", "fail", "abort"])
            sp.add_argument("--attribute", choices=["yes", "no"],
                            help="whether workspace changes found after the crash belong to this run")
    sub.add_parser("list")
    sub.add_parser("models", help="list model ids available at llm.base_url")
    a = p.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # live progress even when piped

    cfg = load_config(a.config)
    d = deps(cfg)
    if a.cmd == "models":
        try:
            print("\n".join(d["llm"].models()))
        except LLMError as e:
            print(f"model error: {e}", file=sys.stderr)
            return 2
        return 0
    if a.cmd == "list":
        for rid in d["store"].runs():
            s = d["store"].load(rid)
            print(f"{rid}  {s['status']:<24} {s['task'][:60]}")
        return 0
    try:
        if a.cmd == "run":
            agent = Agent.create(a.task, mode=a.mode or cfg["mode"], plan_mode=a.plan, **d)
            print(f"run_id: {agent.state['run_id']}")
        else:
            agent = Agent.load(a.run_id, **d)
        s = agent.state
        if a.cmd == "status":
            return report(agent) or 0
        if a.cmd == "plan":
            print(json.dumps({"plan": s["plan"], "plan_status": s["plan_status"]}, ensure_ascii=False, indent=2))
            return 0
        if a.cmd == "tasks":
            return show_tasks(s) or 0
        if a.cmd == "diff":
            print(agent.journal.diff() or "no changes recorded for this run")
            return 0
        if a.cmd == "rollback":
            print("restored: " + ", ".join(agent.rollback()))
            return 0
        if a.cmd in ("approve", "deny"):
            agent.confirm(a.cmd == "approve", getattr(a, "reason", None))
        elif a.cmd in ("plan-approve", "plan-reject"):
            agent.decide_plan(a.cmd == "plan-approve", getattr(a, "reason", None))
        elif a.cmd == "resolve":
            agent.resolve(a.choice, None if a.attribute is None else a.attribute == "yes")
        agent.run()
        if sys.stdin.isatty():
            try:
                interact(agent)
            except (EOFError, KeyboardInterrupt):
                print(f"\npaused, state saved; continue with: python -m agent resume {agent.state['run_id']}")
        report(agent)
    except (UserError, ConflictError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except LLMError as e:
        print(f"model error: {e}\nstate saved; continue with: python -m agent resume {agent.state['run_id']}",
              file=sys.stderr)
        return 2
    except SimulatedCrash as e:
        print(f"simulated crash at {e}; continue with: python -m agent resume {agent.state['run_id']}",
              file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
