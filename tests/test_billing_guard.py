"""The billing guard's decisions, without touching a real billing account."""

import base64
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

GUARD = Path(__file__).resolve().parents[1] / "infra" / "billing_guard" / "main.py"


class FakeResponse:
    def __init__(self, status, body=None):
        self.status_code = status
        self.ok = 200 <= status < 300
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError(self.status_code)


class FakeSession:
    def __init__(self, get_response):
        self.get_response = get_response
        self.unlinked = False

    def get(self, url):
        return self.get_response

    def put(self, url, json):
        assert json == {"billingAccountName": ""}
        self.unlinked = True
        return FakeResponse(200)


@pytest.fixture
def guard(monkeypatch):
    monkeypatch.setenv("PROJECT_ID", "p")
    monkeypatch.setenv("HARD_STOP_AMOUNT", "4")
    monkeypatch.setenv("BUDGET_CURRENCY", "MYR")
    monkeypatch.delenv("DRY_RUN", raising=False)
    spec = importlib.util.spec_from_file_location("billing_guard_main", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(guard, monkeypatch, cost, currency="MYR", get=FakeResponse(200, {"billingEnabled": True})):
    session = FakeSession(get)
    monkeypatch.setattr(guard, "_session", lambda: session)
    data = base64.b64encode(json.dumps({"costAmount": cost, "currencyCode": currency}).encode())
    guard.on_budget.__wrapped__(SimpleNamespace(data={"message": {"data": data}})) \
        if hasattr(guard.on_budget, "__wrapped__") else guard.on_budget(SimpleNamespace(data={"message": {"data": data}}))
    return session


def test_below_threshold_does_nothing(guard, monkeypatch):
    assert not run(guard, monkeypatch, cost=3.99).unlinked


def test_at_threshold_unlinks(guard, monkeypatch):
    assert run(guard, monkeypatch, cost=4.0).unlinked


def test_unreadable_billing_state_still_unlinks(guard, monkeypatch):
    # The bug this guards against: a 403 body has no billingEnabled field and
    # was read as "already disabled".
    assert run(guard, monkeypatch, cost=9, get=FakeResponse(403, {"error": {}})).unlinked


def test_already_disabled_is_left_alone(guard, monkeypatch):
    assert not run(guard, monkeypatch, cost=9, get=FakeResponse(200, {"billingEnabled": False})).unlinked


def test_unexpected_currency_fails_closed(guard, monkeypatch):
    assert run(guard, monkeypatch, cost=0.01, currency="USD").unlinked


def test_dry_run_never_unlinks(guard, monkeypatch):
    monkeypatch.setattr(guard, "DRY_RUN", True)
    assert not run(guard, monkeypatch, cost=100).unlinked
