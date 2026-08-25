#!/usr/bin/env bash
# Inspect the structured JSON log written by checkout-api.
#
#   bash scripts/logs.sh errors      # ERROR/CRITICAL lines with their context
#   bash scripts/logs.sh checkouts   # checkout_completed + checkout_failed
#   bash scripts/logs.sh deps        # dependency/database/pool problems
#   bash scripts/logs.sh tail        # last 10 lines (default)
set -uo pipefail

cd "$(dirname "$0")/.."
LOG_FILE="${LOG_FILE:-var/log/checkout-api.log}"
MODE="${1:-tail}"

python3 - "$LOG_FILE" "$MODE" <<'PY'
import json
import sys

path, mode = sys.argv[1], sys.argv[2]
try:
    lines = open(path, encoding="utf-8").read().splitlines()
except FileNotFoundError:
    print(f"log file not found: {path}")
    raise SystemExit(1)

records = []
for line in lines:
    line = line.strip()
    if not line:
        continue
    try:
        records.append(json.loads(line))
    except json.JSONDecodeError:
        continue

if mode == "errors":
    selected = [r for r in records if str(r.get("level", "")).upper() in {"ERROR", "CRITICAL"}]
elif mode == "checkouts":
    selected = [r for r in records if r.get("event") in {"checkout_completed", "checkout_failed", "checkout_payment_failed"}]
elif mode == "deps":
    selected = [
        r
        for r in records
        if r.get("dependency") or r.get("event") in {"database_pool_exhausted", "database_unavailable", "readiness_check_failed"}
    ]
else:
    selected = records[-10:]

for record in selected:
    ts = record.get("timestamp", "-")
    level = record.get("level", "-")
    event = record.get("event", "-")
    print(f"{ts} {level:<7} {event:<22} request_id={record.get('request_id', '-')} route={record.get('route', '-')}")
    for key in ("error", "error_code", "error_message", "duration_ms", "exit_code", "status_code", "pool_size", "checked_out"):
        if key in record:
            print(f"    {key}: {record[key]}")

print(f"\n{len(selected)} matching line(s) out of {len(records)} in {path}")
PY
