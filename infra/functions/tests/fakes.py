"""In-memory Table Storage and Blob Storage doubles for the backend tests.

Modelled on the platform API's doubles in the figureskatingtools-site repo
(`infra/functions/tests/fakes.py`), trimmed and extended to what this app's
routes depend on:

  * `create_entity` raises `ResourceExistsError` on a duplicate key and
    `get_entity`/`delete_entity` raise `ResourceNotFoundError` on a missing one.
  * `update_entity` merges by default and replaces with `UpdateMode.REPLACE`.
  * `query_entities` understands `and`-joined `<Column> eq|ge|lt '<value>'`
    clauses — every filter this app issues (`PartitionKey eq 'GLOBAL' and
    PlatformId eq '…'`, `RowKey eq '<token>'`, `SentBy eq '…' and SentDate ge
    '…'`). A row lacking the column never matches, like Table Storage.
  * blobs support prefix listing, metadata returned ONLY when `list_blobs` was
    asked for it, and `ResourceNotFoundError` on a missing blob.
"""
import re
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError


class FakeEntity(dict):
    """A dict with `.metadata`, like azure-data-tables' TableEntity."""

    def __init__(self, mapping=None, metadata=None):
        super().__init__(mapping or {})
        self._metadata = dict(metadata or {})

    @property
    def metadata(self):
        return self._metadata


_CLAUSE_RE = re.compile(r"(\w+) (eq|ge|lt) '((?:[^']|'')*)'")


def _parse_filter(query_filter):
    clauses = [(col, op, value.replace("''", "'"))
               for col, op, value in _CLAUSE_RE.findall(query_filter or "")]
    if query_filter and not clauses:
        raise AssertionError(f"fake cannot parse filter: {query_filter!r}")
    return clauses


def _matches(row, clauses):
    for column, op, value in clauses:
        if column not in row:
            return False
        actual = str(row[column])
        if op == "eq" and actual != value:
            return False
        if op == "ge" and actual < value:
            return False
        if op == "lt" and actual >= value:
            return False
    return True


class FakeTableClient:
    """Minimal azure-data-tables TableClient stand-in backed by a dict."""

    def __init__(self, name="table"):
        self.name = name
        self.rows: dict[tuple[str, str], dict] = {}
        self.calls: list[tuple] = []
        # Set to an exception instance to make every get_entity call raise it.
        self.fail_get: Exception | None = None
        self.fail_next_update: Exception | None = None

    def create_table(self):
        self.calls.append(("create_table",))

    def create_entity(self, entity):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.calls.append(("create_entity", key))
        if key in self.rows:
            raise ResourceExistsError("entity already exists")
        self.rows[key] = dict(entity)

    def get_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        self.calls.append(("get_entity", key))
        if self.fail_get is not None:
            raise self.fail_get
        if key not in self.rows:
            raise ResourceNotFoundError("entity not found")
        return FakeEntity(self.rows[key], {"etag": "fake"})

    def update_entity(self, entity, mode=None, **kwargs):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.calls.append(("update_entity", key, dict(entity)))
        if self.fail_next_update is not None:
            error, self.fail_next_update = self.fail_next_update, None
            raise error
        if key not in self.rows:
            raise ResourceNotFoundError("entity not found")
        if str(getattr(mode, "value", mode)).lower() == "replace":
            self.rows[key] = dict(entity)
        else:
            self.rows[key].update(entity)

    def upsert_entity(self, entity, mode=None):
        key = (entity["PartitionKey"], entity["RowKey"])
        self.calls.append(("upsert_entity", key))
        if str(getattr(mode, "value", mode)).lower() == "replace":
            self.rows[key] = dict(entity)
        else:
            self.rows.setdefault(key, {}).update(entity)

    def delete_entity(self, partition_key, row_key):
        key = (partition_key, row_key)
        self.calls.append(("delete_entity", key))
        if key not in self.rows:
            raise ResourceNotFoundError("entity not found")
        del self.rows[key]

    def query_entities(self, query_filter, select=None, **kwargs):
        self.calls.append(("query_entities", query_filter))
        clauses = _parse_filter(query_filter)
        return [FakeEntity(row) for _key, row in sorted(self.rows.items())
                if _matches(row, clauses)]

    # -- test conveniences ------------------------------------------------
    def put(self, **row):
        self.rows[(row["PartitionKey"], row["RowKey"])] = dict(row)
        return row

    def row(self, partition_key, row_key):
        return self.rows.get((partition_key, row_key))

    def updates(self):
        return [call for call in self.calls if call[0] == "update_entity"]


class FakeBlob:
    def __init__(self, name, data, metadata, last_modified):
        self.name = name
        self.data = bytes(data)
        self.metadata = dict(metadata or {})
        self.last_modified = last_modified

    def properties(self, *, with_metadata=True):
        return SimpleNamespace(
            name=self.name,
            size=len(self.data),
            metadata=dict(self.metadata) if with_metadata else {},
            last_modified=self.last_modified,
        )


class FakeDownloader:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class FakeBlobClient:
    def __init__(self, container, blob_name):
        self._container = container
        self.blob_name = blob_name

    def exists(self):
        return self.blob_name in self._container.blobs

    def download_blob(self):
        blob = self._container.blobs.get(self.blob_name)
        if blob is None:
            raise ResourceNotFoundError("blob not found")
        return FakeDownloader(blob.data)

    def upload_blob(self, data, overwrite=False, metadata=None, **kwargs):
        self._container.upload_blob(self.blob_name, data, overwrite=overwrite, metadata=metadata)

    def get_blob_properties(self):
        blob = self._container.blobs.get(self.blob_name)
        if blob is None:
            raise ResourceNotFoundError("blob not found")
        return blob.properties()


class FakeContainerClient:
    def __init__(self, container_name):
        self.container_name = container_name
        self.blobs: dict[str, FakeBlob] = {}
        self._clock = datetime(2026, 1, 2, 10, 0, 0, tzinfo=timezone.utc)

    def _tick(self):
        self._clock += timedelta(seconds=1)
        return self._clock

    def upload_blob(self, name, data, overwrite=False, metadata=None, **kwargs):
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("blob already exists")
        if hasattr(data, "read"):
            data = data.read()
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.blobs[name] = FakeBlob(name, data, metadata, self._tick())

    def list_blobs(self, name_starts_with=None, include=None):
        with_metadata = bool(include) and "metadata" in include
        return [blob.properties(with_metadata=with_metadata)
                for name, blob in sorted(self.blobs.items())
                if not name_starts_with or name.startswith(name_starts_with)]

    def delete_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("blob not found")
        del self.blobs[name]

    def download_blob(self, name):
        return FakeBlobClient(self, name).download_blob()

    def get_blob_client(self, name):
        return FakeBlobClient(self, name)


class FakeBlobServiceClient:
    account_name = "stfakejudgepapers"

    def __init__(self):
        self.containers: dict[str, FakeContainerClient] = {}

    def get_container_client(self, container_name):
        return self.containers.setdefault(container_name, FakeContainerClient(container_name))
