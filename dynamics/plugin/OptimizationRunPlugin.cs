using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Globalization;
using System.Linq;
using System.Threading;
using Microsoft.Xrm.Sdk;
using Microsoft.Xrm.Sdk.Query;

namespace Timefold.FieldService
{
    /// <summary>
    /// Runs a Timefold optimization entirely inside Dynamics 365.
    ///
    /// Creating a "Timefold optimization run" row (tf_optimizationrun) starts
    /// it: this plug-in, registered asynchronously on Create, reads Field
    /// Service, sends the request to Timefold, waits for the solver and writes
    /// the plan back as bookings.
    ///
    /// Dataverse stops any plug-in after two minutes, and stops a chain of
    /// plug-ins that trigger each other at depth 8. A run that needs longer is
    /// advanced in steps, each a fresh call of the tf_ProcessRun custom API
    /// (ProcessRunApi below): the "Timefold: drive optimization runs" flow calls
    /// it every 20 seconds until the run ends, and the progress dialog calls it
    /// too while it is open. For short runs the plug-in also hands itself on
    /// (it bumps tf_checks; the same plug-in, registered asynchronously on
    /// Update of tf_checks, picks it up) while that chain is shallow enough.
    /// A lock on the row (tf_lockeduntil) makes sure only one of them works on
    /// a run at a time. Each step is written to the row, so the progress
    /// dialog and the run's form show exactly where it is.
    /// </summary>
    public class OptimizationRunPlugin : IPlugin
    {
        public const string Table = "tf_optimizationrun";
        public const int Queued = 100000000, Solving = 100000001, Writing = 100000002,
                         Completed = 100000003, FailedStage = 100000004, DryRunDone = 100000005;
        private const int PollBudgetSeconds = 70;       // leaves time to write back inside 2 minutes
        private const int WriteBudgetSeconds = 105;     // stop writing here; the next step writes the rest
        private const int StepWriteBudgetSeconds = 80;  // a custom API call answers inside the flow's 2 minutes
        private const int LockSeconds = 150;            // a step that died lets go after this
        private const int MaxChainDepth = 6;            // Dataverse stops plug-in chains at depth 8
        private const int GraceSeconds = 3600;          // past the solver time, give up after this
        private const int StartSeconds = 900;           // a run still Queued after this never started

        public void Execute(IServiceProvider serviceProvider)
        {
            var context = (IPluginExecutionContext)serviceProvider.GetService(typeof(IPluginExecutionContext));
            var trace = (ITracingService)serviceProvider.GetService(typeof(ITracingService));
            var factory = (IOrganizationServiceFactory)serviceProvider.GetService(typeof(IOrganizationServiceFactory));
            // Run as SYSTEM: the dispatcher who clicks the button may not hold
            // every privilege (reading calendars, creating bookings) the run needs.
            var svc = factory.CreateOrganizationService(null);
            if (context.PrimaryEntityName != Table) return;

            // Hand on to the next step through Update of tf_checks only while the chain
            // stays clear of Dataverse's depth limit; past it the flow and the dialog carry on.
            var chain = context.Depth < MaxChainDepth;
            if (context.MessageName == "Create") Advance(svc, trace, context.PrimaryEntityId, chain, true);
            else if (context.MessageName == "Update") Advance(svc, trace, context.PrimaryEntityId, chain, false);
        }

        /// <summary>
        /// One step of a run: start it (Queued), check on the solver (Solving)
        /// or write a batch of bookings (Writing). Returns the stage after the
        /// step, and false in busy when another caller holds the run.
        /// </summary>
        public static int Advance(IOrganizationService svc, ITracingService trace, Guid id, bool chain,
                                  bool start, out bool busy)
        {
            var run = new Run(svc, trace, id, chain);
            busy = false;
            if (run.Final) return run.Stage;
            // A step caller (start false) also ends a run that never started, so the flow stops.
            var neverStarted = !start && run.Stage == Queued && run.Age > StartSeconds;
            if (start != (run.Stage == Queued) && !neverStarted) return run.Stage;     // not this caller's step
            if (!run.Claim()) { busy = true; return run.Stage; }
            try
            {
                if (run.SettingsError != null) run.Fail(run.SettingsError);
                else if (neverStarted)
                    run.Fail("The run did not start within " + StartSeconds / 60 + " minutes. Check that the " +
                             "plug-in step on Create of " + Table + " is enabled, then start a new run.");
                else if (run.Stage == Queued) run.Begin();
                else if (run.Overdue)
                    run.Fail("Timefold took more than an hour longer than the solver time. Try again, or lower the solver time.");
                else if (run.Stage == Writing) run.Finish();
                else run.Poll();
            }
            catch (InvalidPluginExecutionException e)
            {
                trace.Trace(e.ToString());
                run.Fail(e.Message);
            }
            catch (Exception e)
            {
                // Unexpected: keep where it happened, so the run's log is enough to report it.
                trace.Trace(e.ToString());
                var where = (e.StackTrace ?? "").Split('\n').FirstOrDefault(l => l.Contains("Timefold.FieldService"));
                run.Fail(e.GetType().Name + ": " + e.Message + (where != null ? " (" + where.Trim() + ")" : "") +
                         (Extract.Step != null ? " [reading " + Extract.Step + "]" : ""));
            }
            finally { run.Release(); }
            return run.Stage;
        }

        private static void Advance(IOrganizationService svc, ITracingService trace, Guid id, bool chain, bool start)
        {
            bool busy;
            Advance(svc, trace, id, chain, start, out busy);
        }

        // ------------------------------------------------------------------
        private class Run
        {
            private readonly IOrganizationService svc;
            private readonly ITracingService trace;
            private readonly Entity row;
            private readonly Stopwatch clock = Stopwatch.StartNew();
            private readonly Settings settings;
            private readonly bool chain;       // false: a custom API step, answer quickly
            private string log;
            private bool claimed;

            public int Stage { get { return row.GetAttributeValue<OptionSetValue>("tf_stage")?.Value ?? Queued; } }
            public int Checks { get { return row.GetAttributeValue<int?>("tf_checks") ?? 0; } }
            public bool Final { get { return Stage == Completed || Stage == FailedStage || Stage == DryRunDone; } }

            /// <summary>Set when the environment variables could not be read; the run fails with it.</summary>
            public readonly string SettingsError;

            /// <summary>Seconds since the run was created.</summary>
            public double Age
            {
                get
                {
                    var created = row.GetAttributeValue<DateTime?>("createdon") ?? DateTime.UtcNow;
                    return (DateTime.UtcNow - created.ToUniversalTime()).TotalSeconds;
                }
            }

            public bool Overdue
            {
                get
                {
                    var seconds = row.GetAttributeValue<int?>("tf_solverseconds") ?? settings?.SolverSeconds ?? 0;
                    return Age > seconds + GraceSeconds;
                }
            }

            public Run(IOrganizationService svc, ITracingService trace, Guid id, bool chain)
            {
                this.svc = svc;
                this.trace = trace;
                this.chain = chain;
                row = svc.Retrieve(Table, id, new ColumnSet("tf_name", "tf_plandate", "tf_days", "tf_solverseconds",
                    "tf_dryrun", "tf_stage", "tf_timefoldjobid", "tf_checks", "tf_log", "tf_score",
                    "tf_lockeduntil", "createdon"));
                log = row.GetAttributeValue<string>("tf_log") ?? "";
                try { settings = Settings.Read(svc); }
                catch (Exception e)
                {
                    // For example invalid JSON in tf_RoutingRules: say so on the run, don't leave it Queued.
                    trace.Trace(e.ToString());
                    SettingsError = "Could not read the Timefold environment variables: " + e.Message;
                }
            }

            /// <summary>Take the run for this step, unless someone else has it.</summary>
            public bool Claim()
            {
                var until = row.GetAttributeValue<DateTime?>("tf_lockeduntil");
                if (until.HasValue && until.Value.ToUniversalTime() > DateTime.UtcNow) return false;
                var take = new Entity(Table, row.Id) { RowVersion = row.RowVersion };
                take["tf_lockeduntil"] = DateTime.UtcNow.AddSeconds(LockSeconds);
                try
                {
                    // Only if nobody changed the row since we read it: two callers never both win.
                    svc.Execute(new Microsoft.Xrm.Sdk.Messages.UpdateRequest
                    {
                        Target = take, ConcurrencyBehavior = ConcurrencyBehavior.IfRowVersionMatches
                    });
                }
                catch (System.ServiceModel.FaultException<OrganizationServiceFault> e)
                {
                    trace.Trace("run is busy: " + e.Message);
                    return false;
                }
                claimed = true;
                return true;
            }

            public void Release()
            {
                if (!claimed) return;
                try { svc.Update(new Entity(Table, row.Id) { ["tf_lockeduntil"] = null }); }
                catch (Exception e) { trace.Trace("could not release the run: " + e); }
            }

            private void Save(Dictionary<string, object> changes, string message = null)
            {
                var update = new Entity(Table, row.Id);
                foreach (var kv in changes) { update[kv.Key] = kv.Value; row[kv.Key] = kv.Value; }
                if (message != null)
                {
                    trace.Trace(message);
                    log += DateTime.UtcNow.ToString("HH:mm:ss", CultureInfo.InvariantCulture) + " UTC  " + message + "\n";
                    if (log.Length > 99000) log = log.Substring(log.Length - 99000);
                    update["tf_log"] = log;
                }
                svc.Update(update);
            }

            private void Say(string message)
            {
                Save(new Dictionary<string, object>(), message);
            }

            private RoutingProblem ReadProblem()
            {
                var date = row.GetAttributeValue<DateTime?>("tf_plandate");
                DateTime? day = null;
                if (date.HasValue) day = DateTime.SpecifyKind(date.Value.Date, DateTimeKind.Utc);
                var days = Math.Max(1, row.GetAttributeValue<int?>("tf_days") ?? 1);
                return Extract.Run(svc, settings.RootTerritory, day, days, settings.Rules);
            }

            public void Begin()
            {
                Say("Reading Field Service (territory '" + settings.RootTerritory + "')");
                var problem = ReadProblem();
                foreach (var s in problem.Skipped) Say("Skipped " + s);
                Say(string.Format(CultureInfo.InvariantCulture,
                    "Planning {0:yyyy-MM-dd}: {1} work orders, {2} technicians, {3} already under way",
                    problem.PlanDay, problem.Visits.Count, problem.Technicians.Count,
                    problem.Visits.Count(v => v.PinnedBooking != null)));
                if (problem.Visits.Count == 0) { Fail("Nothing to plan: no open work orders on that day."); return; }

                var seconds = row.GetAttributeValue<int?>("tf_solverseconds") ?? settings.SolverSeconds;
                var request = Json.Write(Translate.BuildRequest(problem, settings.Rules, seconds,
                    row.GetAttributeValue<string>("tf_name")));
                var changes = new Dictionary<string, object>
                {
                    { "tf_request", request.Length <= 1048576 ? request : request.Substring(0, 1048576) },
                    { "tf_visits", problem.Visits.Count }
                };
                if (row.GetAttributeValue<bool>("tf_dryrun"))
                {
                    changes["tf_stage"] = new OptionSetValue(DryRunDone);
                    Save(changes, "Dry run: the Timefold request is on this record. Nothing was sent.");
                    return;
                }
                Save(changes, "Sending " + problem.Visits.Count + " visits to Timefold (" + seconds + " s solver time)");
                using (var tf = settings.Timefold())
                {
                    var job = tf.Submit(request);
                    Save(new Dictionary<string, object>
                    {
                        { "tf_timefoldjobid", job }, { "tf_stage", new OptionSetValue(Solving) }
                    }, "Timefold job " + job + " is solving");
                    PollWith(tf);
                }
            }

            public void Poll()
            {
                using (var tf = settings.Timefold()) PollWith(tf);
            }

            private void PollWith(TimefoldClient tf)
            {
                var job = row.GetAttributeValue<string>("tf_timefoldjobid");
                var before = row.GetAttributeValue<string>("tf_score");
                var budget = chain ? PollBudgetSeconds : 0;     // a custom API step checks once
                while (true)
                {
                    var meta = tf.Metadata(job);
                    var status = Json.Str(meta, "solverStatus");
                    var score = Json.Str(meta, "score");
                    if (score != null && score != row.GetAttributeValue<string>("tf_score"))
                        Save(new Dictionary<string, object> { { "tf_score", score } });
                    if (TimefoldClient.Failed.Contains(status))
                    {
                        Fail("Timefold ended the job as " + status + ": " +
                             (Json.Str(meta, "failureMessage") ?? Json.Write(Json.Get(meta, "validationResult"))));
                        return;
                    }
                    if (TimefoldClient.Done.Contains(status)) break;
                    if (clock.Elapsed.TotalSeconds >= budget)
                    {
                        HandOff(Solving, score != null && score != before ? "Still solving (" + score + ")" : null);
                        return;
                    }
                    Thread.Sleep(5000);
                }
                if (clock.Elapsed.TotalSeconds > PollBudgetSeconds) { HandOff(Writing, "Solved; writing bookings next"); return; }
                Finish(tf);
            }

            public void Finish()
            {
                using (var tf = settings.Timefold()) Finish(tf);
            }

            private void Finish(TimefoldClient tf)
            {
                var job = row.GetAttributeValue<string>("tf_timefoldjobid");
                var response = tf.Result(job);
                var meta = Json.Obj(Json.Get(response, "metadata"));
                List<string> unassigned;
                var assignments = Translate.Itineraries(response, out unassigned);
                // Said once per run: later write steps continue the same Timefold job.
                if (!log.Contains("left open. Writing bookings."))
                    Save(new Dictionary<string, object> { { "tf_stage", new OptionSetValue(Writing) } },
                        "Solved, score " + Json.Str(meta, "score") + ": " + assignments.Count + " visits planned, " +
                        unassigned.Count + " left open. Writing bookings.");
                else
                    Say("Continuing to write bookings for Timefold job " + job);

                var validation = tf.Validation(job);
                foreach (Dictionary<string, object> issue in Json.Arr(Json.Get(validation, "issues")))
                    Say("Timefold " + (Json.Str(issue, "severity") ?? "NOTE") + ": " +
                        (Json.Str(issue, "legacyMessage") ?? Json.Str(issue, "code")));

                // Read Field Service again: bookings may have changed while solving.
                var problem = ReadProblem();
                var writeBudget = chain ? WriteBudgetSeconds : StepWriteBudgetSeconds;
                var result = WriteBack.Run(svc, problem, assignments, unassigned, settings.Rules,
                                           () => clock.Elapsed.TotalSeconds > writeBudget);
                foreach (var e in result.Errors.Take(20)) Say("Error: " + e);
                if (result.Incomplete)
                {
                    HandOff(Writing, string.Format(CultureInfo.InvariantCulture,
                        "Wrote {0} bookings so far ({1} created, {2} updated, {3} removed); continuing",
                        result.Created + result.Updated + result.Removed, result.Created, result.Updated, result.Removed));
                    return;
                }
                Save(new Dictionary<string, object>
                {
                    { "tf_stage", new OptionSetValue(result.Errors.Count == 0 ? Completed : FailedStage) },
                    { "tf_score", Json.Str(meta, "score") },
                    { "tf_scheduled", assignments.Count },
                    { "tf_unscheduled", unassigned.Count }
                }, string.Format(CultureInfo.InvariantCulture,
                    "Done: {0} bookings created, {1} updated, {2} removed, {3} already as planned, " +
                    "{4} under way left alone, {5} errors. Open the schedule board to see the routes.",
                    result.Created, result.Updated, result.Removed, result.Unchanged, result.Pinned, result.Errors.Count));
            }

            private void HandOff(int stage, string message)
            {
                // Release first: the step this update may start needs the lock.
                var changes = new Dictionary<string, object> { { "tf_stage", new OptionSetValue(stage) } };
                if (claimed) { changes["tf_lockeduntil"] = null; claimed = false; }
                // Updating tf_checks starts this plug-in again, with a fresh two minutes,
                // while the chain is shallow; otherwise the flow or the dialog takes the next step.
                if (chain) changes["tf_checks"] = Checks + 1;
                Save(changes, message);
            }

            public void Fail(string message)
            {
                try
                {
                    Save(new Dictionary<string, object> { { "tf_stage", new OptionSetValue(FailedStage) } }, "Failed: " + message);
                }
                catch (Exception e) { trace.Trace("could not record failure: " + e); }
            }
        }
    }

    /// <summary>
    /// The tf_ProcessRun custom API: one step of a run, called by the
    /// "Timefold: drive optimization runs" flow and by the progress dialog.
    /// Each call is a fresh request, so a run can take as long as Timefold
    /// needs. In: RunId. Out: Stage (the tf_stage value after the step), Busy.
    /// </summary>
    public class ProcessRunApi : IPlugin
    {
        public void Execute(IServiceProvider serviceProvider)
        {
            var context = (IPluginExecutionContext)serviceProvider.GetService(typeof(IPluginExecutionContext));
            var trace = (ITracingService)serviceProvider.GetService(typeof(ITracingService));
            var factory = (IOrganizationServiceFactory)serviceProvider.GetService(typeof(IOrganizationServiceFactory));
            var svc = factory.CreateOrganizationService(null);
            var id = (Guid)context.InputParameters["RunId"];
            bool busy;
            var stage = OptimizationRunPlugin.Advance(svc, trace, id, false, false, out busy);
            context.OutputParameters["Stage"] = stage;
            context.OutputParameters["Busy"] = busy;
        }
    }

    /// <summary>
    /// Settings live in Dataverse environment variables, so an administrator
    /// changes them in the solution, without a release:
    ///   tf_TimefoldApiKey, tf_TimefoldBaseUrl, tf_RootTerritory,
    ///   tf_SolverSeconds, tf_RoutingRules (JSON).
    /// </summary>
    public class Settings
    {
        public string ApiKey, BaseUrl, RootTerritory;
        public int SolverSeconds;
        public Dictionary<string, object> Rules;

        public TimefoldClient Timefold()
        {
            return new TimefoldClient(ApiKey, BaseUrl);
        }

        public static Settings Read(IOrganizationService svc)
        {
            var values = new Dictionary<string, string>();
            var query = new QueryExpression("environmentvariabledefinition")
            {
                ColumnSet = new ColumnSet("schemaname", "defaultvalue"),
                Criteria = { Conditions = { new ConditionExpression("schemaname", ConditionOperator.BeginsWith, "tf_") } }
            };
            var link = query.AddLink("environmentvariablevalue", "environmentvariabledefinitionid",
                                     "environmentvariabledefinitionid", JoinOperator.LeftOuter);
            link.Columns = new ColumnSet("value");
            link.EntityAlias = "v";
            foreach (var d in svc.RetrieveMultiple(query).Entities)
            {
                var current = d.GetAttributeValue<AliasedValue>("v.value");
                var value = current != null ? current.Value as string : null;
                values[d.GetAttributeValue<string>("schemaname")] = string.IsNullOrEmpty(value)
                    ? d.GetAttributeValue<string>("defaultvalue") : value;
            }
            Func<string, string, string> get = (k, fallback) =>
            {
                string v;
                return values.TryGetValue(k, out v) && !string.IsNullOrWhiteSpace(v) ? v.Trim() : fallback;
            };
            int seconds;
            return new Settings
            {
                ApiKey = get("tf_TimefoldApiKey", ""),
                BaseUrl = get("tf_TimefoldBaseUrl", "https://app.timefold.ai"),
                RootTerritory = get("tf_RootTerritory", "Timefold Demo London"),
                SolverSeconds = int.TryParse(get("tf_SolverSeconds", "30"), out seconds) ? seconds : 30,
                Rules = Json.Obj(Json.Parse(get("tf_RoutingRules", "{}")))
            };
        }
    }
}
