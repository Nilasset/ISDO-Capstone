"""Flask REST shim serving contract data from data/contracts.csv on port 5001."""
import csv
import os
from collections import defaultdict
from datetime import date, datetime, timedelta

from flask import Flask, jsonify, request

SIMULATED_TODAY = date(2025, 4, 1)
CSV_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "contracts.csv")
VALUE_COL = "annual_value_inr"  # column used for approval band and category totals
PATCHABLE = {"status", "owner", "proposed_uplift_pct"}

app = Flask(__name__)
CONTRACTS = {}  # id -> contract dict, loaded once at startup


def _num(value, cast=float, default=0):
    try:
        return cast(float(value)) if value not in (None, "") else default
    except (TypeError, ValueError):
        return default


def _notice_state(deadline, renewal):
    if renewal < SIMULATED_TODAY:
        return "EXPIRED"
    if deadline < SIMULATED_TODAY:
        return "INSIDE_WINDOW"
    if (deadline - SIMULATED_TODAY).days <= 30:
        return "APPROACHING"
    return "OPEN"


def _approval_band(value):
    return "A" if value < 1_000_000 else "B" if value <= 5_000_000 else "C"


def _enrich(row):
    renewal = datetime.strptime(row["renewal_date"].strip(), "%Y-%m-%d").date()
    notice_days = _num(row.get("notice_days"), int)
    deadline = renewal - timedelta(days=notice_days)
    purchased = _num(row.get("seats_purchased"), int)
    active = _num(row.get("seats_active"), int)
    value = _num(row.get(VALUE_COL))
    row.update(
        notice_days=notice_days,
        seats_purchased=purchased,
        seats_active=active,
        **{VALUE_COL: value},
        notice_deadline=deadline.isoformat(),
        notice_state=_notice_state(deadline, renewal),
        utilisation_pct=round(active / purchased * 100, 2) if purchased else None,
        approval_band=_approval_band(value),
        days_until_renewal=(renewal - SIMULATED_TODAY).days,
    )
    return row


def load_contracts(path=CSV_PATH):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            row = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            CONTRACTS[str(row["contract_id"])] = _enrich(row)


def _not_found(contract_id):
    return jsonify(error="not_found", message=f"Contract {contract_id} not found"), 404


@app.get("/health")
def health():
    return jsonify(status="ok", contract_count=len(CONTRACTS),
                   simulated_date=SIMULATED_TODAY.isoformat())


@app.get("/api/contracts")
def list_contracts():
    filters = {
        "category": request.args.get("category"),
        "approval_band": request.args.get("band"),
        "notice_state": request.args.get("notice_state"),
    }
    results = [
        c for c in CONTRACTS.values()
        if all(v is None or str(c.get(k, "")).lower() == v.lower() for k, v in filters.items())
    ]
    return jsonify(count=len(results), contracts=results)


@app.get("/api/contracts/expiring")
def expiring_contracts():
    try:
        days = int(request.args.get("days", 90))
    except ValueError:
        return jsonify(error="bad_request", message="days must be an integer"), 400
    results = sorted(
        (c for c in CONTRACTS.values() if 0 <= c["days_until_renewal"] <= days),
        key=lambda c: c["days_until_renewal"],
    )
    return jsonify(days=days, count=len(results), contracts=results)


@app.get("/api/contracts/<contract_id>")
def get_contract(contract_id):
    contract = CONTRACTS.get(contract_id)
    return jsonify(contract) if contract else _not_found(contract_id)


@app.get("/api/categories")
def categories():
    grouped = defaultdict(lambda: {"vendors": set(), "total_value": 0.0, "contract_count": 0})
    for c in CONTRACTS.values():
        g = grouped[c.get("category") or "Uncategorised"]
        g["vendors"].add(c.get("vendor", ""))
        g["total_value"] += c[VALUE_COL]
        g["contract_count"] += 1
    return jsonify(categories=[
        {"category": cat, "vendors": sorted(g["vendors"]),
         "total_value": round(g["total_value"], 2), "contract_count": g["contract_count"]}
        for cat, g in sorted(grouped.items())
    ])


@app.patch("/api/contracts/<contract_id>")
def patch_contract(contract_id):
    contract = CONTRACTS.get(contract_id)
    if not contract:
        return _not_found(contract_id)
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error="bad_request", message="JSON object body required"), 400
    unknown = set(body) - PATCHABLE
    if unknown:
        return jsonify(error="bad_request", message=f"Cannot update: {sorted(unknown)}",
                       allowed=sorted(PATCHABLE)), 400
    if "proposed_uplift_pct" in body:
        try:
            body["proposed_uplift_pct"] = float(body["proposed_uplift_pct"])
        except (TypeError, ValueError):
            return jsonify(error="bad_request", message="proposed_uplift_pct must be numeric"), 400
    contract.update(body)
    return jsonify(contract)


load_contracts()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5001)