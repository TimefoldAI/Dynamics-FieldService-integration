#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["msal>=1.28", "httpx>=0.27", "pyyaml"]
# ///
"""Build the Timefold solution in a Dynamics 365 environment, then export it.

    ./scripts/build_plugin.sh                 # compile the plug-in first
    uv run scripts/deploy_solution.py         # create or update, then export
    uv run scripts/deploy_solution.py --no-export

scripts/setup.sh runs it for you. Without a terminal, import the exported file,
dynamics/solution/TimefoldFieldService_<version>.zip, through Power Apps
(Solutions > Import) instead.

It creates, inside the solution TimefoldFieldService (publisher prefix tf):

  * the table tf_optimisationrun ("Timefold optimisation run"), its view and form
  * environment variables tf_TimefoldApiKey, tf_TimefoldBaseUrl,
    tf_RootTerritory, tf_SolverSeconds, tf_RoutingRules
  * the plug-in assembly Timefold.FieldService and two asynchronous steps
  * the custom API tf_ProcessRun: one step of a run
  * the flow "Timefold: drive optimisation runs" and its connection reference
  * web resources tf_/optimise.js and tf_/optimise.html
  * the "Optimise with Timefold" button on the Work Orders, Bookings and
    optimisation runs lists of the Field Service app

If TIMEFOLD_API_KEY is set in .env, it is stored as the environment
variable's current value. That value is not part of the solution and is never
exported.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import struct
import uuid
import zipfile

import yaml

from _common import ROOT, read_env, real_client

SOLUTION = "TimefoldFieldService"
PUBLISHER = "timefold"
TABLE = "tf_optimisationrun"
PLUGIN_DLL = ROOT / "dynamics/plugin/bin/Release/net462/Timefold.FieldService.dll"
SNK = ROOT / "dynamics/plugin/Timefold.FieldService.snk"
PLUGIN_TYPE = "Timefold.FieldService.OptimisationRunPlugin"
STEP_API_TYPE = "Timefold.FieldService.ProcessRunApi"
STEP_API = "tf_ProcessRun"
FLOW = "Timefold: drive optimisation runs"
DATAVERSE_API = "/providers/Microsoft.PowerApps/apis/shared_commondataserviceforapps"
CONNECTION_REF = "tf_dataverse"
STAGES = [(100000000, "Queued"), (100000001, "Solving"), (100000002, "Writing bookings"),
          (100000003, "Completed"), (100000004, "Failed"), (100000005, "Dry run done")]

env = read_env()


def label(text: str, lang: int) -> dict:
    return {"@odata.type": "Microsoft.Dynamics.CRM.Label", "LocalizedLabels": [
        {"@odata.type": "Microsoft.Dynamics.CRM.LocalizedLabel", "Label": text, "LanguageCode": lang}]}


ENGLISH = 1033
# Dataverse writes the system columns, views and form sections of a new table in
# the environment's base language only. The standard English labels, for an
# export from a Spanish environment (the trial the zips come from).
TRANSLATIONS = {
    "Autor": "Created By", "Autor (delegado)": "Created By (Delegate)",
    "Modificado por": "Modified By", "Modificado por (delegado)": "Modified By (Delegate)",
    "Fecha de creación": "Created On", "Fecha de modificación": "Modified On",
    "Fecha de creación del registro": "Record Created On",
    "Propietario": "Owner", "Usuario propietario": "Owning User",
    "Equipo propietario": "Owning Team", "Unidad de negocio propietaria": "Owning Business Unit",
    "Estado": "Status", "Razón para el estado": "Status Reason",
    "Activo": "Active", "Inactivo": "Inactive",
    "Número de secuencia de importación": "Import Sequence Number",
    "Número de versión de regla de zona horaria": "Time Zone Rule Version Number",
    "Código de zona horaria de conversión UTC": "UTC Conversion Time Zone Code",
    "Información": "Information", "Detalles": "Details", "GENERAL": "General",
    "Encabezado": "Header", "Pie de página": "Footer",
    "Id. del propietario": "Owner Id",
    "Solo para uso interno.": "For internal use only.",
    "Formulario para esta entidad.": "A form for this entity.",
    "Formulario de tarjeta para esta entidad.": "A card form for this entity.",
    "Identificador único de instancias de entidad": "Unique identifier for entity instances",
    "Identificador único del usuario propietario del registro.":
        "Unique identifier for the user that owns the record.",
    "Identificador único del equipo propietario del registro.":
        "Unique identifier for the team that owns the record.",
    "Identificador único de la unidad de negocio propietaria del registro":
        "Unique identifier for the business unit that owns the record",
    "Identificador único del usuario que creó el registro.":
        "Unique identifier of the user who created the record.",
    "Identificador único del usuario que modificó el registro.":
        "Unique identifier of the user who modified the record.",
    "Identificador único del usuario delegado que creó el registro.":
        "Unique identifier of the delegate user who created the record.",
    "Identificador único del usuario delegado que modificó el registro.":
        "Unique identifier of the delegate user who modified the record.",
    "Fecha y hora de creación del registro.": "Date and time when the record was created.",
    "Fecha y hora en que se modificó el registro.": "Date and time when the record was modified.",
    "Fecha y hora en que se migró el registro.": "Date and time that the record was migrated.",
    "Número de secuencia de la importación que creó este registro.":
        "Sequence number of the import that created this record.",
    "Código de la zona horaria que estaba en uso cuando se creó el registro.":
        "Time zone code that was in use when the record was created.",
}
# The same for labels that carry the table's name.
TRANSLATION_PATTERNS = [
    (r"Estado del (.+)", r"Status of the \1"),
    (r"Razón para el estado del (.+)", r"Reason for the status of the \1"),
    (r"Mis (.+)", r"My \1"),
    (r"Entidades (.+) activas de mi propiedad", r"Active \1 owned by me"),
    (r"Búsqueda rápida de (.+) activos", r"Quick Find Active \1"),
    (r"(.+) activo", r"Active \1"),
    (r"(.+) inactivo", r"Inactive \1"),
    (r"Vista asociada de (.+)", r"\1 Associated View"),
    (r"Vista de búsqueda avanzada de (.+)", r"\1 Advanced Find View"),
    (r"Vista de búsqueda de (.+)", r"\1 Lookup View"),
]
LABEL = r'<(\w+)\b[^>]*?\blanguagecode="(\d+)"[^>]*/>'


def translate(text: str) -> str:
    if text in TRANSLATIONS:
        return TRANSLATIONS[text]
    for pattern, english in TRANSLATION_PATTERNS:
        if re.fullmatch(pattern, text):
            return re.sub(pattern, english, text)
    return text


def english_labels(xml: str, base: int, untranslated: set) -> str:
    """Keep one English label per text. Where only the base language has one,
    use it as English: our own labels are English text whatever the code."""
    def group(run):
        found = {}
        for m in re.finditer(LABEL, run.group(0)):
            found.setdefault(m.group(1), {})[int(m.group(2))] = m.group(0)
        indent = re.match(r"\s*", run.group(0)).group(0)
        kept = []
        for by_lang in found.values():
            tag = by_lang.get(ENGLISH)
            if tag is None and base in by_lang:
                tag = by_lang[base].replace(f'languagecode="{base}"', f'languagecode="{ENGLISH}"')
                old = re.search(r'description="([^"]*)"', tag)
                if old:
                    new = translate(old.group(1))
                    if new == old.group(1) and re.search(r"[áéíóúñ]|\b(de|del|el|la)\b", new):
                        untranslated.add(new)
                    tag = tag.replace(old.group(0), f'description="{new}"')
            if tag:
                kept.append(tag)
        return "".join(indent + t for t in kept)
    xml = re.sub(rf"(?:\s*{LABEL})+", group, xml)
    xml = re.sub(r"(<Languages>).*?(</Languages>)", rf"\g<1><Language>{ENGLISH}</Language>\g<2>", xml, flags=re.S)
    return re.sub(r'(<ImportExportXml\b[^>]*\blanguagecode=")\d+"', rf'\g<1>{ENGLISH}"', xml)


def english_only(data: bytes, base: int) -> bytes:
    """Make English the solution's base language, so the zip is not tied to the
    language of the environment it was exported from."""
    if base == ENGLISH:
        return data
    untranslated = set()
    out = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            body = src.read(item)
            if item.filename.endswith(".xml"):
                bom = body.startswith(b"\xef\xbb\xbf")
                text = english_labels(body.decode("utf-8-sig"), base, untranslated)
                body = (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")
            dst.writestr(item, body)
    for text in sorted(untranslated):
        print(f"  no English label for: {text}")
    return out.getvalue()


def contains(data: bytes, needle: bytes) -> bool:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return any(needle in z.read(name) for name in z.namelist())


class Deployer:
    def __init__(self, dv):
        self.dv = dv
        self.lang = dv.get("organizations", select=["languagecode"])[0]["languagecode"]
        self.in_solution = {"MSCRM.SolutionUniqueName": SOLUTION}

    # ------------------------------------------------------------ plumbing
    def post(self, path, body, solution=True):
        headers = self.in_solution if solution else {}
        r = self.dv._send("POST", path, f"POST {path}", json=body, headers=headers)
        loc = r.headers.get("OData-EntityId", "")
        return loc[loc.rfind("(") + 1:-1] if "(" in loc else (r.json() if r.content else {})

    def first(self, entity_set, key, flt, select=()):
        rows = self.dv.get(entity_set, select=[key, *select], filter=flt)
        return rows[0] if rows else None

    def upsert(self, entity_set, key, flt, record, solution=True):
        found = self.first(entity_set, key, flt)
        if found:
            self.dv._send("PATCH", f"{entity_set}({found[key]})", f"PATCH {entity_set}", json=record,
                          headers={"If-Match": "*", **(self.in_solution if solution else {})})
            return found[key]
        return self.post(entity_set, record, solution)

    # ---------------------------------------------------- publisher, solution
    def solution(self):
        pub = self.upsert("publishers", "publisherid", f"uniquename eq '{PUBLISHER}'", {
            "uniquename": PUBLISHER, "friendlyname": "Timefold",
            "customizationprefix": "tf", "customizationoptionvalueprefix": 10000,
            "description": "Timefold BV, https://timefold.ai"}, solution=False)
        version = (ROOT / "dynamics/VERSION").read_text().strip()
        self.upsert("solutions", "solutionid", f"uniquename eq '{SOLUTION}'", {
            "uniquename": SOLUTION, "friendlyname": "Timefold for Field Service",
            "version": version,
            "description": "Optimises Dynamics 365 Field Service routes with Timefold Field Service "
                           "Routing, entirely inside Dataverse. https://timefold.ai",
            "publisherid@odata.bind": f"/publishers({pub})"}, solution=False)
        print(f"solution {SOLUTION} {version}")
        return version

    # --------------------------------------------------------------- table
    def table(self):
        L = lambda t: label(t, self.lang)
        exists = self.dv.get("EntityDefinitions", select=["LogicalName"],
                             filter=f"LogicalName eq '{TABLE}'")
        if not exists:
            self.post("EntityDefinitions", {
                "@odata.type": "Microsoft.Dynamics.CRM.EntityMetadata",
                "SchemaName": TABLE, "DisplayName": L("Timefold optimisation run"),
                "DisplayCollectionName": L("Timefold optimisation runs"),
                "Description": L("One route optimisation by Timefold. Creating a row starts it."),
                "OwnershipType": "UserOwned", "IsActivity": False, "HasActivities": False,
                "HasNotes": False, "PrimaryNameAttribute": "tf_name",
                "Attributes": [{
                    "@odata.type": "Microsoft.Dynamics.CRM.StringAttributeMetadata",
                    "SchemaName": "tf_name", "IsPrimaryName": True, "MaxLength": 200,
                    "FormatName": {"Value": "Text"}, "DisplayName": L("Name"),
                    "RequiredLevel": {"Value": "ApplicationRequired"}}]})
            print(f"created table {TABLE}")
        have = {a["LogicalName"] for a in self.dv.function(
            f"EntityDefinitions(LogicalName='{TABLE}')/Attributes?$select=LogicalName")["value"]}

        def attr(schema, kind, name, **extra):
            if schema in have:
                return
            self.post(f"EntityDefinitions(LogicalName='{TABLE}')/Attributes", {
                "@odata.type": f"Microsoft.Dynamics.CRM.{kind}AttributeMetadata",
                "SchemaName": schema, "DisplayName": L(name),
                "RequiredLevel": {"Value": "None"}, **extra})
            print(f"  column {schema}")

        attr("tf_plandate", "DateTime", "Day to plan", Format="DateOnly",
             DateTimeBehavior={"Value": "DateOnly"})
        for schema, name in [("tf_days", "Days"), ("tf_solverseconds", "Solver time (s)"),
                             ("tf_checks", "Checks"), ("tf_visits", "Work orders"),
                             ("tf_scheduled", "Booked"), ("tf_unscheduled", "Left open")]:
            attr(schema, "Integer", name, MinValue=0, MaxValue=100000, Format="None")
        attr("tf_dryrun", "Boolean", "Dry run", DefaultValue=False, OptionSet={
            "@odata.type": "Microsoft.Dynamics.CRM.BooleanOptionSetMetadata",
            "TrueOption": {"Value": 1, "Label": L("Yes")},
            "FalseOption": {"Value": 0, "Label": L("No")}})
        attr("tf_stage", "Picklist", "Stage", DefaultFormValue=STAGES[0][0], OptionSet={
            "@odata.type": "Microsoft.Dynamics.CRM.OptionSetMetadata", "IsGlobal": False,
            "OptionSetType": "Picklist",
            "Options": [{"Value": v, "Label": L(t)} for v, t in STAGES]})
        attr("tf_timefoldjobid", "String", "Timefold job", MaxLength=100,
             FormatName={"Value": "Text"})
        attr("tf_score", "String", "Score", MaxLength=200, FormatName={"Value": "Text"})
        attr("tf_log", "Memo", "Log", MaxLength=100000, Format="TextArea")
        attr("tf_request", "Memo", "Timefold request", MaxLength=1048576, Format="TextArea")
        # Held by whoever is working on the run (plug-in, flow, dialog), so only one does at a time.
        attr("tf_lockeduntil", "DateTime", "Locked until", Format="DateAndTime",
             DateTimeBehavior={"Value": "TimeZoneIndependent"})

    def view_and_form(self):
        """Columns that matter on the default view, and a form that shows them."""
        cols = ["tf_name", "tf_plandate", "tf_stage", "tf_visits", "tf_scheduled",
                "tf_unscheduled", "tf_score", "createdon"]
        for view in self.dv.get("savedqueries", select=["savedqueryid", "querytype", "name"],
                                filter=f"returnedtypecode eq '{TABLE}' and (querytype eq 0 or querytype eq 64)"):
            layout = ('<grid name="resultset" object="1" jump="tf_name" select="1" icon="1" preview="1">'
                      '<row name="result" id="tf_optimisationrunid">'
                      + "".join(f'<cell name="{c}" width="{220 if c == "tf_name" else 120}" />' for c in cols)
                      + "</row></grid>")
            fetch = (f'<fetch version="1.0" mapping="logical"><entity name="{TABLE}">'
                     + "".join(f'<attribute name="{c}" />' for c in cols)
                     + '<attribute name="tf_optimisationrunid" /><order attribute="createdon" descending="true" />'
                     '<filter type="and"><condition attribute="statecode" operator="eq" value="0" /></filter>'
                     "</entity></fetch>")
            self.dv._send("PATCH", f"savedqueries({view['savedqueryid']})", "PATCH view",
                          json={"layoutxml": layout, "fetchxml": fetch}, headers={"If-Match": "*"})
        classid = {"text": "{4273EDBD-AC1D-40d3-9FB2-095C621B552D}",
                   "memo": "{E0DECE4B-6FC8-4a8f-A065-082708572369}",
                   "date": "{5B773807-9FB2-42db-97C3-7A91EFF8ADFF}",
                   "int": "{C6D124CA-7EDA-4a60-AEA9-7FB8D318B68F}",
                   "pick": "{3EF39988-22BB-4f0b-BBBE-64B5A3748AEE}",
                   "bool": "{B0C6723A-8503-4fd7-BB28-C8A06AC933C2}"}
        fields = [("tf_name", "Name", "text"), ("tf_plandate", "Day to plan", "date"),
                  ("tf_days", "Days", "int"), ("tf_solverseconds", "Solver time (s)", "int"),
                  ("tf_dryrun", "Dry run", "bool"), ("tf_stage", "Stage", "pick"),
                  ("tf_visits", "Work orders", "int"), ("tf_scheduled", "Booked", "int"),
                  ("tf_unscheduled", "Left open", "int"), ("tf_score", "Score", "text"),
                  ("tf_timefoldjobid", "Timefold job", "text"), ("tf_log", "Log", "memo"),
                  ("tf_request", "Timefold request", "memo")]
        g = lambda: "{" + str(uuid.uuid4()) + "}"
        lab = lambda t: f'<labels><label description="{t}" languagecode="{self.lang}" /></labels>'
        rows = "".join(f'<row><cell id="{g()}">{lab(n)}<control id="{c}" classid="{classid[k]}" '
                       f'datafieldname="{c}" disabled="{"false" if c in ("tf_name", "tf_plandate", "tf_days", "tf_solverseconds", "tf_dryrun") else "true"}" /></cell></row>'
                       for c, n, k in fields)
        formxml = (f'<form><tabs><tab name="run" id="{g()}" IsUserDefined="1" verticallayout="true">'
                   f'{lab("Run")}<columns><column width="100%"><sections>'
                   f'<section name="details" showlabel="false" showbar="false" id="{g()}" '
                   f'IsUserDefined="1" columns="1">{lab("Details")}<rows>{rows}</rows></section>'
                   "</sections></column></columns></tab></tabs></form>")
        for form in self.dv.get("systemforms", select=["formid"],
                                filter=f"objecttypecode eq '{TABLE}' and type eq 2"):
            self.dv._send("PATCH", f"systemforms({form['formid']})", "PATCH form",
                          json={"formxml": formxml}, headers={"If-Match": "*"})
        print("  view and form")

    # ------------------------------------------------- environment variables
    def environment_variables(self):
        rules = yaml.safe_load((ROOT / "config/routing_rules.yaml").read_text())
        defs = [
            ("tf_TimefoldApiKey", "Timefold API key", 100000000, "",
             "Your key from https://app.timefold.ai > Settings > API keys. Set it as this "
             "variable's current value; it is not exported with the solution."),
            ("tf_TimefoldBaseUrl", "Timefold base URL", 100000000, "https://app.timefold.ai", ""),
            ("tf_RootTerritory", "Territory to plan", 100000000, "Timefold Demo London",
             "The territory whose child territories make up the planning scope."),
            ("tf_SolverSeconds", "Default solver time (s)", 100000001, "30", ""),
            ("tf_RoutingRules", "Routing rules", 100000003, json.dumps(rules, indent=1),
             "Overtime, territory matching, priority direction and constraint weights."),
        ]
        ids = {}
        for schema, name, kind, default, desc in defs:
            ids[schema] = self.upsert("environmentvariabledefinitions", "environmentvariabledefinitionid",
                                      f"schemaname eq '{schema}'", {
                                          "schemaname": schema, "displayname": name, "type": kind,
                                          "defaultvalue": default, "description": desc})
        key = env.get("TIMEFOLD_API_KEY", "").strip()
        if key:
            # Deliberately outside the solution: a key must never be exported.
            self.upsert("environmentvariablevalues", "environmentvariablevalueid",
                        f"_environmentvariabledefinitionid_value eq {ids['tf_TimefoldApiKey']}", {
                            "schemaname": "tf_TimefoldApiKey", "value": key,
                            "EnvironmentVariableDefinitionId@odata.bind":
                                f"/environmentvariabledefinitions({ids['tf_TimefoldApiKey']})"},
                        solution=False)
            print("  environment variables (API key value set from .env, not in the solution)")
        else:
            print("  environment variables (no TIMEFOLD_API_KEY in .env: set it in Dynamics)")

    # --------------------------------------------------------------- plug-in
    def plugin(self):
        if not PLUGIN_DLL.exists():
            raise SystemExit("Build the plug-in first: ./scripts/build_plugin.sh")
        blob = SNK.read_bytes()
        bitlen, exponent = struct.unpack("<II", blob[12:20])
        modulus = blob[20:20 + bitlen // 8]
        public = (struct.pack("<III", 0x2400, 0x8004, 20 + bitlen // 8) + bytes([6, 2, 0, 0])
                  + struct.pack("<I", 0x2400) + b"RSA1" + struct.pack("<II", bitlen, exponent) + modulus)
        token = hashlib.sha1(public).digest()[-8:][::-1].hex()
        assembly = self.upsert("pluginassemblies", "pluginassemblyid", "name eq 'Timefold.FieldService'", {
            "name": "Timefold.FieldService", "content": base64.b64encode(PLUGIN_DLL.read_bytes()).decode(),
            "isolationmode": 2, "sourcetype": 0, "version": "1.0.0.0", "culture": "neutral",
            "publickeytoken": token,
            "description": "Timefold Field Service Routing for Dynamics 365 Field Service."})
        plugin_type = self.upsert("plugintypes", "plugintypeid", f"typename eq '{PLUGIN_TYPE}'", {
            "typename": PLUGIN_TYPE, "name": PLUGIN_TYPE, "friendlyname": "Timefold optimisation run",
            "pluginassemblyid@odata.bind": f"/pluginassemblies({assembly})"})
        for message, step_name, filtering in [
                ("Create", "Timefold: start an optimisation run", None),
                ("Update", "Timefold: continue an optimisation run", "tf_checks")]:
            msg = self.first("sdkmessages", "sdkmessageid", f"name eq '{message}'")["sdkmessageid"]
            flt = self.first("sdkmessagefilters", "sdkmessagefilterid",
                             f"_sdkmessageid_value eq {msg} and primaryobjecttypecode eq '{TABLE}'")
            record = {"name": step_name, "mode": 1, "rank": 1, "stage": 40, "supporteddeployment": 0,
                      "asyncautodelete": False, "description": step_name,
                      "plugintypeid@odata.bind": f"/plugintypes({plugin_type})",
                      "sdkmessageid@odata.bind": f"/sdkmessages({msg})",
                      "sdkmessagefilterid@odata.bind": f"/sdkmessagefilters({flt['sdkmessagefilterid']})"}
            if filtering:
                record["filteringattributes"] = filtering
            self.upsert("sdkmessageprocessingsteps", "sdkmessageprocessingstepid",
                        f"name eq '{step_name}'", record)
        print(f"  plug-in assembly (public key token {token}) and 2 asynchronous steps")
        self.step_api(assembly)

    def step_api(self, assembly):
        """tf_ProcessRun: one step of a run, as a fresh request (custom API)."""
        api_type = self.upsert("plugintypes", "plugintypeid", f"typename eq '{STEP_API_TYPE}'", {
            "typename": STEP_API_TYPE, "name": STEP_API_TYPE, "friendlyname": "Timefold: process a run",
            "pluginassemblyid@odata.bind": f"/pluginassemblies({assembly})"})
        found = self.first("customapis", "customapiid", f"uniquename eq '{STEP_API}'")
        if found:
            api = found["customapiid"]
        else:   # a custom API's shape cannot change after creation, so it is only created
            api = self.post("customapis", {
                "uniquename": STEP_API, "name": STEP_API, "displayname": "Timefold: process an optimisation run",
                "description": "Advances a Timefold optimisation run by one step: checks on the solver or "
                               "writes a batch of bookings. Called by the driver flow and the dialog.",
                "bindingtype": 0, "isfunction": False, "isprivate": False,
                "allowedcustomprocessingsteptype": 0,
                "PluginTypeId@odata.bind": f"/plugintypes({api_type})"})
            self.post("customapirequestparameters", {
                "uniquename": "RunId", "name": "RunId", "displayname": "Run",
                "description": "The tf_optimisationrun to advance.", "type": 12, "isoptional": False,
                "CustomAPIId@odata.bind": f"/customapis({api})"})
            for name, kind, text in [("Stage", 7, "The run's stage after the step (tf_stage value)."),
                                     ("Busy", 0, "True when another caller was already working on the run.")]:
                self.post("customapiresponseproperties", {
                    "uniquename": name, "name": name, "displayname": name, "description": text,
                    "type": kind, "CustomAPIId@odata.bind": f"/customapis({api})"})
        print(f"  custom API {STEP_API}")

    # ------------------------------------------------------- driver flow
    def flow(self):
        """A cloud flow that calls tf_ProcessRun every 20 s from a run's creation until
        it ends: each call is a fresh request, so a run is not bound by the plug-in limits."""
        self.upsert("connectionreferences", "connectionreferenceid",
                    f"connectionreferencelogicalname eq '{CONNECTION_REF}'", {
                        "connectionreferencelogicalname": CONNECTION_REF,
                        "connectionreferencedisplayname": "Timefold: Dataverse",
                        "connectorid": DATAVERSE_API,
                        "description": "The Dataverse connection the Timefold driver flow runs with."})
        host = {"connectionName": "shared_commondataserviceforapps", "apiId": DATAVERSE_API}
        final = ",".join(f"equals(variables('stage'),{s})" for s in (100000003, 100000004, 100000005))
        definition = {
            "$schema": "https://schema.management.azure.com/providers/Microsoft.Logic/schemas/2016-06-01/workflowdefinition.json#",
            "contentVersion": "1.0.0.0",
            "parameters": {"$connections": {"defaultValue": {}, "type": "Object"},
                           "$authentication": {"defaultValue": {}, "type": "SecureObject"}},
            "triggers": {"When_a_run_is_created": {
                "type": "OpenApiConnectionWebhook",
                "inputs": {"host": {**host, "operationId": "SubscribeWebhookTrigger"},
                           "parameters": {"subscriptionRequest/message": 1,
                                          "subscriptionRequest/entityname": TABLE,
                                          "subscriptionRequest/scope": 4},
                           "authentication": "@parameters('$authentication')"}}},
            "actions": {
                "Stage": {"type": "InitializeVariable", "runAfter": {},
                          "inputs": {"variables": [{"name": "stage", "type": "integer", "value": 0}]}},
                "Until_the_run_ends": {
                    "type": "Until", "runAfter": {"Stage": ["Succeeded"]},
                    "expression": f"@or({final})",
                    "limit": {"count": 5000, "timeout": "PT12H"},
                    "actions": {
                        "Wait": {"type": "Wait", "runAfter": {},
                                 "inputs": {"interval": {"count": 20, "unit": "Second"}}},
                        "Process_the_run": {
                            "type": "OpenApiConnection", "runAfter": {"Wait": ["Succeeded"]},
                            "inputs": {"host": {**host, "operationId": "PerformUnboundAction"},
                                       "parameters": {"actionName": STEP_API,
                                                      "item/RunId": "@triggerOutputs()?['body/tf_optimisationrunid']"},
                                       "authentication": "@parameters('$authentication')"}},
                        # A failed call (a timeout, a hiccup) just means: try again next round.
                        "Remember_the_stage": {
                            "type": "SetVariable",
                            "runAfter": {"Process_the_run": ["Succeeded", "Failed", "TimedOut"]},
                            "inputs": {"name": "stage",
                                       "value": "@if(equals(actions('Process_the_run')?['status'],'Succeeded'),"
                                                "outputs('Process_the_run')?['body/Stage'],0)"}}}}},
        }
        clientdata = {"properties": {
            "connectionReferences": {"shared_commondataserviceforapps": {
                "runtimeSource": "embedded",
                "connection": {"connectionReferenceLogicalName": CONNECTION_REF},
                "api": {"name": "shared_commondataserviceforapps"}}},
            "definition": definition}, "schemaVersion": "1.0.0.0"}
        flow = self.upsert("workflows", "workflowid", f"name eq '{FLOW}' and category eq 5", {
            "name": FLOW, "category": 5, "type": 1, "primaryentity": "none",
            "description": "Advances Timefold optimisation runs past Dataverse's two-minute plug-in limit.",
            "clientdata": json.dumps(clientdata)})
        on = self.first("workflows", "workflowid", f"workflowid eq {flow}", select=["statecode"])
        if not (on and on.get("statecode") == 1):
            try:
                self.dv._send("PATCH", f"workflows({flow})", "turn the flow on",
                              json={"statecode": 1, "statuscode": 2}, headers={"If-Match": "*"})
            except Exception as e:                                  # reported below
                print(f"  could not turn the flow on: {e}")
        ref = self.first("connectionreferences", "connectionreferenceid",
                         f"connectionreferencelogicalname eq '{CONNECTION_REF}'", select=["connectionid"])
        if ref and ref.get("connectionid"):
            print(f"  flow '{FLOW}' (on, connected)")
            return
        # A connection signs in as a person, so this one step is done by hand.
        env_id = self.dv.function("RetrieveCurrentOrganization(AccessType=@p)?@p="
                                  "Microsoft.Dynamics.CRM.EndpointAccessType'Default'")["Detail"]["EnvironmentId"]
        print(f"  flow '{FLOW}' NEEDS A CONNECTION. Long runs only move while the dialog is open until then.\n"
              f"    1. Open https://make.powerautomate.com/environments/{env_id}/solutions\n"
              f"       > Timefold for Field Service > Connection references > Timefold: Dataverse\n"
              f"    2. Pick or create a Microsoft Dataverse connection (the green icon), save\n"
              f"    3. Cloud flows > {FLOW}: turn it off and on")

    # ------------------------------------------------- web resources, button
    def webresources(self):
        ids = {}
        for name, kind, display in [("tf_/optimise.js", 3, "Timefold: open the optimise dialog"),
                                    ("tf_/optimise.html", 1, "Timefold: optimise dialog")]:
            content = (ROOT / "dynamics/webresources" / name.split("/")[-1]).read_bytes()
            ids[name] = self.upsert("webresourceset", "webresourceid", f"name eq '{name}'", {
                "name": name, "displayname": display, "webresourcetype": kind,
                "content": base64.b64encode(content).decode()})
        print("  web resources")
        return ids

    def buttons(self, js_id):
        app = self.first("appmodules", "appmoduleid", "uniquename eq 'msdyn_FieldService'")["appmoduleid"]
        common = {"buttonlabeltext": "Optimise with Timefold",
                  "buttontooltiptitle": "Optimise with Timefold",
                  "buttontooltipdescription": "Plan the day's open work orders across your technicians.",
                  "onclickeventtype": 2, "onclickeventjavascriptfunctionname": "Timefold.openOptimiseDialog",
                  "fonticon": "$clientsvg:Calendar", "hidden": False, "type": 0, "sequence": 5,
                  "AppModuleId@odata.bind": f"/appmodules({app})",
                  "OnClickEventJavaScriptWebResourceId@odata.bind": f"/webresourceset({js_id})"}
        # Custom commands cannot go in the global header, only on forms and
        # lists, so the button sits on the lists a dispatcher works from.
        placed = []
        for table in ("msdyn_workorder", "bookableresourcebooking", TABLE):
            unique = f"tf_OptimiseWithTimefold_{table}"
            meta = self.dv.function(f"EntityDefinitions(LogicalName='{table}')?$select=MetadataId")["MetadataId"]
            record = {**common, "name": unique, "uniquename": unique, "location": 1,   # list
                      "context": 1, "contextvalue": table,
                      "ContextEntity@odata.bind": f"/entities({meta})"}
            try:
                self.upsert("appactions", "appactionid", f"uniquename eq '{unique}'", record)
                placed.append(table)
            except Exception as e:                                  # keep going
                print(f"  could not place the button on {table}: {e}")
        print(f"  button on the lists of: {', '.join(placed)}")
        return app

    def publish(self, app, webresources):
        xml = ("<importexportxml><entities><entity>" + TABLE + "</entity></entities>"
               "<webresources>" + "".join(f"<webresource>{{{i}}}</webresource>" for i in webresources.values())
               + "</webresources><appmodules><appmodule>{" + app + "}</appmodule></appmodules></importexportxml>")
        self.dv.action("PublishXml", {"ParameterXml": xml})
        print("published")

    def export(self, version):
        out = ROOT / "dynamics/solution"
        out.mkdir(parents=True, exist_ok=True)
        for managed in (False, True):
            result = self.dv.action("ExportSolution", {"SolutionName": SOLUTION, "Managed": managed})
            name = f"{SOLUTION}_{version.replace('.', '_')}{'_managed' if managed else ''}.zip"
            data = english_only(base64.b64decode(result["ExportSolutionFile"]), self.lang)
            if env.get("TIMEFOLD_API_KEY") and contains(data, env["TIMEFOLD_API_KEY"].encode()):
                raise SystemExit("Refusing to write an export that contains the API key.")
            (out / name).write_bytes(data)
            print(f"exported dynamics/solution/{name} ({len(data) // 1024} KB)")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--no-export", action="store_true")
    args = parser.parse_args()
    with real_client() as dv:
        dv._client.timeout = 300
        d = Deployer(dv)
        version = d.solution()
        d.table()
        d.view_and_form()
        d.environment_variables()
        d.plugin()
        d.flow()
        webresources = d.webresources()
        app = d.buttons(webresources["tf_/optimise.js"])
        d.publish(app, webresources)
        if not args.no_export:
            d.export(version)


if __name__ == "__main__":
    main()
