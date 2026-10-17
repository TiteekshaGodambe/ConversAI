"""Reads the compiled semantic layer (MetricFlow's semantic_manifest.json) and exposes it as a
catalog: governed metric and dimension names, their meaning and synonyms. Everything the model
is allowed to ask for comes from here."""

import json

from .config import MANIFEST, SEMANTIC_DIR, cli_error, run_cli

TIME_GRAINS = ["day", "week", "month", "quarter", "year"]


def ensure_manifest(force: bool = False) -> None:
    """(Re)build semantic_manifest.json with `dbt parse` when the YAML is newer than it."""
    newest_yaml = max(p.stat().st_mtime for p in SEMANTIC_DIR.rglob("*.yml"))
    if not force and MANIFEST.exists() and MANIFEST.stat().st_mtime >= newest_yaml:
        return
    r = run_cli("dbt.cli.main", ["parse", "--quiet"])
    if r.returncode != 0 or not MANIFEST.exists():
        raise RuntimeError("dbt parse failed:\n" + cli_error(r))


def _meta(obj) -> dict:
    return ((obj.get("config") or {}).get("meta") or obj.get("meta") or {})


class Catalog:
    def __init__(self, manifest: dict | None = None):
        """The governed catalog, from the compiled manifest. With `manifest` (the governed manifest plus
        provisional definitions, see engine.definitions) the catalog also knows those; they are marked
        provisional and listed in self.provisional."""
        if manifest is None:
            ensure_manifest()
            manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
        m = manifest
        self.metrics, self.hidden_metrics, self.dimensions, self.time_dimensions = {}, {}, {}, {}
        self.dimension_exprs, self.provisional = {}, {}
        for met in m["metrics"]:
            meta = _meta(met)
            if meta.get("provisional_id"):
                self.provisional[met["name"]] = {"id": meta["provisional_id"], "kind": "metric",
                                                  "description": " ".join((met.get("description") or "").split())}
            info = {
                "label": met.get("label") or met["name"],
                "description": " ".join((met.get("description") or "").split()),
                "type": met.get("type"),
                "kind": meta.get("kind", ""),
                "synonyms": meta.get("synonyms", []),
                "offset": any(i.get("offset_window") or i.get("offset_to_grain")
                              for i in ((met.get("type_params") or {}).get("metrics") or [])),
            }
            if meta.get("provisional_id"):
                info["provisional"] = True
            (self.hidden_metrics if meta.get("hidden") else self.metrics)[met["name"]] = info
        metric_time_grain = None
        for sm in m["semantic_models"]:
            primary = next((e["name"] for e in sm.get("entities", []) if e["type"] == "primary"), sm["name"])
            agg_time = ((sm.get("defaults") or {}).get("agg_time_dimension"))
            for d in sm.get("dimensions", []):
                name = f"{primary}__{d['name']}"
                info = {"description": " ".join((d.get("description") or "").split()), "model": sm["name"],
                        "synonyms": _meta(d).get("synonyms", [])}
                self.dimension_exprs[name] = d.get("expr") or d["name"]
                if _meta(d).get("provisional_id"):
                    info["provisional"] = True
                    self.provisional[name] = {"id": _meta(d)["provisional_id"], "kind": "dimension",
                                              "description": info["description"]}
                if d["type"] == "time":
                    grain = (d.get("type_params") or {}).get("time_granularity", "day")
                    info["grains"] = TIME_GRAINS[TIME_GRAINS.index(grain):]
                    self.time_dimensions[name] = info
                    if d["name"] == agg_time and (metric_time_grain is None
                                                  or TIME_GRAINS.index(grain) > TIME_GRAINS.index(metric_time_grain)):
                        metric_time_grain = grain
                else:
                    self.dimensions[name] = info
        self.metric_time_grains = TIME_GRAINS[TIME_GRAINS.index(metric_time_grain or "day"):]
        self.groupable = (sorted(self.dimensions)
                          + [f"metric_time__{g}" for g in self.metric_time_grains]
                          + [f"{t}__{g}" for t in sorted(self.time_dimensions) for g in self.time_dimensions[t]["grains"]])

    # ------------------------------------------------------------------ context for models
    def as_markdown(self) -> str:
        out = ["# Governed metrics", "",
               "kind: snapshot = value at a cycle (each account counted once, at its latest cycle in the period, "
               "or in each month when grouped by month); flow = summed over every cycle in the period.", ""]
        for n, v in self.metrics.items():
            syn = f" (also: {', '.join(v['synonyms'])})" if v["synonyms"] else ""
            kind = f" [{v['kind']}]" if v["kind"] else ""
            out.append(f"- `{n}`{kind}: {v['label']}. {v['description']}{syn}")
        out += ["", "# Dimensions (group by / filter)", ""]
        for n, v in self.dimensions.items():
            syn = f" (also: {', '.join(v['synonyms'])})" if v["synonyms"] else ""
            out.append(f"- `{n}`: {v['description']}{syn}")
        out += ["", "# Time", "",
                "- `metric_time__<grain>`: the metric's own date (the billing month for cycle metrics); "
                f"grains: {', '.join(self.metric_time_grains)}. Filter values are the first day of the period, "
                "e.g. metric_time__month = '2026-09-01'."]
        for n, v in self.time_dimensions.items():
            out.append(f"- `{n}__<grain>` (grains: {', '.join(v['grains'])}): {v['description']}")
        return "\n".join(out)

    def tool_schema(self) -> dict:
        """JSON schema for the model's `build_semantic_query` tool: names are fixed enums."""
        dims = self.groupable
        return {
            "type": "object",
            "properties": {
                "metrics": {"type": "array", "minItems": 1,
                            "items": {"type": "string", "enum": sorted(self.metrics)}},
                "group_by": {"type": "array", "items": {"type": "string", "enum": dims}},
                "where": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "dimension": {"type": "string", "enum": dims},
                        "op": {"type": "string", "enum": ["=", "!=", ">", ">=", "<", "<=", "in", "not in"]},
                        "value": {"description": "string, number, ISO date, or a list for in / not in"}},
                    "required": ["dimension", "op", "value"]}},
                "order_by": {"type": "array", "items": {"type": "string"},
                             "description": "metric or group_by names; prefix with - for descending"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
                "cannot_answer_reason": {"type": "string",
                                         "description": "Fill ONLY when no governed metric fits the question"},
            },
            "required": ["metrics"],
        }

    # ------------------------------------------------------------------ plain-English summary
    def describe(self, sq: dict) -> str:
        label = lambda n: self.metrics.get(n, {}).get("label", n)
        pretty = lambda n: (label(n) if n in self.metrics
                            else n.split("__", 1)[-1].replace("__", " by ").replace("_", " "))
        s = ", ".join(label(m) for m in sq["metrics"])
        if sq.get("group_by"):
            s += " by " + ", ".join(pretty(g) for g in sq["group_by"])
        if sq.get("where"):
            s += "; only where " + " and ".join(
                f"{pretty(w['dimension'])} {w['op']} {w['value']}" for w in sq["where"])
        if sq.get("order_by"):
            s += "; sorted by " + ", ".join(("descending " if o.startswith("-") else "") + pretty(o.lstrip("-"))
                                             for o in sq["order_by"])
        if sq.get("limit"):
            s += f"; top {sq['limit']}"
        return s
