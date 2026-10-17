"""
Local command line for the semantic engine.

  python cli.py catalog
  python cli.py sql     @examples/dq30_trend.json --target oracle
  python cli.py query   '{"metrics":["dq30_rate"],"group_by":["account__internal_status_desc"]}' --target warehouse
  python cli.py compare @examples/apr_by_score_band.json --targets warehouse,lake
  python cli.py ask     "What is the 30+ delinquency rate by external status?"   (needs ANTHROPIC_API_KEY)

Exports (copy governed data for your own analysis; the copy becomes a target):
  python cli.py export  --from 2026-07-01 --to 2026-09-01 --filter account__paperless=N --dest bigquery
  python cli.py export  --dest local                     (everything, as local Parquet: offline demo)
  python cli.py exports                                  (list them)
  python cli.py query   @examples/dq30_trend.json --target export_20261012_184500_3fa
  python cli.py verify-export export_20261012_184500_3fa   (copy vs source, all gold questions)
  python cli.py delete-export export_20261012_184500_3fa

Proposed definitions (concepts the governed layer does not have yet; see engine/definitions.py):
  python cli.py definitions list                       (provisional ones first, with how often they are used)
  python cli.py definitions show    <id>               (meaning, choices, the YAML it would add)
  python cli.py definitions propose @examples/definitions/bureau_score_drop_50.json --question "..."
  python cli.py query   '{"metrics":["accounts"],"group_by":["account_cycle__bureau_score_drop_50"]}' --definitions <id>
  python cli.py definitions promote <id>               (data owner: write it into semantic/models, re-parse)
  python cli.py definitions reject  <id> --reason "..."
On Cloud Run the log is on Cloud Storage: set CONVERSAI_REGISTRY=gs://<project>-conversai-lake/conversai/exports.json
(the definitions log is definitions.json in the same folder) before these commands.
"""

import argparse
import json
import sys

from engine import pipeline


def show_table(res):
    cols, rows = res["columns"], res["rows"]
    widths = [max(len(str(c)), *(len(f"{r[i]}") for r in rows)) if rows else len(c) for i, c in enumerate(cols)]
    print("  ".join(str(c).ljust(w) for c, w in zip(cols, widths)))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print("  ".join((f"{v:.4f}" if isinstance(v, float) else str(v)).ljust(w) for v, w in zip(r, widths)))
    if res["truncated"]:
        print("... (truncated)")


def load_sq(arg):
    """A semantic query as JSON text, or @path/to/file.json (easier on Windows shells)."""
    if arg.startswith("@"):
        with open(arg[1:], encoding="utf-8") as fh:
            return json.load(fh)
    return json.loads(arg)


def approve_and_run(sq, target, yes, definitions=None):
    p = pipeline.plan(sq, target, definitions=definitions)
    print(f"\nTarget   : {p['target']} ({p['dialect']})")
    print(f"Meaning  : {p['summary']}")
    for n in p["notes"]:
        print(f"Note     : {n}")
    print(f"Check    : {p['engine_check']}" + (f"  estimate {p['estimate']}" if p["estimate"] else ""))
    if p["engine_check"] != "passed":
        print("Engine not available; not running.")
        return
    print(f"\n{p['sql']}\n")
    if not yes and input("Run this query? [y/N] ").strip().lower() != "y":
        print("Not run.")
        return
    show_table(pipeline.run(sq, target, definitions=definitions)["result"])


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog")
    sr = sub.add_parser("simplify-report")
    sr.add_argument("--target")
    for name in ("sql", "query"):
        s = sub.add_parser(name)
        s.add_argument("semantic_query")
        s.add_argument("--target")
        s.add_argument("--yes", action="store_true")
        s.add_argument("--definitions", default="", help="comma-separated provisional definition ids to use")
        s.add_argument("--original", action="store_true", help="sql: also print MetricFlow's SQL before simplification")
    c = sub.add_parser("compare")
    c.add_argument("semantic_query")
    c.add_argument("--targets", default="warehouse,lake")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--target")
    a.add_argument("--yes", action="store_true")
    e = sub.add_parser("export")
    e.add_argument("--from", dest="months_from", help="first billing month, e.g. 2026-07-01")
    e.add_argument("--to", dest="months_to", help="last billing month, e.g. 2026-09-01")
    e.add_argument("--filter", action="append", default=[],
                   help="account filter dimension=value, e.g. account__paperless=N (repeatable)")
    e.add_argument("--source", help="target to copy from (default: targets.yaml default_target)")
    e.add_argument("--dest", choices=["bigquery", "local"], help="default: targets.yaml exports.destination")
    e.add_argument("--question", default="", help="the question this export is for (stored as a description)")
    e.add_argument("--yes", action="store_true")
    sub.add_parser("exports")
    v = sub.add_parser("verify-export")
    v.add_argument("export_id")
    d = sub.add_parser("delete-export")
    d.add_argument("export_id")
    df = sub.add_parser("definitions")
    df.add_argument("action", choices=["list", "show", "propose", "promote", "reject"])
    df.add_argument("arg", nargs="?", help="definition id (show/promote/reject) or @file.json / JSON (propose)")
    df.add_argument("--reason", default="")
    df.add_argument("--question", default="")
    df.add_argument("--target")
    df.add_argument("--all", action="store_true", help="list: include promoted and rejected")
    df.add_argument("--yes", action="store_true")
    args = ap.parse_args()
    defs = [d for d in getattr(args, "definitions", "").split(",") if d.strip()]

    if args.cmd == "catalog":
        print(pipeline.catalog().as_markdown())
    elif args.cmd == "sql":
        p = pipeline.plan(load_sq(args.semantic_query), args.target, check=False, definitions=defs)
        print(f"-- {p['summary']}\n-- target: {p['target']} ({p['dialect']})")
        print("".join(f"-- note: {n}\n" for n in p["notes"]) + f"-- sql: {p['simplified']['note']}\n" + p["sql"])
        if args.original and p["sql_original"]:
            print("\n-- original MetricFlow SQL (before simplification):\n" + p["sql_original"])
    elif args.cmd == "query":
        approve_and_run(load_sq(args.semantic_query), args.target, args.yes, defs)
    elif args.cmd == "definitions":
        definitions_command(args)
    elif args.cmd == "simplify-report":
        simplify_report(args.target)
    elif args.cmd == "compare":
        sq, results = load_sq(args.semantic_query), {}
        for t in args.targets.split(","):
            results[t] = pipeline.run(sq, t)["result"]
            print(f"\n== {t} ==")
            show_table(results[t])
        norm = lambda r: sorted(tuple(round(v, 6) if isinstance(v, float) else str(v)[:10] for v in row) for row in r["rows"])
        same = len({json.dumps(norm(r)) for r in results.values()}) == 1
        print(f"\nAll targets agree: {same}")
        sys.exit(0 if same else 1)
    elif args.cmd == "export":
        from engine import exports
        filters = []
        for f in args.filter:
            dim, _, val = f.partition("=")
            filters.append({"dimension": dim.strip(), "op": "=", "value": val.strip()})
        p = exports.plan_export({"months_from": args.months_from, "months_to": args.months_to,
                                 "account_filters": filters}, args.source, args.dest, args.question)
        print("\n" + exports.approval_text(p) + "\n")
        if not args.yes and input("Export? [y/N] ").strip().lower() != "y":
            print("Not exported.")
            return
        entry = exports.run_export(p)
        print(f"Exported: {entry['id']}  ({entry['rows']['account']:,} accounts, {entry['rows']['account_cycle']:,} cycles)")
        if entry.get("console_url"):
            print(f"BigQuery console: {entry['console_url']}")
        for f in entry.get("files") or []:
            print(f"File: {f}")
        print(f"Query it:  python cli.py query <semantic query> --target {entry['id']}")
    elif args.cmd == "exports":
        from engine import exports
        for x in exports.list_exports():
            print(f"{x['id']}  {x['destination']:8s}  {x['rows']}  {x['scope_text']}  months {x['months']}"
                  + (f"  expires {str(x['expires'])[:10]}" if x.get("expires") else ""))
    elif args.cmd == "verify-export":
        from engine import exports
        e = exports.load_exports()[args.export_id]
        print(f"\nCross-check {args.export_id}: copy vs {e['source']} with the export's scope as filters "
              f"({e['scope_text']})\n")
        res = exports.verify_export(args.export_id)
        for r in res:
            print(f"  {r['status']:8s} {r['id']:5s} {r['detail']}")
        counts = {s: sum(r["status"] == s for r in res) for s in ("SAME", "EXPECTED", "DIFF")}
        print(f"\n{counts['SAME']} same, {counts['EXPECTED']} expected differences, {counts['DIFF']} real differences")
        sys.exit(1 if counts["DIFF"] else 0)
    elif args.cmd == "delete-export":
        from engine import exports
        print(exports.delete_export(args.export_id))
    elif args.cmd == "ask":
        from engine.nl import question_to_semantic_query
        sq = question_to_semantic_query(args.question, pipeline.catalog())
        print("Semantic query:", json.dumps(sq))
        approve_and_run(sq, args.target, args.yes)


def simplify_report(target):
    """Every gold question: MetricFlow's SQL vs the simplified SQL on one target - same rows? faster? fewer bytes?"""
    import time
    import yaml
    from engine.targets import make_target
    gold = yaml.safe_load(open("eval/gold.yaml", encoding="utf-8"))["gold"]
    name = target or pipeline.targets()["default_target"]
    tgt = make_target(pipeline.targets()["targets"][name])
    is_bq = pipeline.targets()["targets"][name]["kind"] == "bigquery"

    def timed(sql):
        best, rows = 1e9, None
        for _ in range(1 if is_bq else 3):
            t0 = time.perf_counter()
            rows = tgt.execute(sql, 100000)["rows"]
            best = min(best, time.perf_counter() - t0)
        return best * 1000, rows

    def key(rows):
        return sorted(tuple(round(float(v), 6) if isinstance(v, (int, float)) and not isinstance(v, bool) else str(v)
                            for v in r) for r in rows)
    print(f"\nTarget {name}: MetricFlow SQL vs simplified SQL\n")
    print(f"{'q':5s} {'chars':>13s} {'ms':>15s} " + (f"{'MB scanned':>17s} " if is_bq else "") + "result")
    totals = [0.0, 0.0]
    for qid, g in gold.items():
        for sq in g.get("queries", []):
            p = pipeline.plan(sq, name)
            if not p["simplified"]["applied"]:
                print(f"{qid:5s} {p['simplified']['note']}")
                continue
            ta, ra = timed(p["sql_original"])
            tb, rb = timed(p["sql"])
            totals[0] += ta
            totals[1] += tb
            mb = ""
            if is_bq:
                a, b = tgt.validate(p["sql_original"])["est_bytes"], tgt.validate(p["sql"])["est_bytes"]
                mb = f"{a / 1e6:7.1f}->{b / 1e6:7.1f} "
            print(f"{qid:5s} {len(p['sql_original']):6d}->{len(p['sql']):6d} {ta:7.1f}->{tb:7.1f} {mb}"
                  + ("same" if key(ra) == key(rb) else "DIFFERENT"))
    print(f"\nTotal time {totals[0]:.0f} ms -> {totals[1]:.0f} ms"
          + (" (BigQuery bills bytes scanned; time varies with caching and slots)" if is_bq else ""))


def definitions_command(args):
    from engine import definitions as D
    if args.action == "list":
        rows = D.list_definitions(None if args.all else "provisional")
        if not rows:
            print("No provisional definitions." + ("" if args.all else " (--all shows promoted / rejected)"))
        for e in sorted(rows, key=lambda e: (e["status"] != "provisional", -e.get("uses", 0))):
            sm = D.summary(e)
            print(f"{e['id']:44s} {e['status']:11s} uses {sm['uses']:<4} {sm['use_as']}\n    {sm['description']}"
                  + (f"\n    reason: {e['reason']}" if e.get("reason") else ""))
        print(f"\n(log: {D.definitions_location()})")
        return
    if not args.arg:
        sys.exit("Give a definition id (or, for propose, the definition as @file.json or JSON).")
    if args.action == "show":
        e = D.get(args.arg)
        print(f"{e['id']}  [{e['status']}]  proposed {e['created_at']}  uses {e.get('uses', 0)}")
        if e.get("question"):
            print(f"Asked: {e['question']}")
        print(f"\n{e['meaning']}\n\n{e['yaml']}")
    elif args.action == "propose":
        checked = D.check(load_sq(args.arg), target=args.target)
        print("\n" + D.approval_text(checked, args.question) + "\n")
        if not args.yes and input("Approve? [y/N] ").strip().lower() != "y":
            print("Not saved.")
            return
        entry = D.register(checked, args.question, approved_via="command line")
        print(f"Saved as {entry['id']} (provisional). Preview:")
        show_table(pipeline.run(checked["preview_query"], checked["target"], definitions=[entry["id"]])["result"])
        print(f"\nUse it:  python cli.py query <semantic query using {D.summary(entry)['use_as']}> "
              f"--definitions {entry['id']}")
    elif args.action == "promote":
        e = D.get(args.arg)
        print(f"Promote {e['id']} into the governed layer?\n\n{e['meaning']}\n\n{e['yaml']}\n")
        if not args.yes and input("Promote? [y/N] ").strip().lower() != "y":
            print("Not promoted.")
            return
        out = D.promote(args.arg)
        print(f"Promoted. Changed: {', '.join(out['files'])}; semantic manifest rebuilt.\n"
              "Next: add a gold question for it (eval/questions.yaml + eval/gold.yaml), run\n"
              "  python eval/run_eval.py\n"
              "then redeploy (deploy\\gcp_deploy.cmd) so the cloud server has it as a governed definition.")
    elif args.action == "reject":
        if not args.reason:
            sys.exit("Give --reason, so the agent does not propose the same thing again.")
        e = D.reject(args.arg, args.reason)
        print(f"Rejected {e['id']}: {e['reason']}")


if __name__ == "__main__":
    main()
