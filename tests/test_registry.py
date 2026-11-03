"""The registry refuses an agent before it ever reaches the database."""

import re

import pytest

from counsel import registry
from counsel.compiler import CAPABILITY_SQL, compile_sql

GOOD = {
    "name": "team_digest",
    "description": "d",
    "owner": "platform",
    "tier": "T1",
    "capabilities": ["read_tasks", "write_drafts"],
    "triggers": {"schedule": "0 * * * *"},
    "suite": "evals/team_digest",
}


def problems(**change):
    doc = {**GOOD, **change}
    doc = {k: v for k, v in doc.items() if v is not None}
    return registry.validate_entry(doc, "agents/team_digest.yaml")


def test_good_entry_passes():
    assert problems() == []


@pytest.mark.parametrize("change, expected", [
    ({"tier": "T1", "capabilities": ["carry_updates"]}, "does not allow carry_updates"),
    ({"tier": "T3", "capabilities": ["read_tasks"]}, "higher than its capabilities need"),
    ({"capabilities": ["update_tasks"]}, "unknown capability 'update_tasks'"),
    ({"owner": None}, "missing 'owner'"),
    ({"suite": None}, "missing 'suite'"),
    ({"triggers": {}}, "missing 'triggers'"),
    ({"triggers": {"cron": "* * * * *"}}, "unknown trigger 'cron'"),
    ({"tier": "T9"}, "tier 'T9'"),
    ({"name": "Team-Digest"}, "must match"),
    ({"owner": "platform\ndrop table work.tasks;"}, "owner must match"),
])
def test_bad_entries_are_refused(change, expected):
    found = problems(**change)
    assert any(expected in p for p in found), found


def test_raw_grants_are_not_a_thing():
    found = registry.validate_entry({**GOOD, "grants": ["update work.tasks"]}, "agents/team_digest.yaml")
    assert any("unknown key 'grants'" in p for p in found)


def test_repository_registry_is_valid(agents):
    assert len(agents) == 8
    assert {a.tier for a in agents} == {"T0", "T1", "T2", "T3"}


def test_no_template_can_grant_a_commitment():
    for cap, statements in CAPABILITY_SQL.items():
        for stmt in statements:
            assert not re.search(r"\b(update|delete|truncate)\b", stmt, re.I), (cap, stmt)
            assert not re.search(r"insert[^;]*on work\.tasks\b", stmt, re.I), (cap, stmt)


def test_compile_is_deterministic(agents):
    assert compile_sql(agents) == compile_sql(list(reversed(agents)))
