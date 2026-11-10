using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using System.Text.RegularExpressions;

namespace Timefold.FieldService
{
    /// <summary>
    /// Builds the Timefold Field Service Routing request and reads the plan
    /// back.
    /// Schema: https://app.timefold.ai/openapis/field-service-routing/v1
    /// </summary>
    public static class Translate
    {
        public static string Iso(DateTime value)
        {
            return value.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture);
        }

        private static string Day(DateTime value)
        {
            return value.ToUniversalTime().ToString("yyyy-MM-dd", CultureInfo.InvariantCulture);
        }

        /// <summary>bookableresourceid -> the vehicle id Timefold sees: the name.</summary>
        public static Dictionary<Guid, string> VehicleIds(RoutingProblem p)
        {
            var counts = p.Technicians.GroupBy(t => t.Name).ToDictionary(g => g.Key, g => g.Count());
            return p.Technicians.ToDictionary(t => t.Id,
                t => counts[t.Name] == 1 ? t.Name : t.Name + " (" + t.Id.ToString().Substring(0, 8) + ")");
        }

        public static string ShiftId(string vehicle, DateTime start)
        {
            return vehicle + "::" + Day(start);
        }

        public static string Priority(int? level, Dictionary<string, object> rules)
        {
            if (!level.HasValue) return null;
            var cfg = Json.Obj(Json.Get(rules, "priority"));
            var value = level.Value;
            if (Json.Bool(cfg, "higherLevelIsMoreImportant", true)) value = 11 - value;
            return Math.Min(10, Math.Max(1, value)).ToString(CultureInfo.InvariantCulture);
        }

        private static List<object> Point(double[] p)
        {
            return new List<object> { p[0], p[1] };
        }

        // Promised times and time-group slots bound the arrival: start inside the
        // window, then take as long as the job takes. Timefold wants windows that do
        // not overlap, so slots that touch become one (08-10 and 10-12 allow the same
        // arrivals as 08-12), and a job may run on only until the next slot opens, so
        // no arrival falls in a gap between slots. -> (min start, max start, max end)
        public static List<Tuple<DateTime, DateTime, DateTime>> ArrivalWindows(
            List<Tuple<DateTime, DateTime>> windows, int minutes)
        {
            var merged = new List<DateTime[]>();
            foreach (var w in windows.OrderBy(x => x.Item1).ThenBy(x => x.Item2))
            {
                if (merged.Count > 0 && w.Item1 <= merged[merged.Count - 1][1])
                {
                    var last = merged[merged.Count - 1];
                    if (w.Item2 > last[1]) last[1] = w.Item2;
                }
                else merged.Add(new[] { w.Item1, w.Item2 });
            }
            var result = new List<Tuple<DateTime, DateTime, DateTime>>();
            for (var i = 0; i < merged.Count; i++)
            {
                var end = merged[i][1].AddMinutes(minutes);
                if (i + 1 < merged.Count && merged[i + 1][0] < end) end = merged[i + 1][0];
                result.Add(Tuple.Create(merged[i][0], merged[i][1], end));
            }
            return result;
        }

        // Timefold stops early once the score stops improving; the rules' optional
        // termination block (e.g. unimprovedSpentLimit) changes that.
        private static Dictionary<string, object> Termination(Dictionary<string, object> rules, int solverSeconds)
        {
            var termination = new Dictionary<string, object> { { "spentLimit", "PT" + Math.Max(1, solverSeconds) + "S" } };
            foreach (var kv in Json.Obj(Json.Get(rules, "termination"))) termination[kv.Key] = kv.Value;
            return termination;
        }

        public static Dictionary<string, object> BuildRequest(RoutingProblem p, Dictionary<string, object> rules,
                                                              int solverSeconds, string runName)
        {
            var routing = Json.Obj(Json.Get(rules, "routingRules"));
            var overtime = (int)Json.Long(routing, "maxOvertimeMinutes", 30);
            var matchTerritory = Json.Bool(routing, "restrictToOwnTerritory", true);
            var names = VehicleIds(p);

            // Work under way stays put: first in its technician's itinerary.
            var pinned = new Dictionary<string, List<Tuple<DateTime, string>>>();
            foreach (var v in p.Visits)
            {
                var b = v.PinnedBooking;
                if (b == null || !names.ContainsKey(b.ResourceId)) continue;
                var tech = p.Technicians.First(t => t.Id == b.ResourceId);
                var at = b.Arrival ?? b.Start;
                var shift = tech.Shifts.FirstOrDefault(s => s.Start <= at && at < s.End)
                            ?? tech.Shifts.FirstOrDefault(s => s.Start.Date == at.Date);
                if (shift == null) continue;
                var sid = ShiftId(names[tech.Id], shift.Start);
                if (!pinned.ContainsKey(sid)) pinned[sid] = new List<Tuple<DateTime, string>>();
                pinned[sid].Add(Tuple.Create(at, v.Id.ToString()));
            }

            var vehicles = new List<object>();
            foreach (var t in p.Technicians)
            {
                var shifts = new List<object>();
                foreach (var s in t.Shifts)
                {
                    var sid = ShiftId(names[t.Id], s.Start);
                    int before = t.TravelBeforeMinutes, after = t.TravelAfterMinutes;
                    var shift = new Dictionary<string, object>
                    {
                        { "id", sid },
                        { "startLocation", Point(t.Start) },
                        { "endLocation", Point(t.End ?? t.Start) },
                        { "minStartTime", Iso(s.Start.AddMinutes(-before)) },
                        { "maxSoftEndTime", Iso(s.End.AddMinutes(after)) },
                        { "maxEndTime", Iso(s.End.AddMinutes(after + overtime)) }
                    };
                    // Travel before working hours: set off early, start work on time.
                    if (before > 0) shift["minFirstVisitArrivalTime"] = Iso(s.Start);
                    // Travel after working hours: finish work on time, drive home after.
                    if (after > 0)
                    {
                        shift["maxSoftLastVisitDepartureTime"] = Iso(s.End);
                        shift["maxLastVisitDepartureTime"] = Iso(s.End.AddMinutes(overtime));
                    }
                    var shiftTags = (matchTerritory ? t.Territories : new List<string>())
                        .Concat(t.Tags.OrderBy(x => x, StringComparer.Ordinal)).ToList();
                    if (shiftTags.Count > 0) shift["tags"] = shiftTags.Cast<object>().ToList();
                    // Timefold's levels start at 1; a role, or a characteristic without a rating, counts as 1.
                    if (t.Skills.Count > 0)
                        shift["skills"] = t.Skills.Select(kv => (object)new Dictionary<string, object>
                            { { "name", kv.Key }, { "level", Math.Max(1, kv.Value) } }).ToList();
                    if (s.Breaks.Count > 0)
                        shift["requiredBreaks"] = s.Breaks.Select((b, i) => (object)new Dictionary<string, object>
                            { { "id", sid + "::break" + (i + 1) }, { "type", "FIXED" },
                              { "startTime", Iso(b.Item1) }, { "endTime", Iso(b.Item2) } }).ToList();
                    if (t.HourlyRate.HasValue && t.HourlyRate.Value > 0)
                        shift["cost"] = new Dictionary<string, object> { { "rates", new List<object> {
                            new Dictionary<string, object> { { "duration", "PT24H" },
                                { "costPerUnit", (double)Math.Round(t.HourlyRate.Value, 2) }, { "unit", "HOUR" } } } } };
                    List<Tuple<DateTime, string>> items;
                    if (pinned.TryGetValue(sid, out items))
                        shift["itinerary"] = items.OrderBy(x => x.Item1).ThenBy(x => x.Item2, StringComparer.Ordinal)
                            .Select(x => (object)new Dictionary<string, object>
                                { { "id", x.Item2 }, { "kind", "VISIT" }, { "startServiceTime", Iso(x.Item1) } }).ToList();
                    shifts.Add(shift);
                }
                if (shifts.Count > 0)
                    vehicles.Add(new Dictionary<string, object> { { "id", names[t.Id] }, { "shifts", shifts } });
            }
            var pinnedIds = new HashSet<string>(pinned.Values.SelectMany(x => x).Select(x => x.Item2));
            var inScope = new HashSet<Guid>(p.Visits.Select(v => v.Id));
            Func<List<Guid>, List<string>> vehiclesOf = ids => ids.Where(names.ContainsKey).Select(i => names[i])
                .Distinct().OrderBy(x => x, StringComparer.Ordinal).ToList();

            var built = new Dictionary<Guid, Dictionary<string, object>>();
            foreach (var v in p.Visits)
            {
                var visit = new Dictionary<string, object>
                {
                    { "id", v.Id.ToString() },
                    { "name", v.Name },
                    { "location", Point(v.Location) },
                    { "serviceDuration", "PT" + v.DurationMinutes + "M" }
                };
                if (v.ArrivalWindows)
                    visit["timeWindows"] = ArrivalWindows(v.Windows, v.DurationMinutes).Select(w => (object)new Dictionary<string, object>
                        { { "minStartTime", Iso(w.Item1) }, { "maxStartTime", Iso(w.Item2) }, { "maxEndTime", Iso(w.Item3) } }).ToList();
                else if (v.Windows.Count > 0)
                    visit["timeWindows"] = v.Windows.Select(w => (object)new Dictionary<string, object>
                        { { "minStartTime", Iso(w.Item1) }, { "maxEndTime", Iso(w.Item2) } }).ToList();
                if (v.PreferredWindows.Count > 0)
                    visit["preferredTimeWindows"] = v.PreferredWindows.Select(w => (object)new Dictionary<string, object>
                        { { "minStartTime", Iso(w.Item1) }, { "maxEndTime", Iso(w.Item2) } }).ToList();
                if (v.RequiredSkills.Count > 0)
                    visit["requiredSkills"] = v.RequiredSkills.Select(kv => kv.Value != 0
                        ? (object)new Dictionary<string, object> { { "name", kv.Key }, { "minLevel", kv.Value } }
                        : new Dictionary<string, object> { { "name", kv.Key } }).ToList();
                var vtags = (matchTerritory && v.Territory != null ? new List<string> { v.Territory } : new List<string>())
                    .Concat(v.RequiredTags.OrderBy(x => x, StringComparer.Ordinal)).ToList();
                if (vtags.Count > 0) visit["requiredTags"] = vtags.Cast<object>().ToList();
                var required = vehiclesOf(v.RequiredResources);
                if (required.Count > 0) visit["requiredVehicles"] = required.Cast<object>().ToList();
                var prohibited = vehiclesOf(v.ProhibitedResources);
                if (prohibited.Count > 0) visit["prohibitedVehicles"] = prohibited.Cast<object>().ToList();
                var preferredV = vehiclesOf(v.PreferredResources).Where(x => !prohibited.Contains(x)).ToList();
                if (preferredV.Count > 0) visit["preferredVehicles"] = preferredV.Cast<object>().ToList();
                var deps = v.After.OrderBy(a => a.Item1.ToString(), StringComparer.Ordinal).ThenBy(a => a.Item2)
                    .Where(a => inScope.Contains(a.Item1))
                    .Select(a =>
                    {
                        var d = new Dictionary<string, object>
                            { { "id", a.Item1 + "->" + v.Id }, { "precedingVisit", a.Item1.ToString() } };
                        if (a.Item2) d["coordination"] = "SAME_VEHICLE";
                        return (object)d;
                    }).ToList();
                if (deps.Count > 0) visit["visitDependencies"] = deps;
                var prio = Priority(v.PriorityLevel, rules);
                if (prio != null) visit["priority"] = prio;
                if (pinnedIds.Contains(v.Id.ToString())) visit["pinningRequested"] = true;
                built[v.Id] = visit;
            }

            // Several technicians on one job at once: a requirement group with at least two
            // requirements in scope becomes a visit group; its visits move into it.
            var visitGroups = new List<object>();
            foreach (var g in p.Visits.Where(v => v.GroupId.HasValue).GroupBy(v => v.GroupId.Value)
                         .OrderBy(g => g.Key.ToString(), StringComparer.Ordinal))
            {
                var ids = g.Select(v => v.Id).ToList();
                if (ids.Count < 2) continue;
                visitGroups.Add(new Dictionary<string, object>
                    { { "id", g.Key.ToString() }, { "visits", ids.Select(i => (object)built[i]).ToList() } });
                foreach (var i in ids) built.Remove(i);
            }
            var visits = p.Visits.Where(v => built.ContainsKey(v.Id)).Select(v => (object)built[v.Id]).ToList();
            var allVisits = visits.Concat(visitGroups.SelectMany(g =>
                (List<object>)((Dictionary<string, object>)g)["visits"])).ToList();

            var tags = new SortedSet<string>(StringComparer.Ordinal);
            var skills = new SortedSet<string>(StringComparer.Ordinal);
            foreach (Dictionary<string, object> veh in vehicles)
                foreach (Dictionary<string, object> s in (List<object>)veh["shifts"])
                {
                    foreach (var tag in Json.Arr(Json.Get(s, "tags"))) tags.Add((string)tag);
                    foreach (Dictionary<string, object> sk in Json.Arr(Json.Get(s, "skills"))) skills.Add((string)sk["name"]);
                }
            foreach (Dictionary<string, object> vis in allVisits)
            {
                foreach (var tag in Json.Arr(Json.Get(vis, "requiredTags"))) tags.Add((string)tag);
                foreach (Dictionary<string, object> sk in Json.Arr(Json.Get(vis, "requiredSkills"))) skills.Add((string)sk["name"]);
            }

            var modelInput = new Dictionary<string, object>
            {
                { "vehicles", vehicles },
                { "visits", visits },
                { "tags", tags.Select(t => (object)new Dictionary<string, object> { { "name", t } }).ToList() },
                { "skills", skills.Cast<object>().ToList() },
                { "planningWindow", new Dictionary<string, object>
                    { { "startDate", Iso(p.WindowStart) }, { "endDate", Iso(p.WindowEnd) } } }
            };
            if (visitGroups.Count > 0) modelInput["visitGroups"] = visitGroups;
            var config = new Dictionary<string, object>
            {
                { "run", new Dictionary<string, object>
                    {
                        { "name", runName ?? "Dynamics 365 Field Service " + Day(p.PlanDay) },
                        { "termination", Termination(rules, solverSeconds) }
                    } }
            };
            var weights = Json.Obj(Json.Get(rules, "constraintWeights"));
            if (weights.Count > 0)
                config["model"] = new Dictionary<string, object> { { "overrides", weights } };
            return new Dictionary<string, object> { { "config", config }, { "modelInput", modelInput } };
        }

        // ------------------------------------------------- Timefold -> plan
        public class Assignment
        {
            public string VisitId, Vehicle, ShiftId;
            public DateTime? Arrival, StartService, Departure;
            public int TravelMinutes;
            public double? TravelMeters;
        }

        public static List<Assignment> Itineraries(Dictionary<string, object> response, out List<string> unassigned)
        {
            var output = Json.Obj(Json.Get(response, "modelOutput"));
            var result = new List<Assignment>();
            foreach (Dictionary<string, object> vehicle in Json.Arr(Json.Get(output, "vehicles")))
                foreach (Dictionary<string, object> shift in Json.Arr(Json.Get(vehicle, "shifts")))
                    foreach (Dictionary<string, object> item in Json.Arr(Json.Get(shift, "itinerary")))
                    {
                        if (Json.Str(item, "kind") != "VISIT") continue;
                        double meters;
                        result.Add(new Assignment
                        {
                            VisitId = Json.Str(item, "id"),
                            Vehicle = Json.Str(vehicle, "id"),
                            ShiftId = Json.Str(shift, "id"),
                            Arrival = Time(Json.Str(item, "arrivalTime")),
                            StartService = Time(Json.Str(item, "startServiceTime")),
                            Departure = Time(Json.Str(item, "departureTime")),
                            TravelMinutes = Minutes(Json.Str(item, "travelTimeFromPreviousStandstill")),
                            TravelMeters = double.TryParse(Json.Str(item, "travelDistanceMetersFromPreviousStandstill"),
                                NumberStyles.Float, CultureInfo.InvariantCulture, out meters) ? meters : (double?)null
                        });
                    }
            unassigned = Json.Arr(Json.Get(output, "unassignedVisits")).Select(x => Convert.ToString(x)).ToList();
            return result;
        }

        public static DateTime? Time(string value)
        {
            if (string.IsNullOrEmpty(value)) return null;
            return DateTime.Parse(value, CultureInfo.InvariantCulture,
                DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
        }

        /// <summary>ISO 8601 duration (PT1H2M3S) to whole minutes, rounded.</summary>
        public static int Minutes(string duration)
        {
            if (string.IsNullOrEmpty(duration)) return 0;
            var m = Regex.Match(duration, @"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:([\d.]+)S)?$");
            if (!m.Success) return 0;
            Func<int, double> g = i => m.Groups[i].Success
                ? double.Parse(m.Groups[i].Value, CultureInfo.InvariantCulture) : 0;
            return (int)Math.Round(g(1) * 1440 + g(2) * 60 + g(3) + g(4) / 60, MidpointRounding.ToEven);
        }
    }
}
