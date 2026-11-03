using System;
using System.Collections.Generic;
using System.Linq;
using Microsoft.Xrm.Sdk;

namespace Timefold.FieldService
{
    /// <summary>
    /// Writes the plan as ordinary bookings: the records the schedule board and
    /// Resource Scheduling Optimization write.
    /// </summary>
    public static class WriteBack
    {
        public class Result
        {
            public int Created, Updated, Removed, Unscheduled, Pinned, Unchanged;
            public bool Incomplete;              // ran out of time; the rest is written by the next hand-off
            public List<string> Errors = new List<string>();
        }

        private const int Committed = 2, Solid = 1;

        public static Guid ScheduledStatus(RoutingProblem p, Dictionary<string, object> rules)
        {
            var wanted = (Json.Str(Json.Obj(Json.Get(rules, "writeBack")), "scheduledStatusName") ?? "").ToLowerInvariant();
            var candidates = p.BookingStatuses.Where(s => s.Value.FsStatus == Extract.FsScheduled && s.Value.Status == Committed).ToList();
            if (candidates.Count == 0)
                throw new InvalidPluginExecutionException(
                    "No booking status maps to the Field Service status 'Scheduled'. Check Booking Statuses.");
            var named = candidates.Where(s => wanted != "" && (s.Value.Name ?? "").ToLowerInvariant() == wanted).ToList();
            return (named.Count > 0 ? named[0] : candidates[0]).Key;
        }

        public static Result Run(IOrganizationService svc, RoutingProblem p, List<Translate.Assignment> assignments,
                                 List<string> unassigned, Dictionary<string, object> rules, Func<bool> timeUp = null)
        {
            timeUp = timeUp ?? (() => false);
            var method = (int)Json.Long(Json.Obj(Json.Get(rules, "writeBack")), "bookingMethod", 192350000);
            var status = ScheduledStatus(p, rules);
            var techOfVehicle = Translate.VehicleIds(p).ToDictionary(kv => kv.Value, kv => kv.Key);
            var visits = p.Visits.ToDictionary(v => v.Id.ToString());
            var result = new Result();
            var creates = new List<Entity>();
            var updates = new List<Entity>();
            var deletes = new List<Guid>();

            foreach (var a in assignments)
            {
                Visit visit;
                if (!visits.TryGetValue(a.VisitId, out visit)) continue;
                if (visit.PinnedBooking != null) { result.Pinned++; continue; }
                Guid resource;
                var serviceStart = a.StartService ?? a.Arrival;
                if (!techOfVehicle.TryGetValue(a.Vehicle, out resource) || !serviceStart.HasValue)
                {
                    result.Errors.Add(visit.Name + ": no technician or start time");
                    continue;
                }
                var start = serviceStart.Value.AddMinutes(-a.TravelMinutes);
                var end = a.Departure ?? serviceStart.Value.AddMinutes(visit.DurationMinutes);
                var booking = new Entity("bookableresourcebooking");
                booking["name"] = visit.Name.Length > 100 ? visit.Name.Substring(0, 100) : visit.Name;
                booking["resource"] = new EntityReference("bookableresource", resource);
                booking["bookingstatus"] = new EntityReference("bookingstatus", status);
                booking["starttime"] = start;
                booking["endtime"] = end;
                booking["duration"] = (int)Math.Round((end - start).TotalMinutes);
                booking["msdyn_estimatedarrivaltime"] = serviceStart.Value;
                booking["msdyn_estimatedtravelduration"] = a.TravelMinutes;
                booking["msdyn_bookingmethod"] = new OptionSetValue(method);
                if (a.TravelMeters.HasValue) booking["msdyn_milestraveled"] = Math.Round(a.TravelMeters.Value / 1609.344, 2);

                var existing = visit.Bookings.Where(b => !b.Underway).ToList();
                if (existing.Count == 1 && existing[0].ResourceId == resource && existing[0].Start == start
                    && existing[0].End == end && existing[0].StatusId == status)
                {
                    result.Unchanged++;              // already as planned (e.g. written before a hand-off)
                    continue;
                }
                // Field Service refuses to move a requirement-group booking to another
                // resource by update ("use Rebook"): replace it instead.
                if (existing.Count > 0 && visit.GroupId.HasValue && existing[0].ResourceId != resource)
                {
                    deletes.AddRange(existing.Select(b => b.Id));
                    existing.Clear();
                }
                if (existing.Count > 0)
                {
                    booking.Id = existing[0].Id;
                    updates.Add(booking);
                    deletes.AddRange(existing.Skip(1).Select(b => b.Id));
                }
                else
                {
                    booking["bookingtype"] = new OptionSetValue(Solid);
                    booking["msdyn_resourcerequirement"] = new EntityReference("msdyn_resourcerequirement", visit.Id);
                    booking["msdyn_workorder"] = new EntityReference("msdyn_workorder", visit.WorkOrderId);
                    creates.Add(booking);
                }
            }

            foreach (var id in unassigned)
            {
                Visit visit;
                if (!visits.TryGetValue(id, out visit) || visit.PinnedBooking != null) continue;
                result.Unscheduled++;
                deletes.AddRange(visit.Bookings.Select(b => b.Id));
            }

            // Remove first, so a technician's row never briefly shows a visit twice. Each
            // write checks the clock: Dataverse stops a plug-in after two minutes.
            foreach (var id in deletes)
            {
                if (timeUp()) { result.Incomplete = true; return result; }
                Try(result, "remove booking " + id, () => { svc.Delete("bookableresourcebooking", id); result.Removed++; });
            }
            foreach (var b in updates)
            {
                if (timeUp()) { result.Incomplete = true; return result; }
                Try(result, "update booking " + b.Id, () => { svc.Update(b); result.Updated++; });
            }
            foreach (var b in creates)
            {
                if (timeUp()) { result.Incomplete = true; return result; }
                Try(result, "book " + b["name"], () => { svc.Create(b); result.Created++; });
            }
            return result;
        }

        private static void Try(Result result, string what, Action action)
        {
            try { action(); }
            catch (Exception e) { result.Errors.Add(what + ": " + e.Message); }
        }
    }
}
