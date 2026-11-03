#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["msal>=1.28", "httpx>=0.27", "tzdata"]
# ///
"""Load the London demo into your Dynamics 365 environment, or remove it.

    uv run scripts/seed_env.py              # load, day 0 = the next working day
    uv run scripts/seed_env.py --today      # load, day 0 = today (if a weekday)
    uv run scripts/seed_env.py --replace    # remove the demo, then load it again
    uv run scripts/seed_env.py --delete     # remove the demo

Every record goes into a standard Field Service table. Records the demo
creates are marked in their description field, and only those are removed.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from _common import ROOT, real_client

DATASET = ROOT / "scripts" / "demo" / "fsr_london.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--delete", action="store_true", help="remove the demo data")
    parser.add_argument("--replace", action="store_true", help="remove, then load again")
    parser.add_argument("--today", action="store_true", help="start the demo today")
    parser.add_argument("--anchor", type=date.fromisoformat, help="first day, YYYY-MM-DD")
    args = parser.parse_args()

    from loader import Loader, next_business_day
    data = json.loads(DATASET.read_text())
    with real_client() as dv:
        loader = Loader(dv, data)
        if args.delete or args.replace:
            print("Removing the demo data")
            print(json.dumps(loader.delete(), indent=1))
            if args.delete:
                return
        if loader.already_loaded():
            sys.exit(f"The demo is already loaded (territory {data['rootTerritory']!r} "
                     "exists). Use --replace to load it again.")
        today = datetime.now(ZoneInfo(data["timeZone"])).date()
        anchor = args.anchor or next_business_day(today, include_today=args.today)
        summary = loader.load(anchor)
    print(json.dumps(summary, indent=1))
    print("\nOpen Field Service > Schedule Board and go to "
          f"{summary['days'][0]} to see the unscheduled work.")


if __name__ == "__main__":
    main()
