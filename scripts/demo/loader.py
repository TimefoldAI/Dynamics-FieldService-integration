"""Load the demo dataset into Dynamics 365 Field Service, or remove it again.

``scripts/seed_env.py`` runs it against your environment over the Web API.

Everything is written to standard Field Service tables, through the same
routes a person would use in the app. That matters because Field Service does
real work on the way in: creating a work order with a primary incident type
makes Field Service create the resource requirement, copy the incident type's
duration and skills onto it, and take the location from the service account.
The integration then reads what Field Service produced, not what we wrote.

    rating model + values        ratingmodel, ratingvalue
    skills                       characteristic
    priorities                   msdyn_priority
    regions                      territory (children of one root territory)
    job types                    msdyn_incidenttype + msdyn_incidenttypecharacteristic
    work order type              msdyn_workordertype
    technicians                  contact + bookableresource (start at their address)
      skills                     bookableresourcecharacteristic
      regions                    msdyn_resourceterritory
      working hours, time off    msdyn_SaveCalendar on the resource calendar
    customers                    account (address + latitude/longitude)
    work orders                  msdyn_workorder (+ requirement, created by Field Service)
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable
from zoneinfo import ZoneInfo

CONTACT = 2
RESOURCE_ADDRESS, ORG_UNIT_ADDRESS = 690970000, 690970001
WO_UNSCHEDULED = 690970000
SKILL = 1                                    # characteristic.characteristictype


def next_business_day(today: date, include_today: bool) -> date:
    day = today if include_today else today + timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def business_days(anchor: date, count: int) -> list[date]:
    days, day = [], anchor
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def _q(text: str) -> str:
    return "'" + text.replace("'", "''") + "'"


def _utc(day: date, clock: str, tz: ZoneInfo) -> str:
    hour, minute = (int(p) for p in clock.split(":")[:2])
    local = datetime.combine(day, time(hour, minute), tzinfo=tz)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Loader:
    def __init__(self, dv, data: dict, say: Callable[[str], None] = print):
        self.dv, self.data, self.say = dv, data, say
        self.marker = data["marker"]
        self.tz = ZoneInfo(data["timeZone"])

    # ------------------------------------------------------------- helpers
    def _find(self, entity_set: str, name_column: str, name: str, key: str) -> str | None:
        rows = self.dv.get(entity_set, select=[key], filter=f"{name_column} eq {_q(name)}")
        return rows[0][key] if rows else None

    def _get_or_create(self, entity_set, name_column, name, key, record) -> str:
        found = self._find(entity_set, name_column, name, key)
        return found or self.dv.create(entity_set, {name_column: name, **record})

    def already_loaded(self) -> bool:
        return bool(self._find("territories", "name", self.data["rootTerritory"], "territoryid"))

    # ---------------------------------------------------------------- load
    def load(self, anchor: date) -> dict:
        d, dv = self.data, self.dv
        days = business_days(anchor, d["days"])
        self.say(f"Loading {d['title']}")
        self.say(f"  days: {', '.join(x.isoformat() for x in days)} ({d['timeZone']})")

        # Rating model: Trainee / Skilled / Expert
        rm = d["ratingModel"]
        levels = [v for _n, v in rm["values"]]
        model = self._get_or_create("ratingmodels", "name", rm["name"], "ratingmodelid",
                                    {"minratingvalue": min(levels), "maxratingvalue": max(levels)})
        rating = {}
        existing = {r["value"]: r["ratingvalueid"] for r in dv.get(
            "ratingvalues", select=["ratingvalueid", "value"],
            filter=f"_ratingmodel_value eq {model}")}
        for name, value in rm["values"]:
            rating[value] = existing.get(value) or dv.create("ratingvalues", {
                "name": name, "value": value,
                "RatingModel@odata.bind": f"/ratingmodels({model})"})

        skills = {s: self._get_or_create("characteristics", "name", s, "characteristicid",
                                         {"characteristictype": SKILL,
                                          "description": self.marker})
                  for s in d["skills"]}
        priorities = {p["name"]: self._get_or_create(
            "msdyn_priorities", "msdyn_name", p["name"], "msdyn_priorityid",
            {"msdyn_levelofimportance": p["level"],
             "msdyn_prioritycolor": p["color"]})
            for p in d["priorities"]}
        self.say(f"  {len(skills)} skills, {len(priorities)} priorities")

        root = self._get_or_create("territories", "name", d["rootTerritory"], "territoryid",
                                   {"description": self.marker})
        territories = {t: self._get_or_create(
            "territories", "name", t, "territoryid",
            {"description": self.marker,
             "parentterritoryid@odata.bind": f"/territories({root})"})
            for t in d["territories"]}

        price_list = (self._find("pricelevels", "name", "Standard", "pricelevelid")
                      or dv.get("pricelevels", select=["pricelevelid"], top=1)[0]["pricelevelid"])
        wo_type = self._get_or_create("msdyn_workordertypes", "msdyn_name", d["workOrderType"],
                                      "msdyn_workordertypeid", {
                                          "msdyn_incidentrequired": True,
                                          "msdyn_serviceaccountrequired": True,
                                          "msdyn_taxable": False,
                                          "msdyn_pricelist@odata.bind":
                                              f"/pricelevels({price_list})"})

        incidents = {}
        for it in d["incidentTypes"]:
            found = self._find("msdyn_incidenttypes", "msdyn_name", it["name"],
                               "msdyn_incidenttypeid")
            if found:
                incidents[it["name"]] = found
                continue
            incidents[it["name"]] = iid = dv.create("msdyn_incidenttypes", {
                "msdyn_name": it["name"], "msdyn_estimatedduration": it["minutes"],
                "msdyn_description": self.marker,
                "msdyn_defaultworkordertype@odata.bind": f"/msdyn_workordertypes({wo_type})"})
            dv.create("msdyn_incidenttypecharacteristics", {
                "msdyn_incidenttype@odata.bind": f"/msdyn_incidenttypes({iid})",
                "msdyn_characteristic@odata.bind": f"/characteristics({skills[it['skill']]})",
                "msdyn_ratingvalue@odata.bind": f"/ratingvalues({rating[it['level']]})"})
        self.say(f"  {len(territories)} territories under {d['rootTerritory']!r}, "
                 f"{len(incidents)} incident types")

        org_units = {u["name"]: self._get_or_create(
            "msdyn_organizationalunits", "msdyn_name", u["name"], "msdyn_organizationalunitid",
            {"msdyn_latitude": u["location"][0], "msdyn_longitude": u["location"][1]})
            for u in d.get("organizationalUnits", [])}
        roles = {r: self._get_or_create("bookableresourcecategories", "name", r,
                                        "bookableresourcecategoryid", {"description": self.marker})
                 for r in d.get("roles", [])}
        time_groups = {}
        for g in d.get("timeGroups", []):
            found = self._find("msdyn_timegroups", "msdyn_name", g["name"], "msdyn_timegroupid")
            time_groups[g["name"]] = found or dv.create("msdyn_timegroups", {"msdyn_name": g["name"]})
            if not found:
                for a, b in g["slots"]:
                    # Only the time of day matters; Field Service ignores the date.
                    dv.create("msdyn_timegroupdetails", {
                        "msdyn_name": f"{a}-{b}",
                        "msdyn_starttime": f"2026-01-05T{a}:00Z", "msdyn_endtime": f"2026-01-05T{b}:00Z",
                        "msdyn_timegroupid@odata.bind": f"/msdyn_timegroups({time_groups[g['name']]})"})
        self.say(f"  {len(org_units)} organisational units, {len(roles)} roles, "
                 f"{len(time_groups)} time groups")

        # Technicians -------------------------------------------------------
        resources = {}
        for t in d["technicians"]:
            name = f"{t['firstName']} {t['lastName']}"
            contact = dv.create("contacts", {
                "firstname": t["firstName"], "lastname": t["lastName"],
                "jobtitle": "Biomedical engineer", "description": self.marker,
                "address1_city": "London", "address1_country": "United Kingdom",
                "address1_latitude": t["home"][0], "address1_longitude": t["home"][1]})
            start_at = ORG_UNIT_ADDRESS if t.get("startAt") == "orgUnit" else RESOURCE_ADDRESS
            record = {
                "name": name, "resourcetype": CONTACT,
                "ContactId@odata.bind": f"/contacts({contact})",
                "timezone": d["dynamicsTimeZoneCode"],
                "msdyn_startlocation": start_at, "msdyn_endlocation": start_at,
                "msdyn_travelbeforeworkinghoursminutes": t.get("travelBeforeMinutes", 0),
                "msdyn_travelafterworkinghoursminutes": t.get("travelAfterMinutes", 0)}
            if t.get("hourlyRate"):
                record["msdyn_hourlyrate"] = t["hourlyRate"]
            if t.get("orgUnit") in org_units:
                record["msdyn_OrganizationalUnit@odata.bind"] = \
                    f"/msdyn_organizationalunits({org_units[t['orgUnit']]})"
            resource = resources[name] = dv.create("bookableresources", record)
            for role in t.get("roles", []):
                dv.create("bookableresourcecategoryassns", {
                    "Resource@odata.bind": f"/bookableresources({resource})",
                    "ResourceCategory@odata.bind": f"/bookableresourcecategories({roles[role]})"})
            for skill, level in t["skills"].items():
                dv.create("bookableresourcecharacteristics", {
                    "Resource@odata.bind": f"/bookableresources({resource})",
                    "Characteristic@odata.bind": f"/characteristics({skills[skill]})",
                    "RatingValue@odata.bind": f"/ratingvalues({rating[level]})"})
            dv.create("msdyn_resourceterritories", {
                "msdyn_resource@odata.bind": f"/bookableresources({resource})",
                "msdyn_territory@odata.bind": f"/territories({territories[t['territory']]})"})
            self._calendar(resource, t, days)
            self.say(f"  technician {name}")

        # Customers and work orders ----------------------------------------
        accounts = {}
        for c in d["customers"]:
            record = {
                "name": c["name"], "description": self.marker,
                "address1_line1": c["street"], "address1_city": c["city"],
                "address1_postalcode": c["postalCode"], "address1_country": c["country"],
                "address1_latitude": c["location"][0], "address1_longitude": c["location"][1]}
            if c.get("preferredTime"):
                record["preferredappointmenttimecode"] = c["preferredTime"]
            if "preferredDayOffset" in c:                       # Sunday is 0 in Dynamics
                record["preferredappointmentdaycode"] = (days[c["preferredDayOffset"]].weekday() + 1) % 7
            if c.get("preferredTechnician") in resources:
                record["msdyn_PreferredResource@odata.bind"] = \
                    f"/bookableresources({resources[c['preferredTechnician']]})"
            accounts[c["name"]] = dv.create("accounts", record)
        self.say(f"  {len(accounts)} customers")

        refs: dict[str, str] = {}                     # generator ref -> requirement id
        for n, w in enumerate(d["workOrders"], 1):
            day = days[w["day"]]
            record = {
                "msdyn_serviceaccount@odata.bind": f"/accounts({accounts[w['customer']]})",
                "msdyn_workordertype@odata.bind": f"/msdyn_workordertypes({wo_type})",
                "msdyn_pricelist@odata.bind": f"/pricelevels({price_list})",
                "msdyn_primaryincidenttype@odata.bind":
                    f"/msdyn_incidenttypes({incidents[w['incidentType']]})",
                "msdyn_serviceterritory@odata.bind":
                    f"/territories({territories[w['territory']]})",
                "msdyn_priority@odata.bind": f"/msdyn_priorities({priorities[w['priority']]})",
                "msdyn_systemstatus": WO_UNSCHEDULED,
                "msdyn_instructions": f"{w['incidentType']} at {w['customer']}.",
                "msdyn_datewindowstart": f"{day.isoformat()}T00:00:00Z",
                "msdyn_datewindowend": f"{day.isoformat()}T00:00:00Z",
            }
            if w["timeWindow"]:
                start, end = w["timeWindow"]
                record["msdyn_timewindowstart"] = _utc(day, start, self.tz)
                record["msdyn_timewindowend"] = _utc(day, end, self.tz)
                record["msdyn_timefrompromised"] = record["msdyn_timewindowstart"]
                record["msdyn_timetopromised"] = record["msdyn_timewindowend"]
            if w.get("promised"):
                start, end = w["promised"]
                record["msdyn_timefrompromised"] = _utc(day, start, self.tz)
                record["msdyn_timetopromised"] = _utc(day, end, self.tz)
            wo = dv.create("msdyn_workorders", record)
            self._features(w, wo, refs, resources, roles, org_units, time_groups)
            if n % 20 == 0:
                self.say(f"  {n} of {len(d['workOrders'])} work orders")
        self.say(f"  {len(d['workOrders'])} work orders")
        return {"days": [x.isoformat() for x in days], "technicians": len(d["technicians"]),
                "customers": len(accounts), "workOrders": len(d["workOrders"])}

    PREFERENCE = {"preferred": 690970000, "restricted": 690970001, "mustChooseFrom": 690970002}

    def _features(self, w, wo, refs, resources, roles, org_units, time_groups) -> None:
        """The standard scheduling features of one job, on the requirement Field
        Service created for its work order."""
        dv = self.dv
        reqs = dv.get("msdyn_resourcerequirements", select=["msdyn_resourcerequirementid"],
                      filter=f"_msdyn_workorder_value eq {wo}")
        if not reqs:
            return
        req = reqs[0]["msdyn_resourcerequirementid"]
        refs[w.get("ref", wo)] = req
        bind = lambda es, i: f"/{es}({i})"
        if w.get("timeGroup") in time_groups:
            dv.update("msdyn_resourcerequirements", req, {
                "msdyn_TimeGroup@odata.bind": bind("msdyn_timegroups", time_groups[w["timeGroup"]])})
        for role in w.get("roles", []):
            dv.create("msdyn_requirementresourcecategories", {
                "msdyn_name": role,
                "msdyn_ResourceRequirement@odata.bind": bind("msdyn_resourcerequirements", req),
                "msdyn_ResourceCategory@odata.bind": bind("bookableresourcecategories", roles[role])})
        for unit in w.get("orgUnits", []):
            dv.create("msdyn_requirementorganizationunits", {
                "msdyn_ResourceRequirement@odata.bind": bind("msdyn_resourcerequirements", req),
                "msdyn_OrganizationalUnit@odata.bind": bind("msdyn_organizationalunits",
                                                            org_units[unit])})
        for p in w.get("resourcePreferences", []):
            dv.create("msdyn_requirementresourcepreferences", {
                "msdyn_preferencetype": self.PREFERENCE[p["type"]],
                "msdyn_ResourceRequirement@odata.bind": bind("msdyn_resourcerequirements", req),
                "msdyn_WorkOrder@odata.bind": bind("msdyn_workorders", wo),
                "msdyn_BookableResource@odata.bind": bind("bookableresources",
                                                          resources[p["technician"]])})
        if w.get("after") and w["after"]["ref"] in refs:
            dv.create("msdyn_requirementdependencies", {
                "msdyn_name": f"{w['after']['ref']} before {w['ref']}",
                "msdyn_dependencytype": 192350000,                     # start after finish
                "msdyn_requiresameresource": bool(w["after"].get("sameResource")),
                "msdyn_requirement@odata.bind": bind("msdyn_resourcerequirements",
                                                     refs[w["after"]["ref"]]),
                "msdyn_dependentrequirement@odata.bind": bind("msdyn_resourcerequirements", req)})
        if w.get("engineers", 1) > 1:
            # Several engineers at once: more requirements on the same work order,
            # all in one requirement group.
            group = dv.create("msdyn_requirementgroups", {
                "msdyn_name": f"{w['incidentType']} · {w['customer']} ({self.marker})",
                "msdyn_autogrouptype": 0, "msdyn_istemplate": False})
            first = dv.get_one("msdyn_resourcerequirements", req, [
                "msdyn_name", "msdyn_duration", "msdyn_latitude", "msdyn_longitude",
                "msdyn_fromdate", "msdyn_todate", "msdyn_timewindowstart", "msdyn_timewindowend",
                "msdyn_timefrompromised", "msdyn_timetopromised", "_msdyn_territory_value",
                "_msdyn_priority_value", "_msdyn_status_value"])
            chars = dv.get("msdyn_requirementcharacteristics",
                           select=["_msdyn_characteristic_value", "_msdyn_ratingvalue_value"],
                           filter=f"_msdyn_resourcerequirement_value eq {req}")
            dv.update("msdyn_resourcerequirements", req, {
                "msdyn_requirementgroupid@odata.bind": bind("msdyn_requirementgroups", group)})
            for k in range(w["engineers"] - 1):
                extra = {c: first.get(c) for c in (
                    "msdyn_duration", "msdyn_latitude", "msdyn_longitude", "msdyn_fromdate",
                    "msdyn_todate", "msdyn_timewindowstart", "msdyn_timewindowend",
                    "msdyn_timefrompromised", "msdyn_timetopromised") if first.get(c) is not None}
                extra.update({
                    "msdyn_name": f"{first['msdyn_name']} ({k + 2})",
                    "msdyn_allocationmethod": 192350000,
                    "msdyn_WorkOrder@odata.bind": bind("msdyn_workorders", wo),
                    "msdyn_requirementgroupid@odata.bind": bind("msdyn_requirementgroups", group)})
                for col, nav, es in (("_msdyn_territory_value", "msdyn_Territory", "territories"),
                                     ("_msdyn_priority_value", "msdyn_Priority", "msdyn_priorities"),
                                     ("_msdyn_status_value", "msdyn_Status",
                                      "msdyn_requirementstatuses")):
                    if first.get(col):
                        extra[f"{nav}@odata.bind"] = bind(es, first[col])
                second = dv.create("msdyn_resourcerequirements", extra)
                for c in chars:
                    dv.create("msdyn_requirementcharacteristics", {
                        "msdyn_ResourceRequirement@odata.bind": bind("msdyn_resourcerequirements", second),
                        "msdyn_Characteristic@odata.bind": bind("characteristics",
                                                                c["_msdyn_characteristic_value"]),
                        "msdyn_ratingvalue@odata.bind": bind("ratingvalues",
                                                             c["_msdyn_ratingvalue_value"])})

    def _calendar(self, resource: str, tech: dict, days: list[date]) -> None:
        """Working hours as a weekly recurrence, then any time off on top."""
        calendar = self.dv.get_one("bookableresources", resource,
                                   ["_calendarid_value"])["_calendarid_value"]
        hours = tech["workingHours"]
        first = days[0] - timedelta(days=7)
        tz_code = self.data["dynamicsTimeZoneCode"]
        # Working hours split around the breaks; the breaks themselves as
        # WorkHourType 1, as Field Service's own work-hours editor saves them.
        rules, cursor = [], hours["start"]
        at = lambda clock: f"{first.isoformat()}T{clock}:00.000Z"
        for b0, b1 in sorted(hours.get("breaks", [])):
            rules.append({"StartTime": at(cursor), "EndTime": at(b0), "Effort": 1, "WorkHourType": 0})
            rules.append({"StartTime": at(b0), "EndTime": at(b1), "Effort": None, "WorkHourType": 1})
            cursor = b1
        rules.append({"StartTime": at(cursor), "EndTime": at(hours["end"]), "Effort": 1,
                      "WorkHourType": 0})
        self.dv.action("msdyn_SaveCalendar", {"CalendarEventInfo": json.dumps({
            "CalendarId": calendar, "EntityLogicalName": "bookableresource",
            "TimeZoneCode": tz_code,
            "RulesAndRecurrences": [{
                "Rules": rules,
                "RecurrencePattern": "FREQ=WEEKLY;INTERVAL=1;BYDAY=" + ",".join(hours["days"]),
            }]})})
        for off in tech["timeOff"]:
            day = days[off["day"]]
            self.dv.action("msdyn_SaveCalendar", {"CalendarEventInfo": json.dumps({
                "CalendarId": calendar, "EntityLogicalName": "bookableresource",
                "TimeZoneCode": tz_code, "InnerCalendarDescription": off["reason"],
                "RulesAndRecurrences": [{"Rules": [{
                    "StartTime": f"{day.isoformat()}T00:00:00.000Z",
                    "EndTime": f"{(day + timedelta(days=1)).isoformat()}T00:00:00.000Z",
                    "Effort": 1, "WorkHourType": 3}]}]})})

    # -------------------------------------------------------------- remove
    def delete(self) -> dict:
        """Remove everything load() created. Shared reference data that the
        environment already had (priorities, price lists) is left alone."""
        d, dv, counts = self.data, self.dv, {}

        def drop(entity_set, key, rows):
            for r in rows:
                dv.delete(entity_set, r[key])
            counts[entity_set] = counts.get(entity_set, 0) + len(rows)

        mine = f"description eq {_q(self.marker)}"
        territories = [r["territoryid"] for r in dv.get(
            "territories", select=["territoryid"], filter=mine)]
        accounts = [r["accountid"] for r in dv.get("accounts", select=["accountid"], filter=mine)]
        contacts = [r["contactid"] for r in dv.get("contacts", select=["contactid"], filter=mine)]

        work_orders = []
        for account in accounts:
            work_orders += dv.get("msdyn_workorders", select=["msdyn_workorderid"],
                                  filter=f"_msdyn_serviceaccount_value eq {account}")
        for wo in work_orders:
            drop("bookableresourcebookings", "bookableresourcebookingid", dv.get(
                "bookableresourcebookings", select=["bookableresourcebookingid"],
                filter=f"_msdyn_workorder_value eq {wo['msdyn_workorderid']}"))
        drop("msdyn_workorders", "msdyn_workorderid", work_orders)
        self.say(f"  removed {len(work_orders)} work orders")

        for contact in contacts:
            for res in dv.get("bookableresources", select=["bookableresourceid"],
                              filter=f"_contactid_value eq {contact}"):
                rid = res["bookableresourceid"]
                drop("bookableresourcebookings", "bookableresourcebookingid", dv.get(
                    "bookableresourcebookings", select=["bookableresourcebookingid"],
                    filter=f"_resource_value eq {rid}"))
                drop("bookableresourcecharacteristics", "bookableresourcecharacteristicid",
                     dv.get("bookableresourcecharacteristics",
                            select=["bookableresourcecharacteristicid"],
                            filter=f"_resource_value eq {rid}"))
                drop("msdyn_resourceterritories", "msdyn_resourceterritoryid", dv.get(
                    "msdyn_resourceterritories", select=["msdyn_resourceterritoryid"],
                    filter=f"_msdyn_resource_value eq {rid}"))
                drop("bookableresourcecategoryassns", "bookableresourcecategoryassnid", dv.get(
                    "bookableresourcecategoryassns", select=["bookableresourcecategoryassnid"],
                    filter=f"_resource_value eq {rid}"))
                dv.delete("bookableresources", rid)
                counts["bookableresources"] = counts.get("bookableresources", 0) + 1
            dv.delete("contacts", contact)
        counts["contacts"] = len(contacts)
        for account in accounts:
            dv.delete("accounts", account)
        counts["accounts"] = len(accounts)

        incident_types = dv.get("msdyn_incidenttypes", select=["msdyn_incidenttypeid"],
                                filter=f"msdyn_description eq {_q(self.marker)}")
        for it in incident_types:
            drop("msdyn_incidenttypecharacteristics", "msdyn_incidenttypecharacteristicid",
                 dv.get("msdyn_incidenttypecharacteristics",
                        select=["msdyn_incidenttypecharacteristicid"],
                        filter=f"_msdyn_incidenttype_value eq {it['msdyn_incidenttypeid']}"))
        drop("msdyn_incidenttypes", "msdyn_incidenttypeid", incident_types)
        drop("msdyn_workordertypes", "msdyn_workordertypeid", dv.get(
            "msdyn_workordertypes", select=["msdyn_workordertypeid"],
            filter=f"msdyn_name eq {_q(d['workOrderType'])}"))
        drop("characteristics", "characteristicid", dv.get(
            "characteristics", select=["characteristicid"], filter=mine))
        drop("msdyn_requirementgroups", "msdyn_requirementgroupid", dv.get(
            "msdyn_requirementgroups", select=["msdyn_requirementgroupid"],
            filter=f"contains(msdyn_name,{_q(self.marker)})"))
        for g in d.get("timeGroups", []):
            gid = self._find("msdyn_timegroups", "msdyn_name", g["name"], "msdyn_timegroupid")
            if gid:
                drop("msdyn_timegroupdetails", "msdyn_timegroupdetailid", dv.get(
                    "msdyn_timegroupdetails", select=["msdyn_timegroupdetailid"],
                    filter=f"_msdyn_timegroupid_value eq {gid}"))
                dv.delete("msdyn_timegroups", gid)
        for role in dv.get("bookableresourcecategories", select=["bookableresourcecategoryid"],
                           filter=mine):
            drop("bookableresourcecategoryassns", "bookableresourcecategoryassnid", dv.get(
                "bookableresourcecategoryassns", select=["bookableresourcecategoryassnid"],
                filter=f"_resourcecategory_value eq {role['bookableresourcecategoryid']}"))
            dv.delete("bookableresourcecategories", role["bookableresourcecategoryid"])
        for u in d.get("organizationalUnits", []):
            uid = self._find("msdyn_organizationalunits", "msdyn_name", u["name"],
                             "msdyn_organizationalunitid")
            if uid:
                dv.delete("msdyn_organizationalunits", uid)
        model = self._find("ratingmodels", "name", d["ratingModel"]["name"], "ratingmodelid")
        if model:
            drop("ratingvalues", "ratingvalueid", dv.get(
                "ratingvalues", select=["ratingvalueid"],
                filter=f"_ratingmodel_value eq {model}"))
            dv.delete("ratingmodels", model)
        # Children before the root.
        root = self._find("territories", "name", d["rootTerritory"], "territoryid")
        for tid in sorted(territories, key=lambda t: t == root):
            dv.delete("territories", tid)
        counts["territories"] = len(territories)
        return counts
