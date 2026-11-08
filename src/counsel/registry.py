"""The agent registry: agents/*.yaml, validated before anything is compiled.

An agent cannot ask for raw grants. It picks named capabilities, and each
capability has a minimum tier and a fixed SQL template in compiler.py. There is
no capability that touches owner_id, due_date or status on work.tasks, that
writes to a customer-visible thread, or that reads support.integration_tokens.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TIERS = ("T0", "T1", "T2", "T3")

# capability -> minimum tier
CAPABILITIES: dict[str, str] = {
    "read_handbook": "T0",
    "write_answers": "T0",
    "read_tasks": "T1",
    "read_updates": "T1",
    "read_tickets": "T1",
    "read_issues": "T1",
    "read_platform": "T1",
    "write_drafts": "T1",
    # A draft meant for a customer: external-facing, so a person sends it.
    "draft_support_replies": "T2",
    "carry_updates": "T2",
    "send_messages": "T3",
}

REQUIRED = ("name", "description", "owner", "tier", "capabilities", "triggers", "suite")
# budget_usd: the owner's monthly model spend; `counsel report` flags a projected overrun.
# cache_hours: reuse the answer to an identical request for this long. T0 only:
# a T0 prompt holds the question and handbook text, never another person's data.
# minutes_per_output: the owner's estimate of how long a person takes to do one
# output by hand. The value ledger counts it only for outputs a person accepted.
# watches: public GitHub repositories whose new issues trigger this agent.
# pool: the runtime that runs this agent, each with its own database login and
# Cloud Run service. A login can become only its own pool's agents, so a
# compromised runtime holds one pool's rights, not every agent's (docs/red-team.md, R3).
OPTIONAL = ("budget_usd", "cache_hours", "minutes_per_output", "watches", "pool")
CORE_POOL = "core"
POOL_RE = re.compile(r"^[a-z][a-z0-9]{1,20}$")
# Customer data stays out of the shared pool: an agent that reads it runs on its own login.
CUSTOMER_DATA = frozenset({"read_tickets", "draft_support_replies"})


def login_for(pool: str) -> str:
    """The database login a pool's runtime connects as."""
    return "counsel_runtime" if pool == CORE_POOL else f"counsel_runtime_{pool}"
ALLOWED = set(REQUIRED) | set(OPTIONAL)
TRIGGER_KEYS = {"schedule", "events"}
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
OWNER_RE = re.compile(r"^[a-z][a-z0-9._-]{1,40}$")
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class RegistryError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Agent:
    name: str
    description: str
    owner: str
    tier: str
    capabilities: tuple[str, ...]
    schedule: str | None
    events: tuple[str, ...]
    suite: str
    source: str = field(default="", compare=False)
    budget_usd: float | None = None
    cache_hours: int | None = None
    minutes_per_output: int | None = None
    watches: tuple[str, ...] = ()
    pool: str = CORE_POOL

    @property
    def role(self) -> str:
        return f"agent_{self.name}"

    @property
    def login(self) -> str:
        return login_for(self.pool)


def _tier_index(tier: str) -> int:
    return TIERS.index(tier)


def validate_entry(doc: object, source: str) -> list[str]:
    where = source
    if not isinstance(doc, dict):
        return [f"{where}: must be a mapping"]

    problems = []
    for key in REQUIRED:
        if doc.get(key) in (None, "", [], {}):
            problems.append(f"{where}: missing '{key}'")
    for key in sorted(set(doc) - ALLOWED):
        problems.append(f"{where}: unknown key '{key}' (agents pick capabilities, not grants)")

    name = doc.get("name")
    if isinstance(name, str) and not NAME_RE.match(name):
        problems.append(f"{where}: name '{name}' must match {NAME_RE.pattern}")
    if isinstance(name, str) and Path(source).stem.replace("-", "_") != name:
        problems.append(f"{where}: file name must match agent name '{name}'")

    owner = doc.get("owner")
    if owner is not None and not (isinstance(owner, str) and OWNER_RE.match(owner)):
        problems.append(f"{where}: owner must match {OWNER_RE.pattern}")

    tier = doc.get("tier")
    if tier is not None and tier not in TIERS:
        problems.append(f"{where}: tier '{tier}' is not one of {', '.join(TIERS)}")

    caps = doc.get("capabilities") or []
    if not isinstance(caps, list):
        problems.append(f"{where}: capabilities must be a list")
        caps = []
    unknown = [c for c in caps if c not in CAPABILITIES]
    for c in unknown:
        problems.append(f"{where}: unknown capability '{c}'")
    if len(set(caps)) != len(caps):
        problems.append(f"{where}: duplicate capabilities")

    known = [c for c in caps if c in CAPABILITIES]
    if tier in TIERS and known:
        needed = max(_tier_index(CAPABILITIES[c]) for c in known)
        if needed > _tier_index(tier):
            too_high = [c for c in known if _tier_index(CAPABILITIES[c]) > _tier_index(tier)]
            problems.append(f"{where}: tier {tier} does not allow {', '.join(too_high)}")
        elif needed < _tier_index(tier):
            # Over-tiering hides what an agent can really do and buys review it does not need.
            problems.append(f"{where}: tier {tier} is higher than its capabilities need ({TIERS[needed]})")

    budget = doc.get("budget_usd")
    if budget is not None and (isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget <= 0):
        problems.append(f"{where}: budget_usd must be a positive number of dollars per month")
    minutes = doc.get("minutes_per_output")
    if minutes is not None and (isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 480):
        problems.append(f"{where}: minutes_per_output must be a whole number of minutes, 1 to 480")
    cache = doc.get("cache_hours")
    if cache is not None:
        if isinstance(cache, bool) or not isinstance(cache, int) or not 1 <= cache <= 24 * 30:
            problems.append(f"{where}: cache_hours must be a whole number of hours, 1 to 720")
        elif tier != "T0":
            problems.append(f"{where}: cache_hours is only allowed at T0 (higher tiers read "
                            "per-person or per-customer data)")

    pool = doc.get("pool", CORE_POOL)
    if not (isinstance(pool, str) and POOL_RE.match(pool)):
        problems.append(f"{where}: pool must match {POOL_RE.pattern}")
    elif pool == CORE_POOL and CUSTOMER_DATA & set(known):
        problems.append(f"{where}: {', '.join(sorted(CUSTOMER_DATA & set(known)))} reads customer data, "
                        "so the agent needs its own pool, not the shared core runtime")

    watches = doc.get("watches")
    if watches is not None and not (isinstance(watches, list) and all(
            isinstance(r, str) and REPO_RE.match(r) for r in watches)):
        problems.append(f"{where}: watches must be a list of owner/name repositories")

    triggers = doc.get("triggers") or {}
    if not isinstance(triggers, dict):
        problems.append(f"{where}: triggers must be a mapping")
    else:
        for key in sorted(set(triggers) - TRIGGER_KEYS):
            problems.append(f"{where}: unknown trigger '{key}'")
        if not triggers.get("schedule") and not triggers.get("events"):
            problems.append(f"{where}: needs a schedule or at least one event")

    return problems


def load(directory: Path) -> list[Agent]:
    files = sorted(directory.glob("*.yaml"))
    if not files:
        raise RegistryError([f"{directory}: no agents found"])

    problems: list[str] = []
    agents: list[Agent] = []
    for path in files:
        source = path.as_posix()
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
        found = validate_entry(doc, source)
        problems += found
        if found:
            continue
        triggers = doc["triggers"]
        agents.append(Agent(
            name=doc["name"],
            description=" ".join(str(doc["description"]).split()),
            owner=doc["owner"],
            tier=doc["tier"],
            capabilities=tuple(sorted(doc["capabilities"])),
            schedule=triggers.get("schedule"),
            events=tuple(triggers.get("events") or ()),
            suite=doc["suite"],
            source=source,
            budget_usd=doc.get("budget_usd"),
            cache_hours=doc.get("cache_hours"),
            minutes_per_output=doc.get("minutes_per_output"),
            watches=tuple(doc.get("watches") or ()),
            pool=doc.get("pool", CORE_POOL),
        ))

    names = [a.name for a in agents]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        problems.append(f"duplicate agent name '{dup}'")

    if problems:
        raise RegistryError(problems)
    return agents
