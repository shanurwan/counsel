"""Structured logs: one JSON object per line, which Cloud Logging parses natively."""

import json
import sys
from datetime import datetime, timezone


def log(severity: str, message: str, **fields) -> None:
    record = {"severity": severity, "message": message,
              "time": datetime.now(timezone.utc).isoformat(), **fields}
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)
