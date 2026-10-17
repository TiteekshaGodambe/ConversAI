"""A stand-in for google.cloud.storage, for tests without a Google Cloud project.

Objects live in memory (text) or in a temp folder (uploaded files); generations are tracked so
if_generation_match behaves like Cloud Storage (PreconditionFailed when someone wrote in between).

  import tests.fake_gcs as fg; fg.install()"""

import shutil
import sys
import tempfile
import types
from pathlib import Path

from google.api_core.exceptions import NotFound, PreconditionFailed

STATE = {"objects": {}, "buckets": set(), "dir": None}     # (bucket, name) -> {"gen", "text" | "path"}


def local_path(uri: str) -> Path | None:
    """gs://bucket/name -> the temp file holding that uploaded object (used by the fake BigQuery)."""
    bucket, _, name = uri[len("gs://"):].partition("/")
    obj = STATE["objects"].get((bucket, name))
    return Path(obj["path"]) if obj and "path" in obj else None


def glob(uri_pattern: str) -> list:
    import fnmatch
    bucket, _, pattern = uri_pattern[len("gs://"):].partition("/")
    return [Path(o["path"]) for (b, n), o in STATE["objects"].items()
            if b == bucket and "path" in o and fnmatch.fnmatch(n, pattern)]


class Blob:
    def __init__(self, bucket, name, chunk_size=None):
        self.bucket_name, self.name, self.generation, self.chunk_size = bucket, name, None, chunk_size

    @property
    def size(self):
        obj = self._obj()
        return Path(obj["path"]).stat().st_size if obj and "path" in obj else None

    def _obj(self):
        return STATE["objects"].get((self.bucket_name, self.name))

    def reload(self):
        obj = self._obj()
        if obj is None:
            raise NotFound(f"gs://{self.bucket_name}/{self.name}")
        self.generation = obj["gen"]

    def download_as_text(self, encoding="utf-8"):
        obj = self._obj()
        if obj is None:
            raise NotFound(f"gs://{self.bucket_name}/{self.name}")
        return obj["text"]

    def _put(self, if_generation_match, **data):
        cur = self._obj()
        cur_gen = cur["gen"] if cur else 0
        if if_generation_match is not None and if_generation_match != cur_gen:
            raise PreconditionFailed(f"generation {cur_gen} != {if_generation_match}")
        STATE["objects"][(self.bucket_name, self.name)] = {"gen": cur_gen + 1, **data}
        self.generation = cur_gen + 1

    def upload_from_string(self, text, content_type=None, if_generation_match=None):
        self._put(if_generation_match, text=text)

    def upload_from_filename(self, filename, if_generation_match=None, timeout=None):
        STATE.setdefault("uploads", []).append(self.name)
        if STATE["dir"] is None:
            STATE["dir"] = tempfile.mkdtemp(prefix="fake_gcs_")
        dest = Path(STATE["dir"]) / self.bucket_name / self.name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(filename, dest)
        self._put(if_generation_match, path=str(dest))


class Bucket:
    def __init__(self, name):
        self.name = name

    def blob(self, name, chunk_size=None):
        return Blob(self.name, name, chunk_size)

    def get_blob(self, name):
        b = Blob(self.name, name)
        return b if b._obj() is not None else None


class Client:
    def __init__(self, project=None):
        self.project = project

    def bucket(self, name):
        return Bucket(name)

    def lookup_bucket(self, name):
        return Bucket(name) if name in STATE["buckets"] else None

    def create_bucket(self, name, location=None):
        STATE["buckets"].add(name)
        return Bucket(name)


def install():
    import importlib
    for pkg in ("google", "google.cloud"):
        try:
            importlib.import_module(pkg)
        except ImportError:
            mod = types.ModuleType(pkg)
            mod.__path__ = []
            sys.modules[pkg] = mod
    storage = types.ModuleType("google.cloud.storage")
    storage.Client, storage.Bucket, storage.Blob = Client, Bucket, Blob
    sys.modules["google.cloud.storage"] = storage
    sys.modules["google.cloud"].storage = storage
    return STATE
