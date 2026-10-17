"""
ConversAI MCP server (local). Exposes the unified semantic layer to any MCP client.

The agent never sends SQL. It sends a semantic query (governed names only); the server
compiles it with MetricFlow, translates it for the chosen target, and asks the human to
approve before anything runs. The SQL that executes is always compiler-generated.

Exports: export_for_analysis copies the governed data behind a question (scoped by months and
account attributes) to BigQuery (or local Parquet) for the user's own analysis, after approval.
The conversation is then pinned to that copy: every later question runs on it, and the source
is not queried again until the user switches back with use_target.

Proposed definitions: when no governed name fits, the agent proposes a definition (propose_definition) in
the layer's own terms - never SQL. The server checks it (governed columns only, allowed functions only,
MetricFlow compile, engine check), the user approves it, and it becomes PROVISIONAL for the conversation:
usable by name, and every answer that uses it is labelled. A data owner promotes it into the governed
layer later (python cli.py definitions promote <id>).

  python mcp_server.py            # stdio (VS Code / Claude Desktop launch it)
  python mcp_server.py --http     # streamable HTTP on :8080/mcp
"""

import json
import os
import sys
import threading

import anyio
from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field, create_model

from engine import definitions, exports, pipeline
from engine.compiler import SemanticQueryError, warm_up
from engine.dialects import BindingError
from engine.nl import query_rules

INSTRUCTIONS = """ConversAI answers questions about a credit card portfolio from governed metrics only.
1. Call list_metrics first: it returns the metrics AND the rules for building a query (the latest billing
   month, how to read "last month" / "last quarter", when to add a month filter, how to filter codes), and
   the target this conversation is using. Follow those rules exactly. Call list_dimensions for names, and
   search_catalog("<words from the question>") for full meanings, codes and synonyms.
2. Build a semantic query with governed names only. Never write SQL, never invent metrics or formulas.
   Use get_dimension_values before filtering on codes you are unsure of.
3. Where the rules say "fill cannot_answer_reason", do not run anything: tell the user the question cannot be
   answered from governed metrics (and why), or ask the clarifying question.
4. Call run_semantic_query; the user approves the generated SQL in a dialog. Answer only from the returned
   rows, state the definition used (the summary and notes), and show rates as percentages.
5. Exports: when the user wants the data to analyse themselves ("export", "give me the data", "put it in
   BigQuery"), call export_for_analysis with the scope of the question: a month range and/or account
   attributes (account__ dimensions). The user approves; the copy is made; from then on this conversation
   is pinned to the copy and every question runs there (do not pass another target). Tell the user where
   the data is (the folder and CSV file for a local export, the console link for BigQuery). To go back to the source, the user asks and you call use_target.
   If a plan's notes contain "OUTSIDE THE EXPORT", tell the user before answering: that part of the question
   is not in the export. Offer to switch back to the source (use_target) or to export a wider scope.
6. Missing concept: if no governed metric or dimension can express the question (e.g. "score improved by 50+
   points", a new ratio), do not give up and do not approximate silently. Call list_definitions: reuse a
   provisional definition that fits (use_definition), else call propose_definition with a definition in
   the layer's terms (never SQL). Put every interpretation you chose (thresholds, what counts, how the first
   cycle is treated) in choices. The user approves it; then use its name in run_semantic_query like any
   other. When an answer uses a provisional definition, say so: it is approved for this conversation but
   not yet governed. If the server rejects a proposal, fix what the error says (at most twice), else explain.
7. Use only these tools to answer data questions. Tool results are small on purpose; never save them to files,
   never search them with terminal or PowerShell commands - call search_catalog instead. Do not read the project's files or run terminal commands.
   If a tool returns an error, show the error to the user and stop; do not work around it."""

mcp = FastMCP("conversai-semantic", instructions=INSTRUCTIONS, host="0.0.0.0",
              port=int(os.environ.get("PORT", "8080")))

_SESSIONS: dict = {}          # id(MCP session) -> {"target": pinned target, "definitions": [provisional ids]}


class Filter(BaseModel):
    dimension: str = Field(description="A dimension name from list_dimensions")
    op: str = Field(description="One of = != > >= < <= in, not in")
    value: str | float | int | list = Field(description="Value, ISO date, or list for in / not in")


class SemanticQuery(BaseModel):
    metrics: list[str] = Field(description="Governed metric names from list_metrics")
    group_by: list[str] = Field(default_factory=list, description="Dimension names from list_dimensions")
    where: list[Filter] = Field(default_factory=list)
    order_by: list[str] = Field(default_factory=list, description="Metric or group_by names; prefix - for descending")
    limit: int | None = Field(default=None, description="1-1000")


class DefinitionSpec(BaseModel):
    name: str = Field(description="New snake_case name, e.g. bureau_score_drop_50")
    kind: str = Field(description="'dimension' or 'metric'")
    description: str = Field(description="Plain-English business meaning the user approves")
    label: str | None = Field(default=None, description="Short display label")
    model: str | None = Field(default=None, description="dimension: 'account' or 'account_cycle'; simple metric: 'account_cycle'")
    expr: str | None = Field(default=None, description="dimension / simple metric: row-level SQL expression on governed "
                                                       "columns; derived metric: arithmetic over metric names")
    metric_type: str | None = Field(default=None, description="metric: 'simple', 'ratio' or 'derived'")
    agg: str | None = Field(default=None, description="simple metric: sum, count, count_distinct, average, min, max")
    snapshot: bool | None = Field(default=None, description="simple metric: true = at each account's latest cycle "
                                                            "(balances, scores); false = summed over every cycle")
    numerator: str | None = Field(default=None, description="ratio: existing metric name")
    denominator: str | None = Field(default=None, description="ratio: existing metric name")
    metrics: list[str] | None = Field(default=None, description="derived: the metric names expr uses")
    choices: list[str] = Field(default_factory=list, description="Every interpretation you chose, for the user to see")
    synonyms: list[str] = Field(default_factory=list)


class Approval(BaseModel):
    approve: bool = Field(description="True to go ahead")


class PinnedTarget(ValueError):
    pass


def err(msg, **kw):
    return json.dumps({"error": True, "message": str(msg), **kw})


# ------------------------------------------------------------------ which target this conversation uses
def _pinned(ctx: Context | None) -> str | None:
    return _SESSIONS.get(id(ctx.session), {}).get("target") if ctx is not None else None


def _pin(ctx: Context, target: str) -> None:
    _SESSIONS.setdefault(id(ctx.session), {})["target"] = target


def _active_defs(ctx: Context | None) -> list:
    """Provisional definitions approved in this conversation (still provisional: promoted ones are governed)."""
    if ctx is None:
        return []
    ids = _SESSIONS.get(id(ctx.session), {}).get("definitions", [])
    if not ids:
        return []
    log = definitions.load_log()
    return [i for i in ids if log.get(i, {}).get("status") == "provisional"]


def _activate(ctx: Context, def_id: str) -> None:
    ids = _SESSIONS.setdefault(id(ctx.session), {}).setdefault("definitions", [])
    if def_id not in ids:
        ids.append(def_id)


def _catalog(ctx: Context | None):
    ids = _active_defs(ctx)
    return (definitions.overlay(ids)[0] or pipeline.catalog()) if ids else pipeline.catalog()


def _resolve(ctx: Context | None, target: str | None) -> str | None:
    """The target to use: the one asked for, else the one this conversation is pinned to, else the default.
    Once pinned (after an export), asking for a different target is refused until use_target switches."""
    pinned = _pinned(ctx)
    if pinned and target and target != pinned:
        raise PinnedTarget(f"This conversation is pinned to {pinned}; {target} was not queried. "
                           f"Ask to switch (use_target) if you really want to query {target}.")
    return target or pinned


def _target_info(name: str | None) -> dict:
    cfg = pipeline.targets()
    name = name or cfg["default_target"]
    t = cfg["targets"].get(name, {})
    info = {"target": name, "dialect": t.get("dialect"), "is_export": bool(t.get("export"))}
    if t.get("export"):
        e = exports.load_exports()[name]
        info.update({k: e.get(k) for k in ("where", "scope_text", "source", "months", "rows", "console_url", "expires")})
    return info


# ------------------------------------------------------------------ catalog
@mcp.resource("semantic://catalog")
def catalog_resource() -> str:
    """The governed metrics and dimensions with business meanings and synonyms, plus the query rules."""
    cat = pipeline.catalog()
    return "# Rules for building a semantic query\n\n" + query_rules(cat) + "\n\n" + cat.as_markdown()


def _short(text: str, limit: int = 110) -> str:
    """First sentence of a description, capped: keeps catalog listings small enough that chat clients
    pass them to the model directly (large tool results get saved to a file, and the model then tries to
    search that file with terminal commands)."""
    text = " ".join((text or "").split())
    first = text.split(". ")[0].rstrip(".")
    return first if len(first) <= limit else first[: limit - 3].rstrip() + "..."


@mcp.tool()
def list_metrics(ctx: Context) -> str:
    """Governed metrics (name -> label [kind]), the rules for building a query (latest month,
    time phrases, point-in-time filters, codes) and the target this conversation uses. Call this first.
    For the full meaning and synonyms of a metric or dimension, call search_catalog. Never invent metrics."""
    cat, target = _catalog(ctx), _pinned(ctx)
    metrics = {n: f"{v['label']} [{v['kind'] or 'n/a'}]{' PROVISIONAL' if v.get('provisional') else ''}"
               for n, v in cat.metrics.items()}
    return json.dumps({"active_target": _target_info(target), "rules": query_rules(pipeline.catalog(), target),
                       "metrics": metrics,
                       "next": "list_dimensions for group-by / filter names; search_catalog('<words>') for full "
                               "meanings and synonyms; list_definitions if no name fits."}, default=str)


@mcp.tool()
def list_dimensions(ctx: Context) -> str:
    """Dimensions you may group by or filter on (name -> short meaning), plus the time names.
    For the full meaning, codes and synonyms of a dimension, call search_catalog."""
    cat = _catalog(ctx)
    dims = {n: ("PROVISIONAL: " if v.get("provisional") else "") + _short(v["description"], 70)
            for n, v in cat.dimensions.items()}
    return json.dumps({"categorical": dims,
                       "time": [g for g in cat.groupable if g not in cat.dimensions]}, default=str)


@mcp.tool()
def search_catalog(query: str, ctx: Context) -> str:
    """Find governed metrics and dimensions by words (e.g. "behavior score improved", "paperless",
    "delinquency roll"): returns the best matches with their full meaning, codes and synonyms.
    Use this instead of reading files or running commands."""
    cat = _catalog(ctx)
    words = [w for w in "".join(c.lower() if c.isalnum() else " " for c in query).split() if len(w) > 2]
    if not words:
        return err("Give a few words to search for.")

    def score(name, info):
        hay = " ".join([name.replace("_", " "), info.get("label", ""), info.get("description", ""),
                        " ".join(info.get("synonyms", []))]).lower()
        return sum(3 if w in name else 1 for w in words if w in hay)

    hits = [("metric", n, v, score(n, v)) for n, v in cat.metrics.items()] + \
           [("dimension", n, v, score(n, v)) for n, v in cat.dimensions.items()]
    hits = sorted((h for h in hits if h[3] > 0), key=lambda h: -h[3])[:10]
    return json.dumps({"query": query, "matches": [
        {"type": t, "name": n, **({"label": v["label"], "kind": v["kind"]} if t == "metric" else {}),
         "description": v["description"], "synonyms": v.get("synonyms", []),
         **({"provisional": True} if v.get("provisional") else {})} for t, n, v, _ in hits],
        "note": "No match? The concept may be missing: see list_definitions." if not hits else ""}, default=str)


@mcp.tool()
def list_targets() -> str:
    """Query engines the same semantic layer can run on (warehouse, lake, oracle, and any exports)."""
    cfg = pipeline.targets()
    return json.dumps({"default": cfg["default_target"],
                       "targets": {k: {"dialect": v["dialect"], "kind": v["kind"], "export": bool(v.get("export"))}
                                   for k, v in cfg["targets"].items()}})


@mcp.tool()
async def get_dimension_values(dimension: str, ctx: Context, target: str | None = None) -> str:
    """Distinct values of a categorical dimension (use before filtering on codes)."""
    try:
        t, defs = _resolve(ctx, target), _active_defs(ctx)
        vals = await anyio.to_thread.run_sync(lambda: pipeline.dimension_values(dimension, t, definitions=defs))
        return json.dumps({"dimension": dimension, "values": vals}, default=str)
    except Exception as exc:  # noqa: BLE001
        return err(exc)


# ------------------------------------------------------------------ queries
@mcp.tool()
def compile_semantic_query(semantic_query: SemanticQuery, ctx: Context, target: str | None = None) -> str:
    """Preview: turn a semantic query into the SQL that WOULD run on a target. Executes nothing."""
    try:
        p = pipeline.plan(semantic_query.model_dump(), _resolve(ctx, target), definitions=_active_defs(ctx))
        return json.dumps({k: p[k] for k in ("target", "dialect", "summary", "notes", "provisional", "sql", "estimate",
                                             "engine_check", "simplified")}, default=str)
    except Exception as exc:  # noqa: BLE001 - unknown names, bad SQL, unreachable engine
        return err(exc)


@mcp.tool()
async def run_semantic_query(semantic_query: SemanticQuery, ctx: Context, target: str | None = None) -> str:
    """Compile, ask the human to approve (dialog), then run on the target. Returns up to 200 rows.
    Without a target it uses this conversation's target (the export, after export_for_analysis)."""
    try:
        sq, defs = semantic_query.model_dump(), _active_defs(ctx)
        p = await anyio.to_thread.run_sync(lambda: pipeline.plan(sq, _resolve(ctx, target), definitions=defs))
    except Exception as exc:  # noqa: BLE001
        return err(exc)
    if p["engine_check"] != "passed":
        return err(f"{p['target']} is not available ({p['engine_check']}); nothing was run.")
    est = p["estimate"] or {}
    try:
        res = await ctx.elicit(
            message=(f"Run this query on {p['target']} ({p['dialect']})?\n\nMeaning: {p['summary']}\n"
                     + "".join(f"Note: {n}\n" for n in p["notes"])
                     + f"SQL: {p['simplified']['note']}\n\n"
                     f"{p['sql']}\n\nEstimate: {est.get('note') or est or 'n/a'}"),
            schema=Approval)
    except Exception:  # noqa: BLE001
        return err("This client does not support approval dialogs; nothing was run.")
    if res.action != "accept" or not res.data or not res.data.approve:
        return err("Not approved by the user; nothing was run.")
    try:
        out = await anyio.to_thread.run_sync(lambda: pipeline.run(sq, p["target"], definitions=defs))
        extra = ({"governance": "PROVISIONAL: uses definitions approved in this conversation, not yet governed: "
                                + ", ".join(out["provisional"])} if out["provisional"] else {})
        return json.dumps({"target": out["target"], "summary": out["summary"], "notes": out["notes"], **extra,
                           **out["result"]}, default=str)
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@mcp.tool()
async def choose_target(question: str, ctx: Context) -> str:
    """Ask the user which engine should answer this question (same definitions on every engine).
    If the conversation is pinned to an export, that export is used without asking."""
    pinned = _pinned(ctx)
    if pinned:
        return json.dumps({"target": pinned, "note": "pinned to this export; use_target to switch"})
    cfg = pipeline.targets()
    names = list(cfg["targets"])
    Choice = create_model("TargetChoice", target=(str, Field(default=cfg["default_target"],
                                                             json_schema_extra={"enum": names})))
    try:
        res = await ctx.elicit(message=f'Where should this be answered?\n\n"{question}"\n\nOptions: '
                                       + ", ".join(f"{n} ({cfg['targets'][n]['dialect']})" for n in names),
                               schema=Choice)
    except Exception:  # noqa: BLE001
        return json.dumps({"target": cfg["default_target"], "note": "client cannot show dialogs; used default"})
    chosen = res.data.target if res.action == "accept" and res.data and res.data.target in names else cfg["default_target"]
    return json.dumps({"target": chosen})


# ------------------------------------------------------------------ exports
@mcp.tool()
async def export_for_analysis(question: str, ctx: Context, months_from: str | None = None,
                              months_to: str | None = None, account_filters: list[Filter] | None = None,
                              source: str | None = None, destination: str | None = None) -> str:
    """Copy the governed data behind a question so the user can analyse it, after the user approves.
    destination: 'local' (Parquet + a CSV in the project's exports folder) or 'bigquery'; default from
    targets.yaml. Scope: months_from / months_to
    (first day of a billing month, e.g. 2026-07-01) and account_filters on account__ dimensions
    (e.g. account__paperless = 'N'). Only governed columns are copied. Afterwards this conversation is
    pinned to the copy: all later questions run on it and the source is not queried again."""
    scope = {"months_from": months_from, "months_to": months_to,
             "account_filters": [f.model_dump() for f in account_filters or []]}
    try:
        src = source or _pinned(ctx)
        p = await anyio.to_thread.run_sync(lambda: exports.plan_export(scope, src, destination, question))
    except Exception as exc:  # noqa: BLE001
        return err(exc)
    try:
        res = await ctx.elicit(message=exports.approval_text(p), schema=Approval)
    except Exception:  # noqa: BLE001
        return err("This client does not support approval dialogs; nothing was exported.")
    if res.action != "accept" or not res.data or not res.data.approve:
        return err("Not approved by the user; nothing was exported.")
    try:
        entry = await anyio.to_thread.run_sync(exports.run_export, p)
    except Exception as exc:  # noqa: BLE001
        return err(f"Export failed: {exc}")
    _pin(ctx, entry["id"])
    return json.dumps({"export": entry["id"], "where": entry["where"], "files": entry.get("files"), "rows": entry["rows"],
                       "months": entry["months"], "expires": entry["expires"], "console_url": entry["console_url"],
                       "columns": entry["columns"],
                       "next": f"This conversation now uses {entry['id']}: follow-up questions run on the copy only."},
                      default=str)


@mcp.tool()
def list_exports() -> str:
    """Exports made so far: where the copy is, its scope, row counts, expiry and console link."""
    return json.dumps(exports.list_exports(), default=str)


@mcp.tool()
def use_target(target: str, ctx: Context) -> str:
    """Switch the target this conversation uses: an export id, or a source (warehouse, lake, oracle)
    to go back to it. Only call this when the user asks to switch."""
    cfg = pipeline.targets()
    if target not in cfg["targets"]:
        return err(f"Unknown target '{target}'. Options: {', '.join(cfg['targets'])}")
    _pin(ctx, target)
    return json.dumps({"active_target": _target_info(target)}, default=str)


@mcp.tool()
def current_target(ctx: Context) -> str:
    """The target this conversation's questions run on (and, for an export, its scope and location)."""
    return json.dumps({"active_target": _target_info(_pinned(ctx)), "pinned": _pinned(ctx) is not None}, default=str)


@mcp.tool()
async def delete_export(export_id: str, ctx: Context) -> str:
    """Delete an export's copied tables (after the user approves) and forget it."""
    if os.environ.get("CONVERSAI_PUBLIC_DEMO", "0") == "1":
        return err("Deleting extracts is disabled on the public demo; extracts expire automatically after 7 days.")
    try:
        res = await ctx.elicit(message=f"Delete export {export_id} and its copied tables?", schema=Approval)
    except Exception:  # noqa: BLE001
        return err("This client does not support approval dialogs; nothing was deleted.")
    if res.action != "accept" or not res.data or not res.data.approve:
        return err("Not approved; nothing was deleted.")
    try:
        msg = await anyio.to_thread.run_sync(exports.delete_export, export_id)
    except Exception as exc:  # noqa: BLE001
        return err(exc)
    if _pinned(ctx) == export_id:
        _SESSIONS.get(id(ctx.session), {}).pop("target", None)
        msg += " This conversation is back on the default target."
    return json.dumps({"message": msg})


# ------------------------------------------------------------------ proposed definitions
@mcp.tool()
def list_definitions(ctx: Context) -> str:
    """For questions no governed metric or dimension can express: how to write a proposed definition (kinds,
    rules, the governed columns you may use, examples), the provisional definitions already proposed (reuse one
    with use_definition when it fits) and the ones active in this conversation."""
    try:
        return json.dumps({"guide": definitions.guide(),
                           "provisional": [definitions.summary(e) for e in definitions.list_definitions("provisional")],
                           "active_in_this_conversation": _active_defs(ctx)}, default=str)
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@mcp.tool()
async def propose_definition(definition: DefinitionSpec, question: str, ctx: Context) -> str:
    """Propose a NEW dimension or metric when the governed layer cannot express the question (never SQL).
    The server checks it (governed columns and allowed functions only, MetricFlow compile, engine check) and
    the user approves it in a dialog. It then becomes a PROVISIONAL definition for this conversation: use its
    name (use_as) in run_semantic_query like any governed name; answers using it are labelled provisional.
    Call list_definitions first for the rules and the governed columns."""
    spec = definition.model_dump()
    t, active = _resolve(ctx, None), _active_defs(ctx)
    try:
        checked = await anyio.to_thread.run_sync(lambda: definitions.check(spec, active, t))
    except Exception as exc:  # noqa: BLE001 - the message says what to fix
        return err(f"Proposal rejected by the server checks: {exc}", hint="Fix the definition and propose again.")
    try:
        res = await ctx.elicit(message=definitions.approval_text(checked, question), schema=Approval)
    except Exception:  # noqa: BLE001
        return err("This client does not support approval dialogs; the definition was not saved.")
    if res.action != "accept" or not res.data or not res.data.approve:
        return err("The user did not approve the definition; it was not saved. Ask what they meant instead.")
    try:
        entry = await anyio.to_thread.run_sync(lambda: definitions.register(checked, question))
        _activate(ctx, entry["id"])
        defs = _active_defs(ctx)
        preview = await anyio.to_thread.run_sync(
            lambda: pipeline.run(checked["preview_query"], checked["target"], definitions=defs))
    except Exception as exc:  # noqa: BLE001
        return err(f"Saved, but the preview failed: {exc}")
    s = entry["spec"]
    return json.dumps({"definition": definitions.summary(entry), "status": "provisional (approved, not yet governed)",
                       "use_as": definitions.summary(entry)["use_as"],
                       "preview": {"query": checked["preview_query"], "columns": preview["result"]["columns"],
                                   "rows": preview["result"]["rows"]},
                       "next": f"Use {definitions.summary(entry)['use_as']} in run_semantic_query (the user approves "
                               f"the query as usual). Tell the user the answer relies on a provisional definition."
                               + (" It compares with the previous cycle: the first cycle shows n/a."
                                  if "LAG(" in (s.get("expr") or "").upper() else "")}, default=str)


@mcp.tool()
async def use_definition(definition_id: str, ctx: Context) -> str:
    """Reuse a provisional definition proposed earlier (ids from list_definitions) in this conversation,
    after the user approves it again."""
    try:
        entry = definitions.get(definition_id)
    except Exception as exc:  # noqa: BLE001
        return err(exc)
    if entry["status"] != "provisional":
        return err(f"{definition_id} is {entry['status']}"
                   + (": it is governed now, use its name directly." if entry["status"] == "promoted"
                      else f" ({entry.get('reason', '')}); do not use it."))
    try:
        res = await ctx.elicit(message=f"Use this provisional definition (not yet governed) in this conversation?\n\n"
                                       f"{entry['meaning']}\n\nProposed {entry['created_at'][:16]}"
                                       + (f" for: {entry['question']}" if entry.get("question") else "")
                                       + f"\n\n{entry['yaml']}", schema=Approval)
    except Exception:  # noqa: BLE001
        return err("This client does not support approval dialogs.")
    if res.action != "accept" or not res.data or not res.data.approve:
        return err("Not approved; the definition is not used.")
    _activate(ctx, definition_id)
    return json.dumps({"active": _active_defs(ctx), "use_as": definitions.summary(entry)["use_as"]})


@mcp.tool()
def drop_definition(definition_id: str, ctx: Context) -> str:
    """Stop using a provisional definition in this conversation (it stays in the log for the data owner)."""
    ids = _SESSIONS.get(id(ctx.session), {}).get("definitions", [])
    if definition_id in ids:
        ids.remove(definition_id)
    return json.dumps({"active": _active_defs(ctx)})


@mcp.tool()
def propose_from_question(question: str, ctx: Context, target: str | None = None) -> str:
    """Server-side: Claude turns the question into a semantic query (needs ANTHROPIC_API_KEY),
    then previews the SQL. Without a key, build the semantic query yourself from list_metrics /
    list_dimensions and call compile_semantic_query."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return err("ANTHROPIC_API_KEY not set on the server. Build the semantic query yourself.")
    try:
        from engine.nl import question_to_semantic_query
        t = _resolve(ctx, target)
        sq = question_to_semantic_query(question, pipeline.catalog(), target=t)
        p = pipeline.plan(sq, t)
        return json.dumps({k: p[k] for k in ("target", "semantic_query", "summary", "notes", "sql", "estimate", "engine_check")},
                          default=str)
    except Exception as exc:  # noqa: BLE001
        return err(exc)


@mcp.prompt()
def analyst(question: str) -> str:
    return f"""Answer using the conversai-semantic MCP server. Steps:
1. Call list_metrics (metrics + query rules + active target) and list_dimensions. Follow the rules exactly.
2. Call choose_target with the question unless the user already named an engine or the conversation is pinned.
3. Build a semantic query using ONLY governed names. Use get_dimension_values before filtering on codes.
   If no metric fits, say so - do not improvise.
4. Call run_semantic_query (the user approves in a dialog). Answer only from the returned rows.
5. If the user wants the data for their own analysis, call export_for_analysis with the question's scope.
6. If no governed name fits, call list_definitions and propose_definition (never SQL); say when an answer
   relies on a provisional definition.
Question: {question}"""


def _warm() -> None:
    """Load the catalog, the MetricFlow engine and the latest month in the background, so the first
    question is fast. Runs after the server is already answering, so start-up is not delayed."""
    try:
        cat = pipeline.catalog()
        warm_up()
        query_rules(cat)
        print("[conversai] ready", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - the first tool call will report it properly
        print(f"[conversai] warm-up failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    threading.Thread(target=_warm, daemon=True).start()
    mcp.run(transport="streamable-http" if "--http" in sys.argv else "stdio")
