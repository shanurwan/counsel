"""Billing hard stop.

Cloud Billing budgets publish to Pub/Sub several times a day. When actual cost
for the project reaches HARD_STOP_AMOUNT (in the billing account's currency),
this unlinks the billing account, which stops every paid service in the
project.

A budget alert alone only sends an email; this turns the alert into a stop.
Budget data lags real usage by hours, so the worst case is the threshold plus
a few hours of spend, not exactly the threshold.
"""

import base64
import json
import os

import functions_framework
import google.auth
from google.auth.transport.requests import AuthorizedSession

PROJECT_ID = os.environ["PROJECT_ID"]
HARD_STOP = float(os.environ["HARD_STOP_AMOUNT"])
CURRENCY = os.environ["BUDGET_CURRENCY"]
DRY_RUN = os.environ.get("DRY_RUN", "false") == "true"

BILLING_INFO = f"https://cloudbilling.googleapis.com/v1/projects/{PROJECT_ID}/billingInfo"


def log(severity: str, message: str, **fields) -> None:
    # One JSON object per line: Cloud Logging reads severity and fields from it.
    print(json.dumps({"severity": severity, "message": message, **fields}), flush=True)


def _session() -> AuthorizedSession:
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-billing"])
    return AuthorizedSession(creds)


@functions_framework.cloud_event
def on_budget(event):
    data = json.loads(base64.b64decode(event.data["message"]["data"]))
    cost = float(data.get("costAmount", 0))
    currency = data.get("currencyCode")
    log("INFO", "budget notification", cost=cost, currency=currency, hard_stop=HARD_STOP)

    if currency != CURRENCY:
        log("ERROR", "unexpected currency; stopping to be safe", currency=currency, expected=CURRENCY)
    elif cost < HARD_STOP:
        return

    session = _session()
    # Fail closed: if the current state cannot be read, unlink anyway. Treating
    # an error response as "already disabled" would leave billing running.
    resp = session.get(BILLING_INFO)
    if resp.ok and resp.json().get("billingEnabled") is False:
        log("INFO", "billing already disabled")
        return
    if not resp.ok:
        log("ERROR", "could not read billing state; unlinking anyway", status=resp.status_code)

    if DRY_RUN:
        log("WARNING", "dry run: would disable billing", project=PROJECT_ID, cost=cost)
        return

    resp = session.put(BILLING_INFO, json={"billingAccountName": ""})
    resp.raise_for_status()
    log("CRITICAL", "billing disabled", project=PROJECT_ID, cost=cost, currency=currency)
