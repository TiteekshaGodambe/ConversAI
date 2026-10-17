"""Question -> semantic query, using Claude with a tool call whose arguments are restricted to the
governed names in the catalog. The model never writes SQL.

Works with every Claude model: the tool call is forced (tool_choice = tool) where the model allows it;
models that reject forced tool choice (e.g. ones that reason before answering) get tool_choice = auto
plus an instruction to answer only through the tool, and are nudged again if they reply in prose."""

import os

from .catalog import Catalog
from .compiler import SemanticQueryError, compile_to_sql

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")
MAX_TOKENS = int(os.environ.get("ANTHROPIC_MAX_TOKENS", "8000"))
TOOL_NAME = "build_semantic_query"

RULES = """- Use ONLY the metric and dimension names listed below. Never invent names or formulas.
- Business terms are already defined by the metrics (e.g. "30+ delinquency rate", "utilization",
  "charge-off rate"); pick the metric, do not rebuild its logic with filters.
- Rates and shares are fractions (0.38 = 38%).
- Time: metric_time__month is the billing month. The latest month in the data is {latest}; "last month",
  "last cycle", "current" and "now" mean that month. "Last quarter" = the 3 months ending {latest};
  use metric_time__month >= first month and <= {latest}. Filter values are ISO dates of the first day.
- Point in time: when a question asks about the current state ("how many accounts are ...", "accounts with a
  score below ...") and filters on an account_cycle__ dimension, also filter metric_time__month = '{latest}'
  unless the question names another period. Without a month such filters match any cycle in the data.
- Account attributes (any account__ dimension: internal/external status, paperless, bankruptcy, charged_off)
  have one value per account and cover the whole book. Questions that only group or filter on them get NO
  month filter, unless the question names a period. Example: "How many accounts are charged off?" ->
  metrics [accounts], where account__charged_off = 'Y' (no metric_time filter).
- Codes: filter code dimensions with exact codes ('D', 'Z', '(blank)'), or use the *_desc dimension with
  the words shown in its description. Band values include their sort prefix (e.g. '3 >80%').
- One semantic query per call. If a question needs a breakdown AND the overall figure, return the
  breakdown; the overall figure is a follow-up query (same metric, no group_by).
- If no listed metric can answer the question (e.g. profit, revenue other than late fees, card numbers,
  names, addresses), still call the tool: put any metric in metrics and fill cannot_answer_reason instead of
  guessing. If a term is ambiguous (e.g. "risky"), fill cannot_answer_reason with the clarifying question to
  ask, listing the governed options."""

SYSTEM = """You translate business questions about a credit card portfolio into a semantic query.
Always answer by calling the build_semantic_query tool exactly once. Never answer in prose and never write SQL.
Rules:
{rules}

{catalog}"""

NUDGE = (f"Answer only by calling the {TOOL_NAME} tool. If no governed metric fits, call it with "
         "cannot_answer_reason filled in.")

_latest = {}            # target -> newest billing month in that target's data
_forced_ok = {}         # model -> False once it has rejected forced tool choice


def latest_month(cat: Catalog | None = None, target: str | None = None) -> str:
    """Newest billing month in a target's data, found through the semantic layer itself (engine-neutral).
    Exports can hold a narrower window than the source, so this is per target."""
    if target not in _latest:
        from .pipeline import run
        try:
            out = run({"metrics": ["accounts"], "group_by": ["metric_time__month"],
                       "order_by": ["-metric_time__month"], "limit": 1}, target)
            _latest[target] = str(out["result"]["rows"][0][0])[:10]
        except Exception:                                 # target down: let the model work without it
            return "the most recent month (unknown)"
    return _latest[target]


def query_rules(cat: Catalog | None = None, target: str | None = None) -> str:
    """The conventions for building a semantic query (time, point in time, codes, refusals), with the
    latest billing month of `target` filled in. Shared by the server-side model (below) and MCP clients
    such as Copilot (mcp_server.py), so both read questions the same way."""
    return RULES.format(latest=latest_month(cat, target))


def _create(client, anthropic, **kw):
    """messages.create with forced tool choice when the model supports it, else auto."""
    if _forced_ok.get(MODEL, True):
        try:
            return client.messages.create(tool_choice={"type": "tool", "name": TOOL_NAME}, **kw)
        except anthropic.BadRequestError as exc:
            if "tool_choice" not in str(exc):
                raise
            _forced_ok[MODEL] = False                     # remember for the rest of the run
    return client.messages.create(tool_choice={"type": "auto"}, **kw)


def question_to_semantic_query(question: str, cat: Catalog | None = None, history: list | None = None,
                               max_repairs: int = 2, target: str | None = None) -> dict:
    import anthropic

    cat = cat or Catalog()
    client = anthropic.Anthropic()
    tool = {"name": TOOL_NAME, "description": "Answer with a semantic query over governed metrics.",
            "input_schema": cat.tool_schema()}
    system = SYSTEM.format(rules=query_rules(cat, target), catalog=cat.as_markdown())
    messages = list(history or []) + [{"role": "user", "content": question}]
    for attempt in range(max_repairs + 1):
        resp = _create(client, anthropic, model=MODEL, max_tokens=MAX_TOKENS, system=system,
                       tools=[tool], messages=messages)
        call = next((b for b in resp.content if b.type == "tool_use" and b.name == TOOL_NAME), None)
        if call is None:                                  # replied in prose: ask again for the tool call
            if attempt == max_repairs:
                break
            messages += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": NUDGE}]
            continue
        sq = call.input
        try:
            compile_to_sql(sq, cat)          # proves it is valid before anyone sees it
            return sq
        except SemanticQueryError as exc:
            if attempt == max_repairs or sq.get("cannot_answer_reason"):
                raise
            messages += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "is_error": True,
                                                       "content": f"Invalid: {exc}. Fix the semantic query."}]}]
    raise SemanticQueryError("Could not build a valid semantic query.")
