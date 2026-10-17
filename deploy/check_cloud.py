"""
Check the ConversAI MCP server on Cloud Run, without VS Code.

  1) in one cmd window:   gcloud run services proxy conversai-mcp --region us-central1 --port 8080
  2) in another:          python deploy\\check_cloud.py            (or --url http://localhost:8080/mcp)

Connects over MCP (streamable HTTP) through the proxy, lists the tools and targets, compiles one governed
question to BigQuery SQL with a dry run (nothing is executed, so no approval dialog is needed), lists
the exports kept on Cloud Storage, reads the proposed-definitions log, and sends a test proposal through
the server's checks (whitelist, MetricFlow, BigQuery dry run) - this script then DECLINES the approval
dialog, so nothing is saved. Every line should say OK.
"""

import argparse
import asyncio
import json
import sys
import time

from mcp import ClientSession, types
from mcp.client.streamable_http import streamablehttp_client

QUESTION = {"metrics": ["dq30_rate", "total_payments"],
            "where": [{"dimension": "account__paperless", "op": "=", "value": "N"},
                      {"dimension": "metric_time__month", "op": ">=", "value": "2026-07-01"},
                      {"dimension": "metric_time__month", "op": "<=", "value": "2026-09-01"}]}


PROBE = {"name": "check_cloud_probe", "kind": "dimension", "model": "account_cycle",
         "description": "Test definition sent by deploy/check_cloud.py; the approval is declined, so it is never saved.",
         "expr": "CASE WHEN LAG(BUREAU_SCORE) OVER (PARTITION BY ACCOUNT_KEY ORDER BY BILLING_CYCLE_DATE) IS NULL "
                 "THEN 'n/a' WHEN BUREAU_SCORE < LAG(BUREAU_SCORE) OVER (PARTITION BY ACCOUNT_KEY ORDER BY "
                 "BILLING_CYCLE_DATE) THEN 'down' ELSE 'not down' END"}
dialogs = []


async def decline(context, params):
    dialogs.append(params.message)
    return types.ElicitResult(action="decline")


async def check(url: str) -> bool:
    ok = True

    def line(good, label, detail=""):
        nonlocal ok
        ok &= bool(good)
        print(f"  {'OK  ' if good else 'FAIL'}  {label}" + (f"  {detail}" if detail else ""))

    t0 = time.time()
    async with streamablehttp_client(url, timeout=120) as (read, write, _):
        async with ClientSession(read, write, elicitation_callback=decline) as s:
            init = await s.initialize()
            line(True, "connected", f"{init.serverInfo.name} in {time.time() - t0:.1f}s (first call after idle can take ~30s)")
            tools = [t.name for t in (await s.list_tools()).tools]
            line(len(tools) >= 17 and "propose_definition" in tools, "tools", f"{len(tools)}: {', '.join(tools)}"
                 + ("" if "propose_definition" in tools else "  <- old version deployed: run deploy\\gcp_deploy.cmd"))

            targets = json.loads((await s.call_tool("list_targets", {})).content[0].text)
            names = [k for k, v in targets["targets"].items() if not v.get("export")]
            line(set(names) == {"bigquery", "gcs_lake"}, "this is the CLOUD server",
                 f"targets {names}, default {targets['default']} (a local server would list warehouse, lake, oracle)")

            p = json.loads((await s.call_tool("compile_semantic_query", {"semantic_query": QUESTION})).content[0].text)
            if p.get("error"):
                line(False, "BigQuery dry run", p["message"][:300])
            else:
                est = p.get("estimate") or {}
                line(p["dialect"] == "bigquery" and p["engine_check"] == "passed", "BigQuery dry run",
                     f"target {p['target']}, {est.get('note', est)}")

            ex = json.loads((await s.call_tool("list_exports", {})).content[0].text)
            line(isinstance(ex, list), "export list (Cloud Storage)", f"{len(ex)} export(s)")

            if "propose_definition" in tools:
                d = json.loads((await s.call_tool("list_definitions", {})).content[0].text)
                line("guide" in d, "definitions log (Cloud Storage)",
                     d.get("message", f"{len(d.get('provisional', []))} provisional definition(s)"))
                r = json.loads((await s.call_tool("propose_definition", {"definition": PROBE,
                                                                         "question": "check_cloud probe"})).content[0].text)
                passed = bool(dialogs) and "bigquery check: ok" in dialogs[-1]
                line(passed and "did not approve" in r.get("message", ""), "proposal checks on BigQuery",
                     "whitelist + MetricFlow + dry run passed, approval declined, nothing saved" if passed
                     else r.get("message", "")[:300])
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8080/mcp")
    a = ap.parse_args()
    print(f"\nChecking {a.url}\n")
    try:
        good = asyncio.run(check(a.url))
    except Exception as exc:  # noqa: BLE001
        print(f"  FAIL  could not talk to the server: {type(exc).__name__}: {exc}\n"
              "        Is the proxy running?  gcloud run services proxy conversai-mcp --region us-central1 --port 8080")
        sys.exit(1)
    print("\nAll good: VS Code can use this server." if good else "\nSomething failed: see the FAIL lines.")
    sys.exit(0 if good else 1)


if __name__ == "__main__":
    main()
