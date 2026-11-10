"""Access tokens for the Dataverse Web API.

Dynamics 365 Field Service keeps its data in Dataverse, and Dataverse accepts
only Microsoft Entra ID bearer tokens. Two ways to get one, tried in order:

1. **An app registration** (``DATAVERSE_CLIENT_ID`` + ``DATAVERSE_CLIENT_SECRET``),
   added to the environment as an application user. This is what production
   uses: no person involved, no expiry to babysit.

2. **Your own sign-in**, cached by ``scripts/login.py``. It runs the OAuth
   device-code flow against Microsoft's public Dataverse sample client, so no
   app registration is needed at all. The cache holds a refresh token, and
   MSAL renews the access token from it silently for as long as your session
   lives (90 days of inactivity, by default).
"""
from __future__ import annotations

import logging
import os
import threading

log = logging.getLogger("tfd365.auth")

# Microsoft's public client id for Dataverse samples and tooling. It is
# pre-consented for Dataverse in every tenant that allows user consent, which
# is why the device-code flow works without an app registration.
# https://learn.microsoft.com/power-apps/developer/data-platform/xrm-tooling/use-connection-strings-xrm-tooling-connect
PUBLIC_CLIENT_ID = "51f81489-12ee-4a9e-aaae-a2591f45987d"


class AuthError(RuntimeError):
    pass


def _scope(org_url: str, delegated: bool) -> list[str]:
    org = org_url.rstrip("/")
    return [f"{org}/user_impersonation"] if delegated else [f"{org}/.default"]


class TokenProvider:
    """Hands out a valid access token, renewing it when needed."""

    def __init__(self, org_url: str, tenant_id: str = "", *, client_id: str = "",
                 client_secret: str = "", cache_file: str = ""):
        self.org_url = org_url.rstrip("/")
        self.tenant_id = tenant_id or "organizations"
        self.client_id = client_id
        self.client_secret = client_secret
        self.cache_file = cache_file
        self._lock = threading.Lock()
        self._app = None
        self._cache = None

    @property
    def mode(self) -> str:
        if self.client_id and self.client_secret:
            return "app-registration"
        if self.cache_file and os.path.exists(self.cache_file):
            return "user-sign-in"
        return "none"

    def token(self) -> str:
        with self._lock:
            if self.mode == "app-registration":
                return self._client_credentials()
            if self.mode == "user-sign-in":
                return self._from_cache()
        raise AuthError(
            "No Dataverse credentials. Run `uv run scripts/login.py` to sign in, "
            "or set DATAVERSE_CLIENT_ID and DATAVERSE_CLIENT_SECRET for an app "
            "registration.")

    # ------------------------------------------------------------------ app
    def _client_credentials(self) -> str:
        import msal
        if self._app is None:
            self._app = msal.ConfidentialClientApplication(
                self.client_id, client_credential=self.client_secret,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}")
        result = self._app.acquire_token_for_client(_scope(self.org_url, False))
        if "access_token" not in result:
            raise AuthError(f"Client-credentials sign-in failed: "
                            f"{result.get('error_description') or result}")
        return result["access_token"]

    # ----------------------------------------------------------------- user
    def _from_cache(self) -> str:
        import msal
        if self._app is None:
            self._cache = msal.SerializableTokenCache()
            with open(self.cache_file) as f:
                self._cache.deserialize(f.read())
            self._app = msal.PublicClientApplication(
                PUBLIC_CLIENT_ID, token_cache=self._cache,
                authority=f"https://login.microsoftonline.com/{self.tenant_id}")
        accounts = self._app.get_accounts()
        result = (self._app.acquire_token_silent(_scope(self.org_url, True),
                                                 account=accounts[0])
                  if accounts else None)
        if not result or "access_token" not in result:
            raise AuthError(
                "The cached sign-in has expired or belongs to another environment. "
                "Run `uv run scripts/login.py` again.")
        if self._cache.has_state_changed:
            try:
                with open(self.cache_file, "w") as f:
                    f.write(self._cache.serialize())
            except OSError:                       # read-only mount: renew in memory
                log.debug("token cache is read-only; keeping the renewal in memory")
        return result["access_token"]


def device_code_login(org_url: str, tenant_id: str, cache_file: str,
                      announce=print) -> dict:
    """Interactive sign-in for scripts/login.py. Returns the token result."""
    import msal
    cache = msal.SerializableTokenCache()
    if os.path.exists(cache_file):
        with open(cache_file) as f:
            cache.deserialize(f.read())
    app = msal.PublicClientApplication(
        PUBLIC_CLIENT_ID, token_cache=cache,
        authority=f"https://login.microsoftonline.com/{tenant_id or 'organizations'}")
    scopes = _scope(org_url, True)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    if not result:
        flow = app.initiate_device_flow(scopes=scopes)
        if "user_code" not in flow:
            raise AuthError(f"Could not start the sign-in: {flow}")
        announce(flow["message"])
        result = app.acquire_token_by_device_flow(flow)
    if "access_token" not in result:
        raise AuthError(f"Sign-in failed: {result.get('error_description') or result}")
    os.makedirs(os.path.dirname(os.path.abspath(cache_file)), exist_ok=True)
    with open(cache_file, "w") as f:
        f.write(cache.serialize())
    os.chmod(cache_file, 0o600)
    return result
