#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["msal>=1.28", "httpx>=0.27", "tzdata"]
# ///
"""Remove the demo's bookings for one day, so it can be planned again.

    uv run scripts/reset_day.py 2026-11-03

Only bookings on the demo's own work orders are removed; Field Service then
returns those work orders to Unscheduled itself.
"""
from __future__ import annotations

import json
import sys

from _common import ROOT, real_client


def main() -> None:
    day = sys.argv[1]
    marker = json.loads((ROOT / "scripts/demo/fsr_london.json").read_text())["marker"]
    with real_client() as dv:
        accounts = {r["accountid"] for r in dv.get("accounts", select=["accountid"],
                                                    filter=f"description eq '{marker}'")}
        work_orders = {r["msdyn_workorderid"] for r in dv.get(
            "msdyn_workorders", select=["msdyn_workorderid", "_msdyn_serviceaccount_value"])
            if r.get("_msdyn_serviceaccount_value") in accounts}
        rows = dv.get("bookableresourcebookings",
                      select=["bookableresourcebookingid", "_msdyn_workorder_value"],
                      filter=f"starttime ge {day}T00:00:00Z and starttime le {day}T23:59:59Z")
        mine = [r for r in rows if r.get("_msdyn_workorder_value") in work_orders]
        for r in mine:
            dv.delete("bookableresourcebookings", r["bookableresourcebookingid"])
        print(f"removed {len(mine)} demo bookings on {day} "
              f"({len(rows) - len(mine)} other bookings left alone)")


if __name__ == "__main__":
    main()
