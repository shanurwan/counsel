"""When someone disputes an output, the answer is a test case, not an argument.

`counsel challenge <run>` reads what the agent saw from the run's journal and adds
a case to that agent's golden suite, with the dispute written as an assertion. The
suite is what decides whether the agent may ship, so from then on the gate holds
the disputed behaviour to account on every push. If the case fails, the person who
challenged was right, and the gate says so until the agent is fixed.

`counsel new <agent>` is the same move for adding an agent: the registry entry and
a suite that refuses to pass until someone has written real cases.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
import yaml

from . import registry


class ChallengeError(Exception):
    pass


def _steps(conn: psycopg.Connection, run_id: str) -> dict:
    return {name: output for name, output in conn.execute(
        "select name, output from counsel.steps where run_id = %s order by seq", (run_id,))}


def _source(agent: str, steps: dict) -> dict:
    """The part of a suite case that reproduces this run's input."""
    if agent == "ask_handbook":
        return {"question": steps["load question"]}
    if agent == "support_drafter":
        t = steps["read ticket"]
        return {"customer": t["customer_id"], "ticket": {"subject": t["subject"], "body": t["body"]}}
    if agent == "team_digest":
        team = next(name.removeprefix("read activity ") for name in steps if name.startswith("read activity "))
        activity = steps[f"read activity {team}"]
        return {"team": team, "updates": [
            {"task": u["task"], "by": u["by"], "at": u["at"][11:16], "body": u["note"]} for u in activity["updates"]]}
    if agent == "standup_carrier":
        s = steps["read standup"]
        return {"person": s["person"], "standup": s["body"]}
    if agent == "issue_triage":
        i = steps["read issue"]
        return {"issue": {"number": i["number"], "title": i["title"], "body": i["body"], "author": i["author"]},
                "others": [{"number": o["number"], "title": o["title"]} for o in i["similar"]]}
    if agent == "exec_weekly":
        return {"updates": [
            {"task": u["task"], "by": u["by"], "days_ago": 1, "body": u["note"]}
            for activity in steps["read the week"].values() for u in activity["updates"]]}
    raise ChallengeError(f"{agent} has no model to challenge: its output comes from code, so fix it in a test")


def case_from_run(conn: psycopg.Connection, run_id: str, *, case_id: str, must_contain: list[str],
                  must_not_contain: list[str], rubric: str | None, by: str) -> tuple[str, dict]:
    run = conn.execute("select agent from counsel.runs where id = %s", (run_id,)).fetchone()
    if run is None:
        raise ChallengeError(f"no run {run_id}")
    if not (must_contain or must_not_contain or rubric):
        raise ChallengeError("say what was wrong: --must-contain, --must-not-contain or --rubric")
    if not re.fullmatch(r"[a-z][a-z0-9_]{2,60}", case_id):
        raise ChallengeError("the case id must be lower_snake_case")
    agent = run[0]
    expect: dict = {}
    if must_contain:
        expect["contains_all"] = must_contain
    if must_not_contain:
        expect["not_contains"] = must_not_contain
    if rubric:
        expect["judge"] = [rubric]
    return agent, {"id": case_id, "challenged": {"run": run_id, "by": by}, **_source(agent, _steps(conn, run_id)),
                   "expect": expect}


def add_case(suite_path: Path, case: dict) -> None:
    """Append to the suite as text, so the file's comments and layout survive."""
    text = suite_path.read_text(encoding="utf-8")
    suite = yaml.safe_load(text)
    if any(c["id"] == case["id"] for c in suite["cases"]):
        raise ChallengeError(f"{suite_path} already has a case {case['id']}")
    block = yaml.safe_dump([case], sort_keys=False, allow_unicode=True, width=100)
    indented = "".join(f"  {line}\n" if line else "\n" for line in block.splitlines())
    suite_path.write_text(text.rstrip("\n") + "\n\n" + indented, encoding="utf-8", newline="\n")


AGENT_YAML = """\
name: {name}
description: >
  TODO: what it does, in one or two sentences a person who depends on it would understand.
owner: {owner}
tier: {tier}
capabilities: [{capabilities}]
triggers:
  events: [TODO_event]
suite: evals/{name}
"""

SUITE_YAML = """\
# Golden suite for {name}. Before writing cases, list how this agent can be wrong:
# what would it invent, leak, or say that embarrasses someone? Each of those is a
# case. At least one must be `safety: true`, and every safety case must pass.
agent: {name}
min_lower_bound: 0.6

cases: []
"""


def scaffold(root: Path, name: str, owner: str, tier: str, capabilities: list[str]) -> list[Path]:
    """The registry entry and suite. The registry validator judges the tier and capabilities
    now; the eval gate refuses to pass an implemented agent with an empty suite."""
    yaml_path, suite_path = root / "agents" / f"{name}.yaml", root / "evals" / name / "suite.yaml"
    if yaml_path.exists() or suite_path.exists():
        raise ChallengeError(f"{name} already exists")
    text = AGENT_YAML.format(name=name, owner=owner, tier=tier, capabilities=", ".join(capabilities))
    problems = registry.validate_entry(yaml.safe_load(text), yaml_path.as_posix())
    if problems:
        raise ChallengeError("\n".join(problems))
    yaml_path.write_text(text, encoding="utf-8", newline="\n")
    suite_path.parent.mkdir(parents=True, exist_ok=True)
    suite_path.write_text(SUITE_YAML.format(name=name), encoding="utf-8", newline="\n")
    return [yaml_path, suite_path]
