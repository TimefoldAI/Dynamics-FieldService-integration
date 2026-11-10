#!/usr/bin/env python3
"""Generate the field-service demo dataset: a team of biomedical engineers
visiting clinics across London for three working days.

The output is neutral data, not Dataverse records. scripts/demo/loader.py turns it
into standard Field Service records (territories, skills, incident types,
technicians with calendars, customers and work orders). Days are numbered
0..n-1; the loader
places day 0 on a real date when it runs.

Deterministic: re-running produces an identical file.
"""
import json
import pathlib
import random
import sys
from collections import Counter

random.seed(20261102)
OUT = pathlib.Path(__file__).resolve().parent / "fsr_london.json"

DAYS = 3
SHIFT_START, SHIFT_END = "08:00", "17:00"
SHIFT_MINUTES = 9 * 60

# --------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------
# Skills are Field Service characteristics. Proficiency uses its own rating
# model, so a job can ask for "Skilled" and a trainee will not be sent.
RATINGS = [("Trainee", 1), ("Skilled", 2), ("Expert", 3)]
SKILLS = ["Ventilator Servicing", "Dialysis Systems", "Imaging Calibration",
          "Electrical Safety Testing", "Infusion Pumps"]

# Incident types carry the estimated duration and the skill a job needs.
# Field Service copies both onto every work order's resource requirement.
INCIDENT_TYPES = [
    # name,                       minutes, skill,                     level, weight
    ("Ventilator annual service",    90, "Ventilator Servicing",      2, 3),
    ("Dialysis machine repair",     120, "Dialysis Systems",          2, 2),
    ("Imaging unit calibration",    150, "Imaging Calibration",       3, 2),
    ("Infusion pump service",        45, "Infusion Pumps",            1, 4),
    ("Electrical safety check",      30, "Electrical Safety Testing", 1, 5),
]

# msdyn_priority. Higher Level of Importance = more urgent, as in the Field
# Service sample data; routing_rules.yaml says so too.
PRIORITIES = [("Urgent", 9, "D13438"), ("High", 7, "EAA300"),
              ("Normal", 5, "0078D4"), ("Low", 2, "69797E")]

ROOT = "Timefold Demo London"
REGIONS = [
    # key,    territory name,  depot lat, depot lon, bounding box
    ("NORTH", "London North", 51.5540, -0.1100, (51.520, 51.610, -0.230, 0.010)),
    ("SOUTH", "London South", 51.4560, -0.1180, (51.390, 51.500, -0.230, 0.030)),
]

# Engineers: (first, last, region, {skill: level}). Every skill is held by at
# least two engineers per region, so one person's diary is never the only way
# a tightly-windowed job can be done.
TECHNICIANS = [
    ("Nadia", "Rahimi",  "NORTH", {"Ventilator Servicing": 3, "Electrical Safety Testing": 2,
                                   "Infusion Pumps": 2}),
    ("Priyanka", "Shah", "NORTH", {"Imaging Calibration": 3, "Electrical Safety Testing": 2,
                                   "Ventilator Servicing": 2}),
    ("Sinead", "Walsh",  "NORTH", {"Dialysis Systems": 3, "Infusion Pumps": 2,
                                   "Electrical Safety Testing": 1}),
    ("Uma", "Krishnan",  "NORTH", {"Imaging Calibration": 3, "Dialysis Systems": 2,
                                   "Infusion Pumps": 3}),
    ("Owen", "Brennan",  "SOUTH", {"Dialysis Systems": 3, "Electrical Safety Testing": 2,
                                   "Infusion Pumps": 2}),
    ("Rafael", "Duarte", "SOUTH", {"Ventilator Servicing": 3, "Imaging Calibration": 3,
                                   "Electrical Safety Testing": 2}),
    ("Tobias", "Lang",   "SOUTH", {"Ventilator Servicing": 2, "Infusion Pumps": 3,
                                   "Dialysis Systems": 2}),
    ("Victor", "Osei",   "SOUTH", {"Imaging Calibration": 3, "Electrical Safety Testing": 3,
                                   "Infusion Pumps": 1}),
]
# One day of leave, so the board shows time off and the solver has to plan
# around a missing engineer.
TIME_OFF = {("Tobias", "Lang"): [(1, "Annual leave")]}

SITE_PREFIX = ["Ashgrove", "Bellwood", "Carlton", "Dunmore", "Elmfield", "Fairoak",
               "Greenway", "Harlow", "Ivybridge", "Kingsmead", "Larchmont", "Marlow",
               "Northgate", "Oakhurst", "Pinehill", "Quarry Bank", "Redcliffe",
               "Stonebridge", "Thornbury", "Underhill", "Vale Park", "Westmoor"]
SITE_KIND = ["Clinic", "Health Centre", "Surgery", "Care Home", "Day Hospital", "Practice"]
STREETS = ["Road", "Street", "Lane", "Avenue", "Grove", "Terrace"]

# --------------------------------------------------------------------------
technicians = []
depots = {key: (lat, lon) for key, _n, lat, lon, _b in REGIONS}
for first, last, region, skills in TECHNICIANS:
    lat, lon = depots[region]
    technicians.append({
        "firstName": first, "lastName": last, "territory": dict(
            (r[0], r[1]) for r in REGIONS)[region],
        "home": [round(lat + random.uniform(-0.035, 0.035), 5),
                 round(lon + random.uniform(-0.055, 0.055), 5)],
        "skills": skills,
        "workingHours": {"days": ["MO", "TU", "WE", "TH", "FR"],
                         "start": SHIFT_START, "end": SHIFT_END},
        "timeOff": [{"day": d, "reason": why} for d, why in TIME_OFF.get((first, last), [])],
        "_region": region,
    })


def crew(day: int, region: str):
    return [t for t in technicians if t["_region"] == region
            and all(o["day"] != day for o in t["timeOff"])]


# --------------------------------------------------------------------------
# Feasibility: can a day's work be spread over the engineers qualified for it?
# Engineers hold several skills, so the pools compete for the same people and
# a per-skill check passes days that cannot be done. This is a max-flow check:
#   source --minutes of skill--> skill --> engineer --usable minutes--> sink
# --------------------------------------------------------------------------
DRIVABLE_FRACTION = 0.60          # the rest of the day goes on driving


def _max_flow(capacity: dict, source: str, sink: str) -> float:
    residual = {u: dict(vs) for u, vs in capacity.items()}
    for u, vs in capacity.items():
        for v in vs:
            residual.setdefault(v, {}).setdefault(u, 0.0)
    total = 0.0
    while True:
        parents, queue = {source: None}, [source]
        while queue and sink not in parents:
            u = queue.pop(0)
            for v, c in residual.get(u, {}).items():
                if c > 1e-9 and v not in parents:
                    parents[v] = u
                    queue.append(v)
        if sink not in parents:
            return total
        bottleneck, node = float("inf"), sink
        while parents[node] is not None:
            bottleneck = min(bottleneck, residual[parents[node]][node])
            node = parents[node]
        node = sink
        while parents[node] is not None:
            residual[parents[node]][node] -= bottleneck
            residual[node][parents[node]] += bottleneck
            node = parents[node]
        total += bottleneck


def fits(day: int, region: str, load: dict) -> bool:
    demand = {(skill, level): m for (d, r, skill, level), m in load.items()
              if d == day and r == region and m > 0}
    if not demand:
        return True
    people = crew(day, region)
    capacity: dict = {"src": {f"s:{k}": v for k, v in demand.items()}}
    for (skill, level) in demand:
        capacity[f"s:{(skill, level)}"] = {
            f"e:{t['lastName']}": float("inf") for t in people
            if t["skills"].get(skill, 0) >= level}
    for t in people:
        capacity[f"e:{t['lastName']}"] = {"sink": SHIFT_MINUTES * DRIVABLE_FRACTION}
    return _max_flow(capacity, "src", "sink") >= sum(demand.values()) - 1e-6


weighted = [it for it in INCIDENT_TYPES for _ in range(it[4])]
load: dict = {}


def draw(day: int, region: str):
    options = weighted[:]
    random.shuffle(options)
    for name, minutes, skill, level, _w in options:
        key = (day, region, skill, level)
        load[key] = load.get(key, 0.0) + minutes
        if fits(day, region, load):
            return name, minutes
        load[key] -= minutes
    return None


# --------------------------------------------------------------------------
# Customers and work orders
# --------------------------------------------------------------------------
customers, work_orders = [], []
PER_DAY = 34
site_no = 0
for day in range(DAYS):
    for slot in range(PER_DAY):
        key, territory, _lat, _lon, box = REGIONS[slot % len(REGIONS)]
        drawn = draw(day, key)
        if drawn is None:
            continue
        incident, minutes = drawn
        lat_lo, lat_hi, lon_lo, lon_hi = box
        site = f"{SITE_PREFIX[site_no % len(SITE_PREFIX)]} {SITE_KIND[site_no % len(SITE_KIND)]}"
        if site_no >= len(SITE_PREFIX):
            site += f" {site_no // len(SITE_PREFIX) + 1}"
        customers.append({
            "name": site,
            "street": f"{random.randint(1, 180)} {random.choice(SITE_PREFIX)} "
                      f"{random.choice(STREETS)}",
            "city": "London",
            "postalCode": f"{random.choice(['N', 'NW', 'SE', 'SW', 'E', 'W'])}"
                          f"{random.randint(1, 20)} {random.randint(1, 9)}"
                          f"{random.choice('ABDEFGHJLNPQRSTUWXYZ')}"
                          f"{random.choice('ABDEFGHJLNPQRSTUWXYZ')}",
            "country": "United Kingdom",
            "location": [round(random.uniform(lat_lo, lat_hi), 5),
                         round(random.uniform(lon_lo, lon_hi), 5)],
        })
        site_no += 1
        # Most jobs may happen any time that day; a quarter carry a tighter
        # promise to the customer, which is what makes routing hard.
        window = None
        if random.random() < 0.25:
            first = random.choice([8, 9, 10, 13])
            window = [f"{first:02d}:00", f"{min(first + 4, 17):02d}:00"]
        work_orders.append({
            "customer": site, "incidentType": incident, "territory": territory,
            "priority": random.choices([p[0] for p in PRIORITIES], [1, 3, 8, 3])[0],
            "day": day, "timeWindow": window,
        })

# --------------------------------------------------------------------------
# Report and refuse an impossible dataset
# --------------------------------------------------------------------------
problems = []
for day in range(DAYS):
    for key, territory, *_ in REGIONS:
        jobs = [w for w in work_orders if w["day"] == day and w["territory"] == territory]
        minutes = sum(next(i[1] for i in INCIDENT_TYPES if i[0] == w["incidentType"])
                      for w in jobs)
        ok = fits(day, key, load)
        people = crew(day, key)
        print(f"  [{'ok ' if ok else 'LOW'}] day {day} {territory:13} jobs {len(jobs):3} "
              f"service {minutes:5.0f} min  crew {len(people)}")
        if not ok:
            problems.append(f"day {day} {territory}: work cannot be spread over the crew")
for name, _m, skill, level, _w in INCIDENT_TYPES:
    for key, territory, *_ in REGIONS:
        qualified = [t for t in technicians
                     if t["_region"] == key and t["skills"].get(skill, 0) >= level]
        if not qualified:
            problems.append(f"{territory}: nobody can do '{name}'")
if problems:
    print("\nINFEASIBLE:\n  " + "\n  ".join(problems))
    sys.exit(1)

# --------------------------------------------------------------------------
# The other standard Field Service scheduling features, so every one of them
# reaches Timefold. Drawn from a separate generator, so the base scenario above
# stays exactly as it was. Each feature is applied to a few jobs only: enough
# to show it, few enough not to over-constrain the day.
# --------------------------------------------------------------------------
extra = random.Random(7)
ORG_UNITS = [{"name": "St. Elmore North Depot", "location": [51.5540, -0.1100], "region": "NORTH"},
             {"name": "St. Elmore South Depot", "location": [51.4560, -0.1180], "region": "SOUTH"}]
SENIOR = "Senior Engineer"
RATES = {"Nadia": 48, "Priyanka": 52, "Sinead": 44, "Uma": 50,
         "Owen": 45, "Rafael": 55, "Tobias": 42, "Victor": 47}
for t in technicians:
    t["workingHours"]["breaks"] = [["12:00", "12:30"]]          # lunch, in the calendar
    t["hourlyRate"] = RATES[t["firstName"]]
    t["orgUnit"] = next(u["name"] for u in ORG_UNITS if u["region"] == t["_region"])
    t["roles"] = [SENIOR] if t["firstName"] in ("Nadia", "Priyanka", "Rafael", "Victor") else []
    # Field technicians may drive to the first job before 08:00 and home after 17:00.
    t["travelBeforeMinutes"] = 30 if t["firstName"] in ("Owen", "Uma", "Victor") else 0
    t["travelAfterMinutes"] = 30 if t["firstName"] in ("Owen", "Uma", "Victor") else 0
    # Two engineers start and end the day at their depot, not at home.
    t["startAt"] = "orgUnit" if t["firstName"] in ("Uma", "Owen") else "home"

for i, w in enumerate(work_orders):
    w["ref"] = f"WO{i:03d}"
by_name = {f"{t['firstName']} {t['lastName']}": t for t in technicians}


def qualified(w, people=None):
    skill, level = next((i[2], i[3]) for i in INCIDENT_TYPES if i[0] == w["incidentType"])
    return [n for n, t in by_name.items() if t["territory"] == w["territory"]
            and t["skills"].get(skill, 0) >= level and (people is None or n in people)
            and all(o["day"] != w["day"] for o in t["timeOff"])]


open_jobs = [w for w in work_orders if not w["timeWindow"]]
picks = extra.sample(open_jobs, 14)
# Time promised to the customer (instead of a time window): 3 jobs.
for w in picks[0:3]:
    first = extra.choice([8, 9, 13])
    w["promised"] = [f"{first:02d}:00", f"{first + 3:02d}:00"]
# Bookable two-hour slots from a time group: 3 jobs.
for w in picks[3:6]:
    w["timeGroup"] = "Two-hour slots"
# Resource preferences: preferred, restricted, must choose from.
for w in picks[6:9]:
    w["resourcePreferences"] = [{"type": "preferred", "technician": extra.choice(qualified(w))}]
w = picks[9]
q = qualified(w)
w["resourcePreferences"] = [{"type": "restricted", "technician": q[0]}] if len(q) > 1 else []
w = picks[10]
q = qualified(w)
w["resourcePreferences"] = [{"type": "mustChooseFrom", "technician": n} for n in q[:2]]
# A role on top of the skill: 2 jobs need a Senior Engineer.
for w in picks[11:13]:
    if any(SENIOR in by_name[n]["roles"] for n in qualified(w)):
        w["roles"] = [SENIOR]
# An organizational unit: 1 job must be done by the depot's own engineers.
w = picks[13]
w["orgUnits"] = [next(u["name"] for u in ORG_UNITS
                      if u["region"] == ("NORTH" if w["territory"] == "London North" else "SOUTH"))]

# Customers: preferred appointment time (and sometimes day), preferred engineer.
for c in extra.sample(customers, 12):
    c["preferredTime"] = extra.choice([1, 2])                   # Morning / Afternoon
for c in extra.sample(customers, 3):
    w0 = next(w for w in work_orders if w["customer"] == c["name"])
    c["preferredDayOffset"] = w0["day"]                         # the day it is due
for c in extra.sample(customers, 2):
    w0 = next(w for w in work_orders if w["customer"] == c["name"])
    if qualified(w0):
        c["preferredTechnician"] = qualified(w0)[0]

# One job after another at the same site: a safety check, then the pump
# service, by the same engineer (requirement dependency, same resource).
site = extra.choice([c for c in customers if c["location"][0] > 51.52])
n0 = len(work_orders)
check = {"customer": site["name"], "incidentType": "Electrical safety check",
         "territory": "London North", "priority": "Normal", "day": 0, "timeWindow": None,
         "ref": f"WO{n0:03d}"}
pump = {"customer": site["name"], "incidentType": "Infusion pump service",
        "territory": "London North", "priority": "Normal", "day": 0, "timeWindow": None,
        "ref": f"WO{n0 + 1:03d}", "after": {"ref": check["ref"], "sameResource": True}}
# Two engineers at once (requirement group): a dialysis machine installation.
install_site = extra.choice([c for c in customers if c["location"][0] < 51.50])
crew_job = {"customer": install_site["name"], "incidentType": "Dialysis machine repair",
            "territory": "London South", "priority": "High", "day": 0,
            "timeWindow": ["10:00", "15:00"], "ref": f"WO{n0 + 2:03d}", "engineers": 2}
work_orders += [check, pump, crew_job]
for w in (check, pump):
    assert qualified(w), w

for t in technicians:
    del t["_region"]
doc = {
    "scenario": "fsr-london",
    "title": "St. Elmore Field Service: three days of biomedical engineering visits in London",
    "generatedBy": "scripts/demo/generate.py",
    "marker": "Seeded by the Timefold Dynamics 365 demo",
    "timeZone": "Europe/London",
    "dynamicsTimeZoneCode": 85,
    "days": DAYS,
    "rootTerritory": ROOT,
    "territories": [r[1] for r in REGIONS],
    "ratingModel": {"name": "Timefold demo proficiency", "values": RATINGS},
    "skills": SKILLS,
    "priorities": [{"name": n, "level": l, "color": c} for n, l, c in PRIORITIES],
    "workOrderType": "Timefold demo visit",
    "incidentTypes": [{"name": n, "minutes": m, "skill": s, "level": l}
                      for n, m, s, l, _w in INCIDENT_TYPES],
    "organizationalUnits": [{k: u[k] for k in ("name", "location")} for u in ORG_UNITS],
    "roles": [SENIOR],
    "timeGroups": [{"name": "Two-hour slots",
                    "slots": [["08:00", "10:00"], ["10:00", "12:00"],
                              ["13:00", "15:00"], ["15:00", "17:00"]]}],
    "technicians": technicians,
    "customers": customers,
    "workOrders": work_orders,
}
OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n")
print(f"\nwrote {OUT}")
c = Counter(w["day"] for w in work_orders)
print(f"  technicians {len(technicians)}, customers {len(customers)}, "
      f"work orders {len(work_orders)} ({', '.join(f'day {d}: {n}' for d, n in sorted(c.items()))})")
