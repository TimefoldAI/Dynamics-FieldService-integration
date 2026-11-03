"""Shared helpers for the command-line scripts."""
from __future__ import annotations

import os
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
TOKEN_CACHE = ROOT / ".auth" / "token_cache.json"
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "demo"))


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$", line)
            if m and not line.lstrip().startswith("#"):
                values[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    return {**values, **{k: v for k, v in os.environ.items() if k.startswith(
        ("DATAVERSE_", "TIMEFOLD_", "ROOT_TERRITORY"))}}


def write_env(updates: dict[str, str]) -> None:
    """Set keys in .env, keeping every other line and comment as it was."""
    if not ENV_FILE.exists():
        example = ROOT / ".env.example"
        ENV_FILE.write_text(example.read_text() if example.exists() else "")
    lines = ENV_FILE.read_text().splitlines()
    pending = dict(updates)
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z0-9_]+)\s*=", line)
        if m and m.group(1) in pending:
            lines[i] = f"{m.group(1)}={pending.pop(m.group(1))}"
    lines += [f"{k}={v}" for k, v in pending.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n")


def real_client(env: dict[str, str] | None = None):
    """A Dataverse client for the environment named in .env."""
    from tfd365.auth import TokenProvider
    from tfd365.dataverse import DataverseClient
    env = env or read_env()
    url = env.get("DATAVERSE_URL", "")
    if not url.startswith("https://"):
        sys.exit("No real environment in .env. Run: uv run scripts/login.py <your org URL>")
    tokens = TokenProvider(url, env.get("DATAVERSE_TENANT_ID", ""),
                           client_id=env.get("DATAVERSE_CLIENT_ID", ""),
                           client_secret=env.get("DATAVERSE_CLIENT_SECRET", ""),
                           cache_file=str(TOKEN_CACHE))
    return DataverseClient(url, tokens)
