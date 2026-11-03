# Timefold × Dynamics 365 Field Service

Plan Dynamics 365 Field Service work with **[Timefold](https://timefold.ai)**,
directly from Dynamics. There is no service to host, no Azure and no
container: a Dataverse plug-in runs inside your environment and calls the
Timefold Model API.

The **Field Service Routing** model plans the open work orders of a day (or a
few days) across your technicians: who does which job, in what order, and
when. The result is written as standard **bookings**, so it shows in the
schedule board with travel bars, in the map view, on the work orders and in
the mobile app.

## Requirements

- A Dynamics 365 Field Service environment. A
  [Field Service trial](https://www.microsoft.com/dynamics-365/free-trial) is
  enough; it needs a work account.
- A Timefold API key with the Field Service Routing model. Get one at
  [app.timefold.ai](https://app.timefold.ai) under **Settings > API keys**.
- For `setup.sh`: `bash` and [uv](https://docs.astral.sh/uv/getting-started/installation/).
  It installs the .NET SDK it needs into `.tools/`.

You can also install without any tools: import the solution zip (see
[Install without the scripts](#install-without-the-scripts)).

## Install

```bash
git clone https://github.com/TimefoldAI/Dynamics-FieldService-integration.git
cd Dynamics-FieldService-integration

cp .env.example .env          # then set TIMEFOLD_API_KEY in .env

./scripts/setup.sh --org https://<org>.crm4.dynamics.com
```

`setup.sh` is idempotent and stops at the first error. It:

1. checks the tools
2. signs you in to the environment (device code: you sign in as yourself, no
   app registration needed)
3. builds the plug-in
4. creates or updates the solution and stores the API key in the environment
5. offers to load the demo data
6. prints a link that opens the Field Service app

Then connect the flow that drives long runs. This is the one manual step,
because a connection signs in as a person. `setup.sh` prints the link:

1. In Power Automate, open the solution **Timefold for Field Service** >
   **Connection references** > **Timefold: Dataverse**.
2. Pick or create a **Microsoft Dataverse** connection (the connector with the
   green icon) and save. The "Premium" label does not matter: the Dynamics 365
   licence covers it.
3. Open **Cloud flows** > **Timefold: drive optimisation runs** and turn it off
   and on.

### Install without the scripts

1. Open [make.powerapps.com](https://make.powerapps.com) and pick your Field
   Service environment.
2. **Solutions > Import solution** and choose
   `dynamics/solution/TimefoldFieldService_1_0_0_0_managed.zip`.
3. On the last page, enter the **Timefold API key**.
4. Connect the flow, as above.

## Usage

Open **Work Orders** (or **Bookings**) in the Field Service app and press
**Optimise with Timefold**. Pick the day, the number of days and the solver
time, and press **Optimise**. The dialog shows the run as it goes: reading
Field Service, solving, writing bookings. When it says *Completed*, press
**Open schedule board**.

You can close the dialog at any time; the run carries on. Each run is a
**Timefold optimisation run** record with its stage, score, counts, log and the
exact request sent to Timefold. Find them in the Timefold optimisation runs
list. *Dry run* builds the request and stores it on the record without
sending it.

Anything that creates a run record starts a run: the dialog, the Web API, or a
Power Automate flow (for example a nightly plan for the next day).

## Setup options

| Question | Default | Flags |
|---|---|---|
| Load the demo data? | ask; no without a terminal | `--seed`, `--no-seed` |

| Option | What it does |
|---|---|
| `--org URL` | The environment. Default: `DATAVERSE_URL` in `.env` |
| `--api-key KEY` | The Timefold API key. Default: `TIMEFOLD_API_KEY`, then `.env` |
| `--yes` | Take the default for each question |
| `--no-export` | Don't write the solution zips in `dynamics/solution/` |

```bash
./scripts/setup.sh --org https://<org>.crm4.dynamics.com --no-seed
./scripts/setup.sh --yes
```

**Demo data.** The demo writes records into the environment and marks them,
so `scripts/seed_env.py --delete` removes them again. Do not load it into an
environment with real data: the integration plans your own Field Service
records. If you do not load it, set **Territory to plan** to your own
territory (see [Configuration](#configuration)).

## The API key

The key is the environment variable **Timefold API key** (`tf_TimefoldApiKey`).
`setup.sh` sets its current value. The value stays in the environment: it is
not part of the solution and never in an export. The plug-in reads it and
sends it as the `X-API-KEY` header.

To set the key by hand: in [make.powerapps.com](https://make.powerapps.com),
open **Solutions** > **Timefold for Field Service** > **Environment
variables** > **Timefold API key**, and set its **Current value**.

## How it works

```
   Dynamics 365                  Dataverse plug-in                Timefold
  ┌─────────────┐   SDK    ┌─────────────────────────┐          ┌───────────┐
  │  standard   │ ───────► │ Extract                 │          │ Field     │
  │  Field      │          │   ↓                     │ X-API-KEY│ Service   │
  │  Service    │          │ Translate ──────────────┼─────────►│ Routing   │
  │  tables     │          │   ↓                     │          │           │
  │             │ ◄─────── │ WriteBack ◄─────────────┼──────────│           │
  └─────────────┘ bookings └─────────────────────────┘   poll   └───────────┘
```

| Step | Class |
|---|---|
| Read Field Service | `Extract` |
| Build the Timefold request, read the plan | `Translate` |
| Call the Timefold Model API | `TimefoldClient` |
| Write the plan back as bookings | `WriteBack` |
| Start, poll and finish a run | `OptimisationRunPlugin`, `ProcessRunApi` |

The plan is written as one `bookableresourcebooking` for each visit: the same
record the schedule board creates when a dispatcher drags work onto a
technician, and the one Resource Scheduling Optimization writes.

| Timefold | Booking field |
|---|---|
| the technician | `Resource` |
| start of travel to the visit | `starttime`; the schedule board draws the travel bar from here |
| start of work | `msdyn_estimatedarrivaltime` |
| end of work | `endtime` |
| travel time and distance | `msdyn_estimatedtravelduration`, `msdyn_milestraveled` |
| | `BookingStatus` *Scheduled*, `msdyn_bookingmethod` *Optimization* |

A re-plan updates a visit's booking instead of adding a second one. A visit
that Timefold leaves out loses its booking, and Field Service returns its work
order to *Unscheduled*.

### Runs across executions

Dataverse stops a plug-in after two minutes, and stops a chain of plug-ins
that start each other at depth 8 within an hour. So a run moves in steps, and
the run record keeps the state:

```
create the run   the plug-in reads Field Service, POSTs to Timefold,
                 and polls for about 70 seconds
step             tf_ProcessRun: one check on the solver, or one batch of bookings
                   still solving?   store the score, wait for the next step
                   solved?          GET the plan, write bookings, close the run
```

The flow **Timefold: drive optimisation runs** calls the `tf_ProcessRun`
custom API every 20 seconds from a run's creation until it ends. Each call is
a fresh request, so a solve can take up to an hour. The dialog calls it too
while it is open, and the plug-in hands itself on for the first few minutes.
A lock on the run record (`tf_lockeduntil`) lets only one of them work on a
run at a time; a step that died lets go after 150 seconds. A run that has not
finished an hour after its solver time is marked *Failed*.

### Data used from Dynamics

| Timefold needs | Taken from |
|---|---|
| the planning scope | the territory **Territory to plan** and the territories below it |
| the jobs | `msdyn_resourcerequirement` of open work orders in scope |
| the job locations | `msdyn_latitude` / `msdyn_longitude` (from the service account) |
| the job durations | `msdyn_duration` (from the incident type) |
| the skills a job needs | `msdyn_requirementcharacteristic` with rating values |
| the engineers | `bookableresource` with a `msdyn_resourceterritory` in scope |
| their skills | `bookableresourcecharacteristic` with rating values |
| their start and end location | `msdyn_startlocation` / `msdyn_endlocation`: their address or organisational unit |
| their working hours, breaks and time off | the resource calendar, through `ExpandCalendar` |
| how urgent a job is | `msdyn_priority` Level of Importance |
| jobs under way | bookings that are Traveling, In Progress or On Break |

Travel times and distances come from the Timefold Platform maps service.

### Business rules

| Rule | Source |
|---|---|
| A job happens inside its time window | requirement time window, else its date window |
| The engineer arrives inside the promised time | work order *Time From / To Promised*: an arrival window, the job may run on |
| The engineer arrives in one of the bookable slots | time group on the requirement: arrival windows |
| An engineer needs the skills and levels a job requires | characteristics and rating values |
| Each territory's work stays with its own engineers | resource territories, sent as a required tag |
| Roles and organisational units | requirement roles and organisational unit |
| Preferred, restricted, must choose from | requirement resource preferences |
| The customer's preferred time, day and engineer | account preferences (soft) |
| One job after another, optionally by the same engineer | requirement dependencies |
| Several engineers on one job at the same time | requirement groups |
| Lunch and other breaks | calendar breaks, as `FIXED` breaks |
| Travel before and after working hours | resource travel settings |
| A labour cost per hour | resource hourly rate |

Skipped, and listed in the run log: work orders that are Completed, Posted or
Cancelled, and requirements with no coordinates, no duration, or no window in
the planning period.

### Work under way

A booking that is Traveling, In Progress or On Break is sent to Timefold as
pinned, at its engineer and time, and the write-back leaves it alone. Every
other booking in the planning window may be moved.

## Configuration

The integration is configured through the solution's environment variables, in
**Solutions** > **Timefold for Field Service** > **Environment variables**.

| Variable | Default | Notes |
|---|---|---|
| Timefold API key | | See [The API key](#the-api-key) |
| Timefold base URL | `https://app.timefold.ai` | |
| Territory to plan | `Timefold Demo London` | The territory to plan, with all territories below it |
| Default solver time (s) | `30` | The dialog's solver time overrides it |
| Routing rules | [`config/routing_rules.yaml`](config/routing_rules.yaml), as JSON | The policy that has no Field Service home |

The routing rules:

| Key | Default | Notes |
|---|---|---|
| `routingRules.maxOvertimeMinutes` | `30` | The end of the working day is a soft limit; this is the hard limit after it |
| `routingRules.restrictToOwnTerritory` | `true` | If `false`, Timefold can give a job to an engineer of another territory |
| `priority.higherLevelIsMoreImportant` | `true` | The direction of Level of Importance, which Field Service leaves to each organisation |
| `constraintWeights.*` | | Soft-constraint weights; the full list is in the [OpenAPI specification](https://app.timefold.ai/openapis/field-service-routing/v1) |
| `preferences.appointmentTimes` | Morning 08–12, Afternoon 12–17, Evening 17–20 | What the account's preferred time means |
| `writeBack.scheduledStatusName` | the first *Scheduled* status | The booking status to write |
| `termination` | | Optional: how Timefold decides to stop. By default it stops once the score stops improving, often before the solver time |

## Demo data

`scripts/seed_env.py` loads a field service round in London: eight biomedical
engineers with skills, calendars and home locations, 99 clinics, and 102 work
orders over three working days. It uses every rule above: time windows,
promised times, time-group slots, preferences, roles, organisational units,
dependencies, a two-engineer job, breaks, travel before and after work.

```bash
uv run scripts/seed_env.py              # day 0 = the next working day
uv run scripts/seed_env.py --today      # day 0 = today, if a weekday
uv run scripts/seed_env.py --replace    # remove the demo, then load it again
uv run scripts/seed_env.py --delete     # remove the demo
uv run scripts/reset_day.py 2026-11-03  # remove the demo's bookings on one day
```

The demo creates work orders the way a person would, so Field Service creates
each requirement, its duration and its skills itself. It marks everything it
creates and removes only that. The dataset is `scripts/demo/fsr_london.json`;
`scripts/demo/generate.py` makes it, and checks that the scenario can be
planned.

## Schedule board and map

The bookings show on the schedule board on each engineer's row, with a travel
bar before each visit. In the board's **Map** view, the **Routes** layer draws
one line for each engineer. Turn off the **Requirements** layer to hide open
work of other days. The map draws the route up to the last visit; the drive
home after it has no booking, though Timefold plans it.

## Troubleshooting

**The run fails with `403 TFP-11001`.** The Timefold tenant does not have the
Field Service Routing model. Contact Timefold.

**The run fails with `401`.** The API key is not set or is wrong. See [The API
key](#the-api-key).

**The dialog says the run has not moved for some minutes.** The flow is off or
has no connection. See [Install](#install). The run carries on as soon as the
flow runs.

**A 30-minute solve ends after one minute.** Timefold stops once the score
stops improving. To always use the full solver time, set `termination` in the
routing rules, for example `{"unimprovedSpentLimit": "PT1H"}`.

**Nothing to plan.** No open work orders in the territory on that day. Check
**Territory to plan**, and that the requirements' dates fall on the day.

**The schedule board shows two weeks instead of one day.** Switching the board
to Map and back leaves it on *Daily*. Set it to *Hourly*.

## Building from source

```bash
./scripts/build_plugin.sh          # compiles the plug-in (installs .NET into .tools/)
uv run scripts/deploy_solution.py  # creates or updates the solution, exports the zips
```

`deploy_solution.py` creates everything in the solution (table, form,
environment variables, plug-in and steps, custom API, flow, dialog and
buttons), publishes it, and exports `dynamics/solution/`. Bump
`dynamics/VERSION` before exporting a release.

## Repository layout

```
dynamics/
  plugin/
    Extract.cs                  read Field Service
    Translate.cs                build the request, read the plan
    TimefoldClient.cs           calls the Timefold Model API
    WriteBack.cs                write the plan back as bookings
    OptimisationRunPlugin.cs    start, poll and finish a run; the tf_ProcessRun step
    Json.cs                     JSON without dependencies (the plug-in sandbox)
  webresources/                 the Optimise with Timefold dialog
  solution/                     the exported solution, managed and unmanaged
  VERSION

scripts/
  setup.sh                      sets up an environment
  login.py                      signs in, writes .env
  build_plugin.sh               compiles the plug-in
  deploy_solution.py            creates the solution, exports it
  seed_env.py, reset_day.py     the demo data
  demo/                         the demo dataset, its generator and loader
  tfd365/                       Dataverse sign-in and Web API client

config/
  routing_rules.yaml            the default routing rules
```

## Known limits

- **The schedule board map doesn't show the drive home.** It is planned, but
  has no booking.
- **A run plans with what is in the planning window.** Work orders without a
  date in it, and requirements without coordinates or a duration, are skipped.
- **Breaks are always `FIXED`.** Calendar breaks have a fixed start and end.
  Timefold also supports floating breaks, but Field Service cannot express them.
- **Very large write-backs take several steps.** Bookings are written about 80
  seconds at a time, so hundreds of bookings take a few minutes after solving.
- **The flow needs a connection.** Without it, a run only moves while the
  dialog is open.

## Contributing

This repository does not accept external contributions. See
[CONTRIBUTING.md](CONTRIBUTING.md) for how to report a bug or ask for a change,
and [SECURITY.md](SECURITY.md) for how to report a security problem.

## License

Apache 2.0. See [LICENSE](LICENSE).
