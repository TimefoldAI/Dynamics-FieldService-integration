#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["msal>=1.28", "httpx>=0.27"]
# ///
"""Sign in to your Dynamics 365 environment and point .env at it.

    uv run scripts/login.py https://<org>.crm4.dynamics.com

Uses the OAuth device-code flow: it prints a code, you enter it at
https://microsoft.com/devicelogin and sign in as yourself. No app registration
is needed. The refresh token is cached in .auth/token_cache.json (git-ignored),
and the integration renews access tokens from it silently.

The tenant is discovered from the environment itself: Dataverse answers an
unauthenticated request with the sign-in authority in WWW-Authenticate.
"""
from __future__ import annotations

import argparse
import re
import sys

import httpx

from _common import TOKEN_CACHE, read_env, write_env


def discover_tenant(org_url: str) -> str:
    response = httpx.get(f"{org_url}/api/data/v9.2/", timeout=30)
    header = response.headers.get("WWW-Authenticate", "")
    m = re.search(r"login\.microsoftonline\.com/([0-9a-fA-F-]{36})", header)
    if not m:
        sys.exit(f"{org_url} did not identify its tenant (HTTP {response.status_code}). "
                 "Is this a Dynamics 365 / Dataverse URL?")
    return m.group(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("url", nargs="?", help="e.g. https://org1234.crm4.dynamics.com")
    args = parser.parse_args()
    url = (args.url or read_env().get("DATAVERSE_URL", "")).rstrip("/")
    if not url.startswith("https://"):
        parser.error("give your environment URL, e.g. https://org1234.crm4.dynamics.com")
    url = re.sub(r"(\.dynamics\.com).*$", r"\1", url)

    from tfd365.auth import device_code_login
    from tfd365.dataverse import DataverseClient
    from tfd365.auth import TokenProvider

    tenant = discover_tenant(url)
    print(f"Environment {url}\nTenant      {tenant}\n")
    device_code_login(url, tenant, str(TOKEN_CACHE), announce=lambda m: print(m + "\n"))
    with DataverseClient(url, TokenProvider(url, tenant, cache_file=str(TOKEN_CACHE))) as dv:
        who = dv.ping()
    write_env({"DATAVERSE_URL": url, "DATAVERSE_TENANT_ID": tenant})
    print(f"Signed in. User {who['userId']} in organization {who['organizationId']}.")
    print("Wrote DATAVERSE_URL and DATAVERSE_TENANT_ID to .env.")
    print("Next: uv run scripts/seed_env.py")


if __name__ == "__main__":
    main()
