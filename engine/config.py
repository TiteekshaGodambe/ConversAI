import json
import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SEMANTIC_DIR = ROOT / "semantic"
MANIFEST = SEMANTIC_DIR / "target" / "semantic_manifest.json"
MAX_ROWS = 200


EXPORTS_DIR = Path(os.environ.get("CONVERSAI_EXPORTS_DIR", ROOT / "exports"))
EXPORT_REGISTRY = EXPORTS_DIR / "registry.json"
_RELATIVE_PARQUET = re.compile(r"read_parquet\('(?![A-Za-z]:|/)")


REGISTRY_URI = os.environ.get("CONVERSAI_REGISTRY", "")      # gs://bucket/path.json on Cloud Run; else local
DEFINITIONS_FILE = ROOT / "proposals" / "definitions.json"   # local log of proposed (provisional) definitions


# ------------------------------------------------------------------ small JSON documents (local file or gs://)
def _gcs_blob(uri: str | None = None):
    from google.cloud import storage
    uri = uri or REGISTRY_URI
    bucket, _, path = uri[len("gs://"):].partition("/")
    return storage.Client().bucket(bucket).blob(path or "conversai/exports.json")


def load_doc(where) -> dict:
    """A JSON object kept in a local file or on Cloud Storage (gs://bucket/path.json); {} if missing."""
    if str(where).startswith("gs://"):
        from google.api_core.exceptions import NotFound
        try:
            return json.loads(_gcs_blob(str(where)).download_as_text(encoding="utf-8"))
        except NotFound:
            return {}
    path = Path(where)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_doc(where, doc: dict) -> None:
    text = json.dumps(doc, indent=2, default=str)
    if str(where).startswith("gs://"):
        _gcs_blob(str(where)).upload_from_string(text, content_type="application/json")
        return
    path = Path(where)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def update_doc(where, change, what: str = "document") -> dict:
    """Read-modify-write with change(doc). On Cloud Storage the write only succeeds if nobody else wrote in
    between (generation match); otherwise it re-reads and retries."""
    if not str(where).startswith("gs://"):
        doc = load_doc(where)
        change(doc)
        save_doc(where, doc)
        return doc
    from google.api_core.exceptions import NotFound, PreconditionFailed
    blob = _gcs_blob(str(where))
    for _ in range(5):
        try:
            blob.reload()
            generation, doc = blob.generation, json.loads(blob.download_as_text(encoding="utf-8"))
        except NotFound:
            generation, doc = 0, {}
        change(doc)
        try:
            blob.upload_from_string(json.dumps(doc, indent=2, default=str), content_type="application/json",
                                    if_generation_match=generation)
            return doc
        except PreconditionFailed:
            continue
    raise RuntimeError(f"Could not update the {what} (too many concurrent changes); try again.")


# ------------------------------------------------------------------ export registry
def _registry():
    return REGISTRY_URI if REGISTRY_URI.startswith("gs://") else EXPORT_REGISTRY


def load_exports() -> dict:
    """Exports made so far: id -> entry, each with its own target config. Kept in exports/registry.json,
    or on Cloud Storage when CONVERSAI_REGISTRY=gs://... (Cloud Run: the container's disk is temporary)."""
    return load_doc(_registry())


def save_exports(registry: dict) -> None:
    save_doc(_registry(), registry)


def update_exports(change) -> dict:
    """Read-modify-write the export registry (safe against concurrent writers on Cloud Storage)."""
    return update_doc(_registry(), change, "export registry")


# ------------------------------------------------------------------ proposed definitions log
def definitions_location() -> str:
    """Where the log of proposed definitions lives: CONVERSAI_DEFINITIONS (a path or gs://...), else next to
    the export registry on Cloud Storage (same folder, definitions.json), else proposals/definitions.json."""
    explicit = os.environ.get("CONVERSAI_DEFINITIONS", "")
    if explicit:
        return explicit
    if REGISTRY_URI.startswith("gs://"):
        return REGISTRY_URI.rsplit("/", 1)[0] + "/definitions.json"
    return str(DEFINITIONS_FILE)


def load_targets() -> dict:
    """targets.yaml plus one target per export (named after the export id). Relative Parquet paths
    are resolved against the project folder."""
    cfg = yaml.safe_load((ROOT / "targets.yaml").read_text(encoding="utf-8"))
    only = [t.strip() for t in os.environ.get("CONVERSAI_TARGETS", "").split(",") if t.strip()]
    if only:                              # e.g. on Cloud Run: only the BigQuery targets exist there
        cfg["targets"] = {k: v for k, v in cfg["targets"].items() if k in only}
    cfg["default_target"] = os.environ.get("CONVERSAI_DEFAULT_TARGET") or cfg["default_target"]
    if cfg["default_target"] not in cfg["targets"]:
        raise ValueError(f"default target '{cfg['default_target']}' is not one of: {', '.join(cfg['targets'])}")
    for eid, e in load_exports().items():
        if e.get("status") == "ready":
            cfg["targets"][eid] = {**e["target"], "export": True}
    root = ROOT.as_posix()
    for t in cfg["targets"].values():
        t["tables"] = {k: _RELATIVE_PARQUET.sub(f"read_parquet('{root}/", v) for k, v in t["tables"].items()}
        project = t.get("project") or os.environ.get(t.get("project_env") or "GCP_PROJECT")
        if project:                       # BigQuery targets name tables `{project}.dataset.table`
            t["tables"] = {k: v.replace("{project}", project) for k, v in t["tables"].items()}
        if t.get("path") and t["path"] != ":memory:":
            t["path"] = str(ROOT / t["path"])
    return cfg


def python_cli(module: str, func: str = "cli") -> list:
    """Run a console entry point with the current interpreter (works on Windows and Linux)."""
    return [sys.executable, "-c", f"import sys; from {module} import {func}; sys.argv[0] = 'cli'; sys.exit({func}())"]


def run_cli(module: str, args: list) -> subprocess.CompletedProcess:
    """Run dbt / MetricFlow as a child process, always in UTF-8.

    On Windows a piped child process otherwise writes in the legacy code page (cp1252), and
    MetricFlow's progress symbols cannot be encoded, so the CLI crashes before printing the SQL.
    """
    env = {**os.environ, "DBT_PROFILES_DIR": str(SEMANTIC_DIR), "NO_COLOR": "1",
           "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(python_cli(module) + args, cwd=SEMANTIC_DIR, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def cli_error(r: subprocess.CompletedProcess, limit: int = 1500) -> str:
    """The useful part of a failed dbt / MetricFlow run: its ERROR lines, else the tail of the output."""
    text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", (r.stdout or "") + "\n" + (r.stderr or ""))
    start = text.find("ERROR")
    if start < 0:
        return text.strip()[-limit:]
    body = text[start:].split("Log File::")[0].split("If you think you found a bug")[0]
    return body.strip()[:limit]
