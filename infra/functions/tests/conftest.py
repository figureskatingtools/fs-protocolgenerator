"""The backend modules import each other flatly (`import structure as st`), the
way the Functions host loads them, so the function app's directory has to be on
sys.path for the tests to import them the same way."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import platform_access as pa
import storage_helpers as sh
import structure as st


# ── the router's gate (PROXY-CONTRACT.md) ─────────────────────────────────────

PROXY_SECRET = "test-proxy-secret"
TID = "11111111-2222-4333-8444-555555555555"
OID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def auth_headers(email="organizer@example.com", admin=True, tid=TID, oid=OID, **extra):
    """Headers exactly as the site router forwards a signed-in user. Admin by
    default: route tests that are not about access then need no grants (an
    admin is "owner" everywhere, legacy unbound records included); the access
    rules themselves are covered by test_competition_access.py."""
    headers = {"x-proxy-secret": PROXY_SECRET,
               "x-forwarded-user-tid": tid,
               "x-forwarded-user-oid": oid}
    if email:
        headers["x-forwarded-user-email"] = email
    if admin:
        headers["x-forwarded-user-admin"] = "1"
    headers.update(extra)
    return headers


_HTTP_ROUTES = None


def http_routes():
    """{function name: handler} for every HTTP-triggered function. Built once:
    the SDK's `get_functions()` refuses a second call (duplicate names)."""
    global _HTTP_ROUTES
    if _HTTP_ROUTES is None:
        import function_app as fa
        _HTTP_ROUTES = {fn.get_function_name(): fn.get_user_function()
                        for fn in fa.app.get_functions()
                        if type(fn.get_trigger()).__name__ == "HttpTrigger"}
    return _HTTP_ROUTES


@pytest.fixture(autouse=True)
def proxy_gate(monkeypatch):
    """Every test runs with the gate configured, as deployed, and a cold
    access cache."""
    monkeypatch.setenv("PROXY_SHARED_SECRET", PROXY_SECRET)
    pa.clear_cache()
    yield
    pa.clear_cache()


# ── dict-backed stand-ins for blob storage ────────────────────────────────────

class FakeDownload:
    def __init__(self, data):
        self._data = data

    def readall(self):
        return self._data


class FakeBlobClient:
    def __init__(self, blobs, path):
        self._blobs = blobs
        self._path = path

    def exists(self):
        return self._path in self._blobs

    def download_blob(self):
        return FakeDownload(self._blobs[self._path])


class FakeContainerClient:
    """Just the surface the backend uses: blob path -> bytes."""

    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})

    def get_blob_client(self, path):
        return FakeBlobClient(self.blobs, path)

    def upload_blob(self, path, data, overwrite=False):
        if path in self.blobs and not overwrite:
            raise RuntimeError(f"blob exists: {path}")
        self.blobs[path] = data if isinstance(data, (bytes, bytearray)) else str(data).encode()

    def delete_blob(self, path):
        self.blobs.pop(path, None)


class FakeStorage:
    """This tool's own storage in memory: the `competitions` registry rows, the
    metadata.json structure documents (keyed by folder path) and the blob
    container uploads land in."""

    def __init__(self):
        self.rows = {}
        self.structures = {}
        self.container = FakeContainerClient()

    def competition(self, comp_id="abc12345", name="Spring Trophy 2026", **extra):
        """Seed a visible competition with an empty structure; returns the
        structure so a test can hang categories off it."""
        folder_path = f"{name}-{comp_id}"
        row = {
            "PartitionKey": "GLOBAL",
            "RowKey": comp_id,
            "Name": name,
            "FolderPath": folder_path,
            "Visible": True,
            "CreatedBy": "organizer@example.com",
            "CreatedDate": "2026-05-01T10:00:00Z",
        }
        row.update(extra)
        self.rows[comp_id] = row
        structure = st.new_structure(comp_id, name, "", "organizer@example.com",
                                     "2026-05-01T10:00:00Z")
        self.structures[folder_path] = structure
        return structure


@pytest.fixture
def storage(monkeypatch):
    """Wire `FakeStorage` under the storage helpers the routes call."""
    fake = FakeStorage()
    monkeypatch.setattr(sh, "get_competition_entity", lambda comp_id: fake.rows.get(comp_id))
    monkeypatch.setattr(sh, "read_structure", lambda folder_path: fake.structures.get(folder_path))
    monkeypatch.setattr(sh, "write_structure",
                        lambda folder_path, structure: fake.structures.__setitem__(folder_path, structure))
    monkeypatch.setattr(sh, "get_container_client", lambda: fake.container)
    return fake
