using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
using Microsoft.Crm.Sdk.Messages;
using Microsoft.Xrm.Sdk;
using Microsoft.Xrm.Sdk.Query;

namespace Timefold.FieldService
{
    public class Shift
    {
        public DateTime Start, End;
        public List<Tuple<DateTime, DateTime>> Breaks = new List<Tuple<DateTime, DateTime>>();
    }

    public class Technician
    {
        public Guid Id;
        public string Name;
        public double[] Start;
        public double[] End;
        public List<string> Territories = new List<string>();
        public SortedDictionary<string, int> Skills = new SortedDictionary<string, int>(StringComparer.Ordinal);
        public List<Shift> Shifts = new List<Shift>();
        public List<string> Tags = new List<string>();            // organizational unit
        public int TravelBeforeMinutes, TravelAfterMinutes;
        public decimal? HourlyRate;
    }

    public class Booking
    {
        public Guid Id, ResourceId, StatusId;
        public int? FsStatus;
        public DateTime Start, End;
        public DateTime? Arrival;
        public bool Underway { get { return FsStatus.HasValue && Extract.Underway.Contains(FsStatus.Value); } }
    }

    public class Visit
    {
        public Guid Id, WorkOrderId;
        public string Name;
        public double[] Location;
        public int DurationMinutes;
        public List<Tuple<DateTime, DateTime>> Windows = new List<Tuple<DateTime, DateTime>>();
        // True when the windows bound only the arrival (promised time, time-group
        // slots): the work may run past their end. Otherwise the whole visit fits.
        public bool ArrivalWindows;
        public string Territory;
        public int? PriorityLevel;
        public SortedDictionary<string, int> RequiredSkills = new SortedDictionary<string, int>(StringComparer.Ordinal);
        public List<Booking> Bookings = new List<Booking>();
        public Booking PinnedBooking { get { return Bookings.FirstOrDefault(b => b.Underway); } }
        public List<Tuple<DateTime, DateTime>> PreferredWindows = new List<Tuple<DateTime, DateTime>>();
        public List<string> RequiredTags = new List<string>();
        public List<Guid> PreferredResources = new List<Guid>(), ProhibitedResources = new List<Guid>(),
                          RequiredResources = new List<Guid>();
        public List<Tuple<Guid, bool>> After = new List<Tuple<Guid, bool>>();   // preceding, same technician
        public Guid? GroupId;
    }

    public class BookingStatus
    {
        public string Name;
        public int? FsStatus;
        public int? Status;
    }

    public class RoutingProblem
    {
        public List<Technician> Technicians = new List<Technician>();
        public List<Visit> Visits = new List<Visit>();
        public Dictionary<Guid, string> Territories = new Dictionary<Guid, string>();
        public Dictionary<Guid, BookingStatus> BookingStatuses = new Dictionary<Guid, BookingStatus>();
        public DateTime WindowStart, WindowEnd;
        /// <summary>The first day planned, as a date in the planning time zone.</summary>
        public DateTime PlanDay;
        public List<string> Skipped = new List<string>();
    }

    /// <summary>
    /// Reads the routing problem out of Field Service's standard tables.
    /// </summary>
    public static class Extract
    {
        public const int WoUnscheduled = 690970000, WoScheduled = 690970001, WoInProgress = 690970002;
        public const int FsScheduled = 690970000, FsTraveling = 690970001, FsOnBreak = 690970002,
                         FsInProgress = 690970003, FsCompleted = 690970004;
        public const int BookingCanceled = 3;
        public const int ResourceAddress = 690970000, OrgUnitAddress = 690970001;
        public const int Preferred = 690970000, Restricted = 690970001, MustChoose = 690970002;
        public const int LocationAgnosticWork = 690970002;
        public static readonly HashSet<int> Underway = new HashSet<int> { FsTraveling, FsOnBreak, FsInProgress };
        private static readonly HashSet<int> Plannable = new HashSet<int> { WoUnscheduled, WoScheduled, WoInProgress };
        private const int Chunk = 200;

        // ----------------------------------------------------------- queries
        public static List<Entity> All(IOrganizationService svc, string entity, string[] columns,
                                       params ConditionExpression[] conditions)
        {
            var query = new QueryExpression(entity) { ColumnSet = new ColumnSet(columns) };
            foreach (var c in conditions) query.Criteria.AddCondition(c);
            query.PageInfo = new PagingInfo { Count = 5000, PageNumber = 1 };
            var rows = new List<Entity>();
            while (true)
            {
                var page = svc.RetrieveMultiple(query);
                rows.AddRange(page.Entities);
                if (!page.MoreRecords) return rows;
                query.PageInfo.PageNumber++;
                query.PageInfo.PagingCookie = page.PagingCookie;
            }
        }

        public static List<Entity> AllIn(IOrganizationService svc, string entity, string[] columns,
                                         string column, IEnumerable<Guid> ids, params ConditionExpression[] extra)
        {
            var list = ids.Where(g => g != Guid.Empty).Distinct().OrderBy(g => g.ToString()).ToList();
            var rows = new List<Entity>();
            for (var i = 0; i < list.Count; i += Chunk)
            {
                var chunk = list.Skip(i).Take(Chunk).Cast<object>().ToArray();
                var conditions = new List<ConditionExpression> { new ConditionExpression(column, ConditionOperator.In, chunk) };
                conditions.AddRange(extra);
                rows.AddRange(All(svc, entity, columns, conditions.ToArray()));
            }
            return rows;
        }

        private static Guid Ref(Entity e, string column)
        {
            var r = e.GetAttributeValue<EntityReference>(column);
            return r == null ? Guid.Empty : r.Id;
        }

        private static int? Option(Entity e, string column)
        {
            var o = e.GetAttributeValue<OptionSetValue>(column);
            return o == null ? (int?)null : o.Value;
        }

        private static double[] Point(Entity e, string lat, string lon)
        {
            var a = e.GetAttributeValue<double?>(lat);
            var b = e.GetAttributeValue<double?>(lon);
            return a.HasValue && b.HasValue ? new[] { a.Value, b.Value } : null;
        }

        private static DateTime? Utc(Entity e, string column)
        {
            var d = e.GetAttributeValue<DateTime?>(column);
            return d.HasValue ? DateTime.SpecifyKind(d.Value.ToUniversalTime(), DateTimeKind.Utc) : (DateTime?)null;
        }

        /// <summary>Local clock ranges ("08:00", "12:00") on every day of a range, as UTC.</summary>
        public static List<Tuple<DateTime, DateTime>> DayWindows(DateTime from, DateTime to,
            List<string[]> clocks, TimeZoneInfo zone)
        {
            var result = new List<Tuple<DateTime, DateTime>>();
            var day = TimeZoneInfo.ConvertTimeFromUtc(from, zone).Date;
            while (TimeZoneInfo.ConvertTimeToUtc(DateTime.SpecifyKind(day, DateTimeKind.Unspecified), zone) < to)
            {
                foreach (var c in clocks)
                {
                    Func<string, DateTime> at = clock => DateTime.SpecifyKind(TimeZoneInfo.ConvertTimeToUtc(
                        DateTime.SpecifyKind(day + TimeSpan.Parse(clock, CultureInfo.InvariantCulture),
                                             DateTimeKind.Unspecified), zone), DateTimeKind.Utc);
                    result.Add(Tuple.Create(at(c[0]), at(c[1])));
                }
                day = day.AddDays(1);
            }
            return result;
        }

        /// <summary>Midnight of a local date in the zone, as UTC.</summary>
        public static DateTime ToUtc(DateTime localDate, TimeZoneInfo zone)
        {
            return DateTime.SpecifyKind(TimeZoneInfo.ConvertTimeToUtc(
                DateTime.SpecifyKind(localDate.Date, DateTimeKind.Unspecified), zone), DateTimeKind.Utc);
        }

        // ------------------------------------------------------------ scope
        public static Dictionary<Guid, string> ScopeTerritories(IOrganizationService svc, string rootName)
        {
            var roots = All(svc, "territory", new[] { "name" }, new ConditionExpression("name", ConditionOperator.Equal, rootName));
            if (roots.Count == 0)
                throw new InvalidPluginExecutionException(
                    "No territory named '" + rootName + "'. Load the demo data or set the tf_RootTerritory environment variable.");
            var children = All(svc, "territory", new[] { "name" },
                new ConditionExpression("parentterritoryid", ConditionOperator.Equal, roots[0].Id));
            return (children.Count > 0 ? children : roots).ToDictionary(r => r.Id, r => r.GetAttributeValue<string>("name"));
        }

        // ----------------------------------------------------------- extract
        /// <summary>The step Run is on, so an unexpected failure says where it happened.</summary>
        [ThreadStatic] public static string Step;

        public static RoutingProblem Run(IOrganizationService svc, string rootTerritory, DateTime? windowStart, int days,
                                         Dictionary<string, object> rules = null)
        {
            var prefs = Json.Obj(Json.Get(rules ?? new Dictionary<string, object>(), "preferences"));
            var zone = TimeZoneInfo.Utc;
            var zoneName = Json.Str(prefs, "windowsTimeZone");
            if (!string.IsNullOrEmpty(zoneName)) zone = TimeZoneInfo.FindSystemTimeZoneById(zoneName);
            var appointmentTimes = new Dictionary<int, string[]>
                { { 1, new[] { "08:00", "12:00" } }, { 2, new[] { "12:00", "17:00" } }, { 3, new[] { "17:00", "20:00" } } };
            var configured = Json.Obj(Json.Get(prefs, "appointmentTimes"));
            if (configured.Count > 0)
                appointmentTimes = configured.ToDictionary(kv => int.Parse(kv.Key, CultureInfo.InvariantCulture),
                    kv => Json.Arr(kv.Value).Select(x => (string)x).ToArray());
            Step = "territories";
            var p = new RoutingProblem { Territories = ScopeTerritories(svc, rootTerritory) };

            Step = "reference data";
            var characteristic = All(svc, "characteristic", new[] { "name" })
                .ToDictionary(r => r.Id, r => r.GetAttributeValue<string>("name"));
            var rating = All(svc, "ratingvalue", new[] { "value" })
                .ToDictionary(r => r.Id, r => r.GetAttributeValue<int?>("value") ?? 0);
            var priority = All(svc, "msdyn_priority", new[] { "msdyn_levelofimportance" })
                .ToDictionary(r => r.Id, r => Option(r, "msdyn_levelofimportance"));   // a choice column
            foreach (var s in All(svc, "bookingstatus", new[] { "name", "msdyn_fieldservicestatus", "status" }))
                p.BookingStatuses[s.Id] = new BookingStatus
                {
                    Name = s.GetAttributeValue<string>("name"),
                    FsStatus = Option(s, "msdyn_fieldservicestatus"),
                    Status = Option(s, "status")
                };

            Step = "requirements";
            // ---- requirements in scope
            var requirements = AllIn(svc, "msdyn_resourcerequirement",
                new[] { "msdyn_name", "msdyn_workorder", "msdyn_duration", "msdyn_latitude", "msdyn_longitude",
                        "msdyn_fromdate", "msdyn_todate", "msdyn_timewindowstart", "msdyn_timewindowend",
                        "msdyn_territory", "msdyn_priority", "msdyn_timefrompromised", "msdyn_timetopromised",
                        "msdyn_timegroup", "msdyn_worklocation", "msdyn_requirementgroupid" },
                "msdyn_territory", p.Territories.Keys,
                new ConditionExpression("statecode", ConditionOperator.Equal, 0),
                new ConditionExpression("msdyn_workorder", ConditionOperator.NotNull));
            var workOrders = AllIn(svc, "msdyn_workorder",
                new[] { "msdyn_name", "msdyn_systemstatus", "msdyn_primaryincidenttype", "msdyn_serviceaccount", "msdyn_priority" },
                "msdyn_workorderid", requirements.Select(r => Ref(r, "msdyn_workorder"))).ToDictionary(w => w.Id);
            var incidents = AllIn(svc, "msdyn_incidenttype", new[] { "msdyn_name" }, "msdyn_incidenttypeid",
                workOrders.Values.Select(w => Ref(w, "msdyn_primaryincidenttype")))
                .ToDictionary(r => r.Id, r => r.GetAttributeValue<string>("msdyn_name"));
            var accounts = AllIn(svc, "account", new[] { "name", "preferredappointmenttimecode",
                    "preferredappointmentdaycode", "msdyn_preferredresource" }, "accountid",
                workOrders.Values.Select(w => Ref(w, "msdyn_serviceaccount"))).ToDictionary(r => r.Id);
            var slots = new Dictionary<Guid, List<string[]>>();
            foreach (var d in AllIn(svc, "msdyn_timegroupdetail", new[] { "msdyn_timegroupid", "msdyn_starttime", "msdyn_endtime" },
                         "msdyn_timegroupid", requirements.Select(r => Ref(r, "msdyn_timegroup"))))
            {
                var a = Utc(d, "msdyn_starttime");
                var b = Utc(d, "msdyn_endtime");
                if (!a.HasValue || !b.HasValue) continue;
                var g = Ref(d, "msdyn_timegroupid");
                if (!slots.ContainsKey(g)) slots[g] = new List<string[]>();
                slots[g].Add(new[] { a.Value.ToString("HH:mm", CultureInfo.InvariantCulture),
                                     b.Value.ToString("HH:mm", CultureInfo.InvariantCulture) });
            }

            Func<Entity, List<Tuple<DateTime, DateTime>>> windowsOf = r =>
            {
                // The customer promise wins; then the time window; then the date window.
                var s = Utc(r, "msdyn_timefrompromised");
                var e = Utc(r, "msdyn_timetopromised");
                if (s.HasValue && e.HasValue) return new List<Tuple<DateTime, DateTime>> { Tuple.Create(s.Value, e.Value) };
                s = Utc(r, "msdyn_timewindowstart");
                e = Utc(r, "msdyn_timewindowend");
                if (s.HasValue && e.HasValue) return new List<Tuple<DateTime, DateTime>> { Tuple.Create(s.Value, e.Value) };
                s = Utc(r, "msdyn_fromdate");
                e = Utc(r, "msdyn_todate");
                if (s.HasValue && e.HasValue) return new List<Tuple<DateTime, DateTime>> { Tuple.Create(s.Value, e.Value) };
                return new List<Tuple<DateTime, DateTime>>();
            };

            if (!windowStart.HasValue)
            {
                var today = TimeZoneInfo.ConvertTimeFromUtc(DateTime.UtcNow, zone).Date;
                var starts = requirements.SelectMany(windowsOf).Where(w => w.Item2 >= ToUtc(today, zone))
                    .Select(w => w.Item1).OrderBy(d => d).ToList();
                windowStart = starts.Count > 0 ? TimeZoneInfo.ConvertTimeFromUtc(starts[0], zone).Date : today;
            }
            // The planning day runs from midnight to midnight in the planning time
            // zone (windowsTimeZone), not in UTC.
            p.PlanDay = DateTime.SpecifyKind(windowStart.Value.Date, DateTimeKind.Utc);
            p.WindowStart = ToUtc(p.PlanDay, zone);
            p.WindowEnd = ToUtc(p.PlanDay.AddDays(days), zone);

            Step = "visits";
            // ---- visits
            var visits = new Dictionary<Guid, Visit>();
            foreach (var r in requirements)
            {
                Entity wo;
                workOrders.TryGetValue(Ref(r, "msdyn_workorder"), out wo);
                var label = (wo != null ? wo.GetAttributeValue<string>("msdyn_name") : null)
                            ?? r.GetAttributeValue<string>("msdyn_name") ?? r.Id.ToString();
                var status = wo != null ? Option(wo, "msdyn_systemstatus") : null;
                if (!status.HasValue || !Plannable.Contains(status.Value)) continue;
                var location = Point(r, "msdyn_latitude", "msdyn_longitude");
                if (location == null) { p.Skipped.Add(label + ": no latitude/longitude"); continue; }
                if (Option(r, "msdyn_worklocation") == LocationAgnosticWork)
                { p.Skipped.Add(label + ": location-agnostic work needs no route"); continue; }
                var all = windowsOf(r);
                var windows = all.Where(w => w.Item1 < p.WindowEnd && w.Item2 > p.WindowStart)
                    .Select(w => Tuple.Create(w.Item1 > p.WindowStart ? w.Item1 : p.WindowStart,
                                              w.Item2 < p.WindowEnd ? w.Item2 : p.WindowEnd)).ToList();
                if (all.Count > 0 && windows.Count == 0) continue;
                List<string[]> groupSlots;
                var promised = Utc(r, "msdyn_timefrompromised").HasValue && Utc(r, "msdyn_timetopromised").HasValue;
                if (slots.TryGetValue(Ref(r, "msdyn_timegroup"), out groupSlots) && !promised)
                {
                    // A time group offers fixed slots: any one of them on any allowed day.
                    var span = windows.Count > 0 ? windows
                        : new List<Tuple<DateTime, DateTime>> { Tuple.Create(p.WindowStart, p.WindowEnd) };
                    windows = span.SelectMany(w => DayWindows(w.Item1, w.Item2, groupSlots, TimeZoneInfo.Utc)
                            .Where(x => x.Item1 < w.Item2 && x.Item2 > w.Item1)
                            .Select(x => Tuple.Create(x.Item1 > w.Item1 ? x.Item1 : w.Item1, x.Item2 < w.Item2 ? x.Item2 : w.Item2))
                            .Where(x => x.Item1 < x.Item2))
                        .OrderBy(x => x.Item1).ThenBy(x => x.Item2).ToList();
                }
                var duration = r.GetAttributeValue<int?>("msdyn_duration");
                if (!duration.HasValue || duration.Value == 0) { p.Skipped.Add(label + ": no duration"); continue; }
                string incident = null, account = null;
                Entity acc = null;
                if (wo != null)
                {
                    incidents.TryGetValue(Ref(wo, "msdyn_primaryincidenttype"), out incident);
                    accounts.TryGetValue(Ref(wo, "msdyn_serviceaccount"), out acc);
                    account = acc != null ? acc.GetAttributeValue<string>("name") : null;
                }
                var preferred = new List<Tuple<DateTime, DateTime>>();
                var timeCode = acc != null ? Option(acc, "preferredappointmenttimecode") : null;
                var dayCode = acc != null ? Option(acc, "preferredappointmentdaycode") : null;
                if ((timeCode.HasValue && appointmentTimes.ContainsKey(timeCode.Value)) || dayCode.HasValue)
                {
                    var clocks = new List<string[]> { timeCode.HasValue && appointmentTimes.ContainsKey(timeCode.Value)
                        ? appointmentTimes[timeCode.Value] : new[] { "00:00", "23:59" } };
                    foreach (var w in DayWindows(p.WindowStart, p.WindowEnd, clocks, zone))
                    {
                        var weekday = (int)TimeZoneInfo.ConvertTimeFromUtc(w.Item1, zone).DayOfWeek;  // Sunday is 0, as in Dynamics
                        if (dayCode.HasValue && weekday != dayCode.Value) continue;
                        preferred.Add(w);
                    }
                }
                string territory;
                p.Territories.TryGetValue(Ref(r, "msdyn_territory"), out territory);
                var prioId = Ref(r, "msdyn_priority");
                if (prioId == Guid.Empty && wo != null) prioId = Ref(wo, "msdyn_priority");
                int? level;
                priority.TryGetValue(prioId, out level);
                visits[r.Id] = new Visit
                {
                    Id = r.Id,
                    WorkOrderId = Ref(r, "msdyn_workorder"),
                    Name = string.Join(" · ", new[] { label, incident, account }.Where(x => !string.IsNullOrEmpty(x))),
                    Location = location,
                    DurationMinutes = duration.Value,
                    Windows = windows,
                    ArrivalWindows = windows.Count > 0 && (promised || groupSlots != null),
                    Territory = territory,
                    PriorityLevel = level,
                    PreferredWindows = preferred,
                    GroupId = Ref(r, "msdyn_requirementgroupid") == Guid.Empty ? (Guid?)null : Ref(r, "msdyn_requirementgroupid")
                };
                if (acc != null && Ref(acc, "msdyn_preferredresource") != Guid.Empty)
                    visits[r.Id].PreferredResources.Add(Ref(acc, "msdyn_preferredresource"));
            }

            // ---- who may, may not, or should do the job
            Step = "resource preferences";
            var byWo = visits.Values.GroupBy(v => v.WorkOrderId).ToDictionary(g => g.Key, g => g.ToList());
            var byAccount = visits.Values.Where(v => workOrders.ContainsKey(v.WorkOrderId)
                    && Ref(workOrders[v.WorkOrderId], "msdyn_serviceaccount") != Guid.Empty)
                .GroupBy(v => Ref(workOrders[v.WorkOrderId], "msdyn_serviceaccount")).ToDictionary(g => g.Key, g => g.ToList());
            var prefCols = new[] { "msdyn_resourcerequirement", "msdyn_workorder", "msdyn_account", "msdyn_bookableresource",
                                   "msdyn_preferencetype", "msdyn_expirationdate" };
            var prefRows = AllIn(svc, "msdyn_requirementresourcepreference", prefCols, "msdyn_resourcerequirement", visits.Keys,
                                 new ConditionExpression("statecode", ConditionOperator.Equal, 0))
                .Concat(AllIn(svc, "msdyn_requirementresourcepreference", prefCols, "msdyn_workorder", byWo.Keys,
                              new ConditionExpression("statecode", ConditionOperator.Equal, 0)))
                .Concat(AllIn(svc, "msdyn_requirementresourcepreference", prefCols, "msdyn_account", byAccount.Keys,
                              new ConditionExpression("statecode", ConditionOperator.Equal, 0)));
            var seenPrefs = new HashSet<string>();
            foreach (var r in prefRows)
            {
                var resource = Ref(r, "msdyn_bookableresource");
                var expires = Utc(r, "msdyn_expirationdate");
                var key = Ref(r, "msdyn_resourcerequirement") + "|" + Ref(r, "msdyn_workorder") + "|" + Ref(r, "msdyn_account")
                          + "|" + resource + "|" + Option(r, "msdyn_preferencetype");
                if (!seenPrefs.Add(key) || resource == Guid.Empty || (expires.HasValue && expires.Value < DateTime.UtcNow)) continue;
                List<Visit> targets;
                Visit single;
                if (visits.TryGetValue(Ref(r, "msdyn_resourcerequirement"), out single)) targets = new List<Visit> { single };
                else if (!byWo.TryGetValue(Ref(r, "msdyn_workorder"), out targets)
                         && !byAccount.TryGetValue(Ref(r, "msdyn_account"), out targets)) targets = new List<Visit>();
                var type = Option(r, "msdyn_preferencetype");
                foreach (var v in targets)
                {
                    var list = type == Preferred ? v.PreferredResources : type == Restricted ? v.ProhibitedResources
                             : type == MustChoose ? v.RequiredResources : null;
                    if (list != null && !list.Contains(resource)) list.Add(resource);
                }
            }

            // ---- roles and organizational units a job needs
            Step = "roles and units";
            var categoryNames = All(svc, "bookableresourcecategory", new[] { "name" })
                .ToDictionary(r => r.Id, r => r.GetAttributeValue<string>("name"));
            foreach (var r in AllIn(svc, "msdyn_requirementresourcecategory", new[] { "msdyn_resourcerequirement", "msdyn_resourcecategory" },
                         "msdyn_resourcerequirement", visits.Keys))
            {
                Visit v; string role;
                if (visits.TryGetValue(Ref(r, "msdyn_resourcerequirement"), out v) &&
                    categoryNames.TryGetValue(Ref(r, "msdyn_resourcecategory"), out role))
                    v.RequiredSkills["Role: " + role] = 0;
            }
            var unitNames = All(svc, "msdyn_organizationalunit", new[] { "msdyn_name" })
                .ToDictionary(r => r.Id, r => r.GetAttributeValue<string>("msdyn_name"));
            foreach (var r in AllIn(svc, "msdyn_requirementorganizationunit", new[] { "msdyn_resourcerequirement", "msdyn_organizationalunit" },
                         "msdyn_resourcerequirement", visits.Keys))
            {
                Visit v; string unit;
                if (visits.TryGetValue(Ref(r, "msdyn_resourcerequirement"), out v) &&
                    unitNames.TryGetValue(Ref(r, "msdyn_organizationalunit"), out unit) &&
                    !v.RequiredTags.Contains("Org unit: " + unit))
                    v.RequiredTags.Add("Org unit: " + unit);
            }

            // ---- one job after another
            Step = "dependencies";
            foreach (var r in AllIn(svc, "msdyn_requirementdependency",
                         new[] { "msdyn_requirement", "msdyn_dependentrequirement", "msdyn_requiresameresource" },
                         "msdyn_dependentrequirement", visits.Keys, new ConditionExpression("statecode", ConditionOperator.Equal, 0)))
            {
                Visit v;
                if (visits.TryGetValue(Ref(r, "msdyn_dependentrequirement"), out v) && visits.ContainsKey(Ref(r, "msdyn_requirement")))
                    v.After.Add(Tuple.Create(Ref(r, "msdyn_requirement"), r.GetAttributeValue<bool?>("msdyn_requiresameresource") ?? false));
            }

            Step = "requirement skills";
            foreach (var rc in AllIn(svc, "msdyn_requirementcharacteristic",
                         new[] { "msdyn_resourcerequirement", "msdyn_characteristic", "msdyn_ratingvalue" },
                         "msdyn_resourcerequirement", visits.Keys))
            {
                Visit v;
                string name;
                if (visits.TryGetValue(Ref(rc, "msdyn_resourcerequirement"), out v) &&
                    characteristic.TryGetValue(Ref(rc, "msdyn_characteristic"), out name))
                {
                    int level;
                    rating.TryGetValue(Ref(rc, "msdyn_ratingvalue"), out level);
                    v.RequiredSkills[name] = level;
                }
            }

            Step = "bookings";
            foreach (var b in AllIn(svc, "bookableresourcebooking",
                         new[] { "msdyn_resourcerequirement", "resource", "bookingstatus", "starttime", "endtime", "msdyn_estimatedarrivaltime" },
                         "msdyn_resourcerequirement", visits.Keys,
                         new ConditionExpression("statecode", ConditionOperator.Equal, 0)))
            {
                BookingStatus st;
                p.BookingStatuses.TryGetValue(Ref(b, "bookingstatus"), out st);
                st = st ?? new BookingStatus();
                if (st.Status == BookingCanceled) continue;
                Visit v;
                if (!visits.TryGetValue(Ref(b, "msdyn_resourcerequirement"), out v)) continue;
                if (st.FsStatus == FsCompleted) { visits.Remove(v.Id); continue; }
                v.Bookings.Add(new Booking
                {
                    Id = b.Id, ResourceId = Ref(b, "resource"), StatusId = Ref(b, "bookingstatus"),
                    FsStatus = st.FsStatus, Start = Utc(b, "starttime").Value, End = Utc(b, "endtime").Value,
                    Arrival = Utc(b, "msdyn_estimatedarrivaltime")
                });
            }

            Step = "technicians";
            // ---- technicians
            var membership = new Dictionary<Guid, List<string>>();
            foreach (var m in AllIn(svc, "msdyn_resourceterritory", new[] { "msdyn_resource", "msdyn_territory" },
                         "msdyn_territory", p.Territories.Keys, new ConditionExpression("statecode", ConditionOperator.Equal, 0)))
            {
                var rid = Ref(m, "msdyn_resource");
                if (!membership.ContainsKey(rid)) membership[rid] = new List<string>();
                membership[rid].Add(p.Territories[Ref(m, "msdyn_territory")]);
            }
            var resources = AllIn(svc, "bookableresource",
                new[] { "name", "contactid", "userid", "msdyn_organizationalunit", "msdyn_startlocation", "msdyn_endlocation", "calendarid",
                        "msdyn_displayonscheduleboard", "msdyn_travelbeforeworkinghoursminutes",
                        "msdyn_travelafterworkinghoursminutes", "msdyn_hourlyrate" },
                "bookableresourceid", membership.Keys, new ConditionExpression("statecode", ConditionOperator.Equal, 0));

            Step = "locations";
            var addresses = new Dictionary<Guid, double[]>();
            foreach (var c in AllIn(svc, "contact", new[] { "address1_latitude", "address1_longitude" }, "contactid",
                         resources.Select(r => Ref(r, "contactid"))))
                addresses[c.Id] = Point(c, "address1_latitude", "address1_longitude");
            foreach (var u in AllIn(svc, "systemuser", new[] { "address1_latitude", "address1_longitude" }, "systemuserid",
                         resources.Select(r => Ref(r, "userid"))))
                addresses[u.Id] = Point(u, "address1_latitude", "address1_longitude");
            foreach (var o in AllIn(svc, "msdyn_organizationalunit", new[] { "msdyn_latitude", "msdyn_longitude" },
                         "msdyn_organizationalunitid", resources.Select(r => Ref(r, "msdyn_organizationalunit"))))
                addresses[o.Id] = Point(o, "msdyn_latitude", "msdyn_longitude");

            Func<Entity, int?, double[]> locate = (r, choice) =>
            {
                double[] point = null;
                if (choice == ResourceAddress)
                {
                    var who = Ref(r, "contactid") != Guid.Empty ? Ref(r, "contactid") : Ref(r, "userid");
                    addresses.TryGetValue(who, out point);
                }
                else if (choice == OrgUnitAddress)
                    addresses.TryGetValue(Ref(r, "msdyn_organizationalunit"), out point);
                return point;
            };

            Step = "calendars";
            var technicians = new Dictionary<Guid, Technician>();
            foreach (var r in resources.OrderBy(x => x.GetAttributeValue<string>("name") ?? "", StringComparer.Ordinal))
            {
                if (r.GetAttributeValue<bool?>("msdyn_displayonscheduleboard") == false) continue;   // hidden from scheduling
                var name = r.GetAttributeValue<string>("name") ?? r.Id.ToString();
                var start = locate(r, Option(r, "msdyn_startlocation"));
                if (start == null)
                {
                    p.Skipped.Add(name + ": start location is location-agnostic or has no latitude/longitude");
                    continue;
                }
                var tech = new Technician
                {
                    Id = r.Id, Name = name, Start = start,
                    End = locate(r, Option(r, "msdyn_endlocation")) ?? start,
                    Territories = membership[r.Id].OrderBy(x => x, StringComparer.Ordinal).ToList(),
                    TravelBeforeMinutes = r.GetAttributeValue<int?>("msdyn_travelbeforeworkinghoursminutes") ?? 0,
                    TravelAfterMinutes = r.GetAttributeValue<int?>("msdyn_travelafterworkinghoursminutes") ?? 0,
                    HourlyRate = r.GetAttributeValue<Money>("msdyn_hourlyrate") != null
                        ? r.GetAttributeValue<Money>("msdyn_hourlyrate").Value : (decimal?)null
                };
                string techUnit;
                if (unitNames.TryGetValue(Ref(r, "msdyn_organizationalunit"), out techUnit)) tech.Tags.Add("Org unit: " + techUnit);
                tech.Shifts = WorkingTime(svc, Ref(r, "calendarid"), p.WindowStart, p.WindowEnd, zone);
                if (tech.Shifts.Count == 0) p.Skipped.Add(name + ": no working hours in the planning window");
                technicians[tech.Id] = tech;
            }

            Step = "technician skills";
            foreach (var c in AllIn(svc, "bookableresourcecharacteristic", new[] { "resource", "characteristic", "ratingvalue" },
                         "resource", technicians.Keys, new ConditionExpression("statecode", ConditionOperator.Equal, 0)))
            {
                Technician t;
                string name;
                if (technicians.TryGetValue(Ref(c, "resource"), out t) &&
                    characteristic.TryGetValue(Ref(c, "characteristic"), out name))
                {
                    int level;
                    rating.TryGetValue(Ref(c, "ratingvalue"), out level);
                    t.Skills[name] = level;
                }
            }

            foreach (var a in AllIn(svc, "bookableresourcecategoryassn", new[] { "resource", "resourcecategory" },
                         "resource", technicians.Keys, new ConditionExpression("statecode", ConditionOperator.Equal, 0)))
            {
                Technician t; string role;
                if (technicians.TryGetValue(Ref(a, "resource"), out t) && categoryNames.TryGetValue(Ref(a, "resourcecategory"), out role))
                    t.Skills["Role: " + role] = 0;
            }

            p.Technicians = technicians.Values.Where(t => t.Shifts.Count > 0).ToList();
            p.Visits = visits.Values.OrderBy(v => v.Name, StringComparer.Ordinal).ToList();
            return p;
        }

        /// <summary>
        /// Working blocks from the resource calendar, via ExpandCalendar: the
        /// platform's own flattening of working hours, time off and closures,
        /// which is what the schedule board shows as available time.
        /// </summary>
        public static List<Shift> WorkingTime(IOrganizationService svc, Guid calendarId, DateTime start, DateTime end,
                                              TimeZoneInfo zone = null)
        {
            var result = new List<Shift>();
            if (calendarId == Guid.Empty) return result;
            var response = (ExpandCalendarResponse)svc.Execute(
                new ExpandCalendarRequest { CalendarId = calendarId, Start = start, End = end });
            Func<DateTime?, DateTime> utc = d => DateTime.SpecifyKind(d.Value.ToUniversalTime(), DateTimeKind.Utc);
            // Blocks belong to the same working day by the local date, not the UTC date.
            zone = zone ?? TimeZoneInfo.Utc;
            Func<DateTime, DateTime> localDay = d => TimeZoneInfo.ConvertTimeFromUtc(d, zone).Date;
            var blocks = response.result.Where(b => b.TimeCode == TimeCode.Available)
                .Select(b => Tuple.Create(utc(b.Start), utc(b.End))).OrderBy(b => b.Item1).ThenBy(b => b.Item2).ToList();
            var breaks = response.result.Where(b => b.TimeCode == TimeCode.Unavailable && b.SubCode == SubCode.Break)
                .Select(b => Tuple.Create(utc(b.Start), utc(b.End))).OrderBy(b => b.Item1).ToList();
            var merged = new List<DateTime[]>();
            foreach (var b in blocks)
            {
                var last = merged.LastOrDefault();
                if (last != null && localDay(b.Item1) == localDay(last[0]))
                    last[1] = b.Item2 > last[1] ? b.Item2 : last[1];
                else
                    merged.Add(new[] { b.Item1, b.Item2 });
            }
            foreach (var m in merged)
            {
                if (m[1] <= m[0]) continue;
                var inside = breaks.Where(b => b.Item1 < m[1] && b.Item2 > m[0])
                    .Select(b => Tuple.Create(b.Item1 > m[0] ? b.Item1 : m[0], b.Item2 < m[1] ? b.Item2 : m[1])).ToList();
                // gaps between the day's own blocks are breaks too
                var day = blocks.Where(b => localDay(b.Item1) == localDay(m[0])).ToList();
                for (var i = 0; i + 1 < day.Count; i++)
                    if (day[i + 1].Item1 > day[i].Item2 && !inside.Any(x => x.Item1 <= day[i].Item2 && x.Item2 >= day[i + 1].Item1))
                        inside.Add(Tuple.Create(day[i].Item2, day[i + 1].Item1));
                result.Add(new Shift { Start = m[0], End = m[1],
                    Breaks = inside.Distinct().OrderBy(x => x.Item1).ThenBy(x => x.Item2).ToList() });
            }
            return result;
        }
    }
}
