#!/usr/bin/env bash
# Set up Timefold for Field Service in a Dynamics 365 environment.
#
#   ./scripts/setup.sh --org https://<org>.crm4.dynamics.com
#
# Idempotent; stops at the first error. It:
#   1. checks the tools (uv; .NET is installed into .tools/ when needed)
#   2. signs you in to the environment, if .env does not point at it yet
#   3. builds the plug-in
#   4. creates or updates the solution and stores the Timefold API key
#   5. offers to load the London demo data
#   6. prints the link that opens the Field Service app
#
# Options:
#   --org URL          the environment (default: DATAVERSE_URL in .env)
#   --api-key KEY      the Timefold API key (default: TIMEFOLD_API_KEY, then .env)
#   --seed, --no-seed  load the demo data, or don't (default: ask; no without a terminal)
#   --yes              take the default for each question
#   --no-export        don't write the solution zips in dynamics/solution/
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

org="" key="${TIMEFOLD_API_KEY:-}" seed="ask" yes=false export_flag=""
while [ $# -gt 0 ]; do
  case "$1" in
    --org|--target-org) org="${2:?--org needs a URL}"; shift 2 ;;
    --api-key) key="${2:?--api-key needs a value}"; shift 2 ;;
    --seed) seed=yes; shift ;;
    --no-seed) seed=no; shift ;;
    --yes|-y) yes=true; shift ;;
    --no-export) export_flag="--no-export"; shift ;;
    -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

step() { printf '\n== %s\n' "$*"; }
from_env() { [ -f .env ] && sed -n "s/^$1=//p" .env | tail -1 | tr -d '"'"'" || true; }

step "1. Tools"
command -v uv >/dev/null || { echo "Needs uv: https://docs.astral.sh/uv/getting-started/installation/" >&2; exit 1; }
[ -f .env ] || { cp .env.example .env; echo "Created .env from .env.example"; }
echo "uv $(uv --version | cut -d' ' -f2)"

step "2. Sign in"
current="$(from_env DATAVERSE_URL)"
org="${org:-$current}"
[ -n "$org" ] || { echo "Give the environment: ./scripts/setup.sh --org https://<org>.crm4.dynamics.com" >&2; exit 2; }
org="${org%/}"
if [ "$org" != "$current" ] || [ ! -f .auth/token_cache.json ]; then
  uv run -q scripts/login.py "$org"
else
  echo "Signed in to $org (scripts/login.py signs in again)"
fi

step "3. Build the plug-in"
./scripts/build_plugin.sh

step "4. Solution"
key="${key:-$(from_env TIMEFOLD_API_KEY)}"
[ -n "$key" ] || echo "No Timefold API key: set TIMEFOLD_API_KEY in .env or pass --api-key, or set it in Dynamics later."
TIMEFOLD_API_KEY="$key" uv run -q scripts/deploy_solution.py $export_flag

step "5. Demo data"
if [ "$seed" = ask ]; then
  if $yes || [ ! -t 0 ]; then
    seed=no
  else
    read -r -p "Load the London demo (8 engineers, 102 work orders over 3 days)? Don't in an environment with real data. [y/N] " answer
    case "$answer" in [yY]*) seed=yes ;; *) seed=no ;; esac
  fi
fi
if [ "$seed" = yes ]; then uv run -q scripts/seed_env.py; else echo "Skipped (scripts/seed_env.py loads it later)"; fi

step "6. Done"
echo "Open Work Orders and press Optimise with Timefold:"
echo "  $org/main.aspx?appname=msdyn_FieldService&pagetype=entitylist&etn=msdyn_workorder"
