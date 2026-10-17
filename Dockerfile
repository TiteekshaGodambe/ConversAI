# ConversAI MCP server for Cloud Run.
# Serves the governed semantic layer over MCP (streamable HTTP, path /mcp) on $PORT.
# The source data lives in BigQuery / Cloud Storage (targets "bigquery" and "gcs_lake"); exports go to
# BigQuery and the export list to Cloud Storage. No local warehouse, lake or exports are used in the cloud.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONUTF8=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY engine/ engine/
COPY semantic/ semantic/
COPY eval/ eval/
COPY mcp_server.py cli.py targets.yaml ./

# Compile the semantic layer once, at build time (semantic/target/semantic_manifest.json), and check it loads.
RUN python -c "from engine.catalog import ensure_manifest, Catalog; ensure_manifest(force=True); print(len(Catalog().metrics), 'metrics')"

# Cloud defaults (override with --set-env-vars at deploy time). GCP_PROJECT and CONVERSAI_REGISTRY are set by deploy.
ENV CONVERSAI_TARGETS=bigquery,gcs_lake \
    CONVERSAI_DEFAULT_TARGET=bigquery \
    CONVERSAI_EXPORT_DESTINATION=bigquery

EXPOSE 8080
CMD ["python", "mcp_server.py", "--http"]
