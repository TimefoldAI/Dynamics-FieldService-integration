"""A small client for the Dataverse Web API (OData v4).

Dynamics 365 Field Service is a set of Dataverse tables, so this is the only
API the integration needs. It covers what the integration uses and nothing
else: paged reads, create, update, delete, and calling a bound function.

    GET    {org}/api/data/v9.2/{entitySet}?$select=...&$filter=...
    POST   {org}/api/data/v9.2/{entitySet}
    PATCH  {org}/api/data/v9.2/{entitySet}({id})
    DELETE {org}/api/data/v9.2/{entitySet}({id})

Lookups are read as ``_<name>_value`` and written as
``<navigationProperty>@odata.bind: "/<entitySet>(<id>)"``. The navigation
property name is case-sensitive and is not always the column name, which is the
single most common way to get a 400 from Dataverse.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any, Iterable
from urllib.parse import quote

import httpx

from . import http_retry
from .auth import TokenProvider

log = logging.getLogger("tfd365.dataverse")

FORMATTED = "OData.Community.Display.V1.FormattedValue"
API_PATH = "/api/data/v9.2/"


class DataverseError(RuntimeError):
    def __init__(self, status: int, payload: Any, context: str = ""):
        self.status, self.payload = status, payload
        message = payload
        if isinstance(payload, dict):
            message = (payload.get("error") or {}).get("message") or payload
        super().__init__(f"Dataverse returned {status}"
                         f"{' for ' + context if context else ''}: {message}")


def literal(value: Any) -> str:
    """Render a Python value as an OData literal for $filter."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    text = str(value)
    if re.fullmatch(r"[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", text):
        return text                                   # guids are bare
    return "'" + text.replace("'", "''") + "'"


def in_filter(column: str, values: Iterable[Any]) -> str:
    """``column in (...)`` for Dataverse, which has no OData `in` operator.

    The function takes the column's logical name, so a lookup read as
    ``_msdyn_territory_value`` is filtered as ``msdyn_territory``.
    """
    values = list(values)
    if not values:
        return "false"
    m = re.fullmatch(r"_(.+)_value", column)
    name = m.group(1) if m else column
    quoted = ",".join(f"'{v}'" for v in values)
    return f'Microsoft.Dynamics.CRM.In(PropertyName=\'{name}\',PropertyValues=[{quoted}])'


class DataverseClient:
    def __init__(self, org_url: str, tokens: TokenProvider | None = None,
                 timeout: float = 60.0, http_client: httpx.Client | None = None):
        self.org_url = org_url.rstrip("/")
        self.tokens = tokens
        # An http_client can be passed in, e.g. an in-process one for testing.
        self._client = http_client or httpx.Client(base_url=self.org_url + API_PATH,
                                                   timeout=timeout)
        self._client.headers.update({
            "Accept": "application/json", "OData-MaxVersion": "4.0", "OData-Version": "4.0",
            "Content-Type": "application/json; charset=utf-8"})

    def close(self):
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------------------------------------------------------- plumbing
    def _headers(self, extra: dict | None = None) -> dict:
        headers = dict(extra or {})
        if self.tokens is not None:
            headers["Authorization"] = "Bearer " + self.tokens.token()
        return headers

    # Errors Dataverse reports when the database is momentarily busy: a deadlock,
    # or the session dropped while a customisation is being published. Microsoft
    # documents both as transient; the request had no effect and can be repeated.
    TRANSIENT = ("deadlock", "database session was disconnected", "please try again",
                 "sql number: 1205", "there is no active transaction")

    def _send(self, method: str, url: str, context: str, *, headers=None, **kw):
        for attempt in range(6):
            response = http_retry.request(self._client, method, url, context=context,
                                          headers=self._headers(headers), **kw)
            if response.status_code < 400:
                return response
            try:
                payload = response.json()
            except ValueError:
                payload = response.text
            text = str(payload).lower()
            if attempt < 5 and any(t in text for t in self.TRANSIENT):
                log.warning("%s: Dataverse is busy (%s); retrying", context,
                            "deadlock" if "deadlock" in text else "session dropped")
                time.sleep(3 * 2 ** attempt)            # 3, 6, 12, 24, 48 s
                continue
            raise DataverseError(response.status_code, payload, context)

    # ------------------------------------------------------------------- reads
    def get(self, entity_set: str, *, select: Iterable[str] = (),
            filter: str = "", orderby: str = "", expand: str = "",
            top: int | None = None, formatted: bool = False) -> list[dict]:
        """Every row matching the query, following @odata.nextLink."""
        params = []
        if select:
            params.append("$select=" + ",".join(select))
        if filter:
            params.append("$filter=" + quote(filter, safe="(),'=$:/@-_."))
        if orderby:
            params.append("$orderby=" + quote(orderby, safe=","))
        if expand:
            params.append("$expand=" + quote(expand, safe="(),;=$"))
        if top:
            params.append(f"$top={top}")
        url = entity_set + ("?" + "&".join(params) if params else "")
        prefer = ["odata.maxpagesize=5000"]
        if formatted:
            prefer.append(f'odata.include-annotations="{FORMATTED}"')
        rows: list[dict] = []
        while url:
            body = self._send("GET", url, f"GET {entity_set}",
                              headers={"Prefer": ",".join(prefer)}).json()
            rows.extend(body.get("value", []))
            url = None if top else body.get("@odata.nextLink")
            if url and url.startswith(self.org_url):
                url = url[len(self.org_url + API_PATH):]
        return rows

    def get_one(self, entity_set: str, record_id: str, select: Iterable[str] = ()) -> dict:
        url = f"{entity_set}({record_id})"
        if select:
            url += "?$select=" + ",".join(select)
        return self._send("GET", url, f"GET {entity_set}({record_id})").json()

    def function(self, path: str) -> dict:
        """Call a function, e.g. ``WhoAmI`` or ``calendars(<id>)/...ExpandCalendar(...)``."""
        return self._send("GET", path, f"GET {path.split('(')[0]}").json()

    def action(self, name: str, body: dict) -> dict:
        response = self._send("POST", name, f"POST {name}", json=body)
        return response.json() if response.content else {}

    def ping(self) -> dict:
        who = self.function("WhoAmI")
        return {"reachable": True, "orgUrl": self.org_url,
                "organizationId": who.get("OrganizationId"),
                "userId": who.get("UserId")}

    def count(self, entity_set: str, filter: str = "") -> int:
        url = f"{entity_set}?$count=true&$top=1&$select={_primary_key(entity_set)}"
        if filter:
            url += "&$filter=" + quote(filter, safe="(),'=$:/@-_.")
        return int(self._send("GET", url, f"COUNT {entity_set}").json()
                   .get("@odata.count", 0))

    # ------------------------------------------------------------------ writes
    def create(self, entity_set: str, record: dict) -> str:
        """Create a row and return its id."""
        response = self._send("POST", entity_set, f"POST {entity_set}", json=record)
        location = response.headers.get("OData-EntityId", "")
        match = re.search(r"\(([0-9a-fA-F-]{36})\)\s*$", location)
        if match:
            return match.group(1)
        body = response.json() if response.content else {}
        return body.get(_primary_key(entity_set), "")

    def update(self, entity_set: str, record_id: str, changes: dict) -> None:
        self._send("PATCH", f"{entity_set}({record_id})",
                   f"PATCH {entity_set}({record_id})", json=changes,
                   headers={"If-Match": "*"})

    def delete(self, entity_set: str, record_id: str) -> None:
        self._send("DELETE", f"{entity_set}({record_id})",
                   f"DELETE {entity_set}({record_id})")


def _primary_key(entity_set: str) -> str:
    """The primary key column of an entity set, by Dataverse's naming rule."""
    singular = {"msdyn_workorders": "msdyn_workorder",
                "msdyn_resourcerequirements": "msdyn_resourcerequirement",
                "bookableresources": "bookableresource",
                "bookableresourcebookings": "bookableresourcebooking",
                "bookableresourcecharacteristics": "bookableresourcecharacteristic",
                "msdyn_requirementcharacteristics": "msdyn_requirementcharacteristic",
                "msdyn_resourceterritories": "msdyn_resourceterritory",
                "characteristics": "characteristic",
                "territories": "territory",
                "accounts": "account",
                "contacts": "contact",
                "bookingstatuses": "bookingstatus"}.get(entity_set)
    if singular is None:
        singular = entity_set[:-3] + "y" if entity_set.endswith("ies") else entity_set.rstrip("s")
    return singular + "id"


def formatted(row: dict, column: str) -> str | None:
    """The display value Dataverse attaches to lookups and choices."""
    return row.get(f"{column}@{FORMATTED}")
