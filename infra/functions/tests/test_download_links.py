"""Generated-protocol download links are minted per request, after the access
check, read-only and valid for 15 minutes; nothing long-lived is stored or
returned, so a revoked user is left with at most a 15-minute link. Names are
invented."""
import base64
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit, unquote

import azure.functions as func
import pytest
from azure.core import MatchConditions
from azure.core.exceptions import ResourceModifiedError, ResourceNotFoundError
from azure.data.tables import UpdateMode
from azure.storage.blob import UserDelegationKey

import function_app as fa
import platform_access as pa
import platform_clock as pc
import storage_helpers as sh
from conftest import OID, TID, auth_headers

PID = "0b5c7e0e-6f3a-4d55-9c1e-2a7d8b9c0d11"
USER_ID = "c0ffee00-1234-4abc-8def-0123456789ab"
COMP = "abc12345"
ACCOUNT = "stfsprotocoltest"
OLD_URL = (f"https://{ACCOUNT}.blob.core.windows.net/fs-protocolgenerator/"
           "elsewhere/protocols/old.pdf?sv=2023&se=2099-01-01&sp=rwd&sig=stolen")


class FakeTable:
    """Rows by (PartitionKey, RowKey); `get_entity` raises like the SDK."""

    def __init__(self, rows=None):
        self.rows = {(r["PartitionKey"], r["RowKey"]): dict(r) for r in rows or []}
        self.upserts = []

    def create_table(self):
        pass

    def get_entity(self, partition_key, row_key):
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[(partition_key, row_key)])

    def query_entities(self, query):
        pk = query.split("PartitionKey eq '")[1].rstrip("'")
        return [dict(r) for (p, _), r in self.rows.items() if p == pk]

    def upsert_entity(self, entity, mode=UpdateMode.MERGE):
        self.upserts.append((dict(entity), mode))
        key = (entity["PartitionKey"], entity["RowKey"])
        if mode == UpdateMode.REPLACE or key not in self.rows:
            self.rows[key] = dict(entity)
        else:
            self.rows[key].update(entity)

    def update_entity(self, entity, mode=UpdateMode.MERGE, etag=None, match_condition=None):
        """Like the SDK: never creates a row (If-Match "*" by default), and
        IfNotModified needs the row's current etag (here: its stored "etag")."""
        key = (entity["PartitionKey"], entity["RowKey"])
        if key not in self.rows:
            raise ResourceNotFoundError(entity["RowKey"])
        if match_condition == MatchConditions.IfNotModified and self.rows[key].get("etag") != etag:
            raise ResourceModifiedError("etag mismatch")
        if mode == UpdateMode.REPLACE:
            self.rows[key] = dict(entity)
        else:
            self.rows[key].update(entity)


class FakeBlobService:
    """The surface `mint_read_urls` and `generate_protocol` use."""

    def __init__(self, container):
        self.account_name = ACCOUNT
        self.url = f"https://{ACCOUNT}.blob.core.windows.net/"
        self.container = container
        self.key_requests = []

    def get_user_delegation_key(self, start, expiry):
        self.key_requests.append((start, expiry))
        key = UserDelegationKey()
        key.signed_oid = OID
        key.signed_tid = TID
        key.signed_start = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_expiry = expiry.strftime("%Y-%m-%dT%H:%M:%SZ")
        key.signed_service = "b"
        key.signed_version = "2023-11-03"
        key.value = base64.b64encode(b"k" * 32).decode()
        return key

    def get_container_client(self, name):
        assert name == sh.CONTAINER_NAME
        return self.container


class Access:
    def __init__(self):
        self.rows = {}

    def get_entity(self, partition_key, row_key):
        if (partition_key, row_key) not in self.rows:
            raise ResourceNotFoundError(row_key)
        return dict(self.rows[(partition_key, row_key)])


@pytest.fixture
def world(storage, monkeypatch):
    monkeypatch.setenv("AzureWebJobsStorage__accountName", ACCOUNT)
    monkeypatch.setattr(pc, "get_platform_table_client", lambda: None)
    identities, access = Access(), Access()
    identities.rows[("IDENTITY", f"{TID}_{OID}")] = {"UserId": USER_ID}
    monkeypatch.setattr(pa, "get_identities_table_client", lambda: identities)
    monkeypatch.setattr(pa, "get_access_table_client", lambda: access)
    storage.grant = lambda role: access.rows.__setitem__((PID, USER_ID), {"Role": role})
    storage.revoke = lambda: access.rows.pop((PID, USER_ID), None)

    storage.competition(COMP, PlatformId=PID)
    storage.folder = storage.rows[COMP]["FolderPath"]
    storage.generated = FakeTable()
    monkeypatch.setattr(sh, "get_table_client",
                        lambda table_name="generatedprotocols":
                        storage.generated if table_name == "generatedprotocols" else None)
    storage.blob_service = FakeBlobService(storage.container)
    monkeypatch.setattr(sh, "get_blob_service_client", lambda: storage.blob_service)
    monkeypatch.setattr(fa, "assemble_protocol", lambda structure, get_bytes: b"%PDF-1.7")
    return storage


def details(headers):
    req = func.HttpRequest("GET", "/api/get_competition_details", headers=headers,
                           params={"id": COMP}, body=b"")
    return fa.get_competition_details._function.get_user_function()(req)


def generate(headers):
    req = func.HttpRequest("POST", "/api/generate_protocol", headers=headers, params={},
                           body=json.dumps({"id": COMP}).encode())
    return fa.generate_protocol._function.get_user_function()(req)


def user():
    return auth_headers(admin=False)


def sas_of(url):
    parts = urlsplit(url)
    return unquote(parts.path), {k: v[0] for k, v in parse_qs(parts.query).items()}


def at(value):
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def test_a_generated_protocol_is_listed_without_a_stored_link(world):
    world.grant("write")
    response = generate(user())
    assert response.status_code == 200, response.get_body()
    name = json.loads(response.get_body())["fileName"]
    assert f"{world.folder}/protocols/{name}" in world.container.blobs
    [(row, mode)] = world.generated.upserts
    assert mode == UpdateMode.REPLACE
    assert row["FileName"] == name and row["PartitionKey"] == COMP
    assert "Url" not in row and "ExpirationDate" not in row
    # Generating signs nothing: links are minted only when someone looks.
    assert world.blob_service.key_requests == []


def test_the_protocol_is_named_after_the_competition_code(world):
    """FS Manager style: `<competition code>_protocol.pdf`, no timestamp in the
    name. When it was generated travels beside it (`generatedAt`)."""
    world.rows[COMP]["Code"] = "spring-trophy-2026"
    world.grant("write")
    body = json.loads(generate(user()).get_body())
    assert body["fileName"] == "spring-trophy-2026_protocol.pdf"
    [(row, _)] = world.generated.upserts
    assert row["GeneratedAt"] == body["generatedAt"]
    assert datetime.strptime(body["generatedAt"], "%Y-%m-%dT%H:%M:%SZ")
    [entry] = json.loads(details(user()).get_body())["generatedFiles"]
    assert (entry["fileName"], entry["generatedAt"]) == (body["fileName"], body["generatedAt"])


def test_regenerating_replaces_the_protocol(world):
    world.rows[COMP]["Code"] = "spring-trophy-2026"
    world.grant("write")
    generate(user())
    generate(user())
    assert [b for b in world.container.blobs if "/protocols/" in b] == [
        f"{world.folder}/protocols/spring-trophy-2026_protocol.pdf"]
    assert len(world.generated.rows) == 1


def test_without_a_code_the_cleaned_name_names_the_protocol(world):
    world.structures[world.folder]["name"] = "Spring Trophy: 2026!"
    world.grant("write")
    body = json.loads(generate(user()).get_body())
    assert body["fileName"] == "Spring_Trophy_2026_protocol.pdf"


def test_a_legacy_row_has_no_generation_time(world):
    world.grant("read")
    world.generated.rows[(COMP, "p.pdf")] = {
        "PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf"}
    [entry] = json.loads(details(user()).get_body())["generatedFiles"]
    assert entry["generatedAt"] is None


@pytest.mark.parametrize("code, name, expected", [
    ("winter-cup-2026", "Winter Cup", "winter-cup-2026_protocol.pdf"),
    ("../../x y", "Winter Cup", "x-y_protocol.pdf"),
    ("", "Winter Cup 2026", "Winter_Cup_2026_protocol.pdf"),
    (None, "", "competition_protocol.pdf"),
])
def test_protocol_file_name(code, name, expected):
    assert sh.protocol_file_name(code, name) == expected


def test_details_mint_a_fresh_read_only_15_minute_link(world):
    world.grant("read")
    world.generated.rows[(COMP, "p.pdf")] = {
        "PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf", "FileSize": 2048,
        "Description": "Competition protocol (PDF)"}
    before = datetime.now(timezone.utc).replace(microsecond=0)
    response = details(user())
    after = datetime.now(timezone.utc)
    assert response.status_code == 200, response.get_body()
    [entry] = json.loads(response.get_body())["generatedFiles"]
    assert entry["fileName"] == "p.pdf" and entry["size"] == 2048

    path, sas = sas_of(entry["url"])
    assert entry["url"].startswith(f"https://{ACCOUNT}.blob.core.windows.net/")
    assert path == f"/{sh.CONTAINER_NAME}/{world.folder}/protocols/p.pdf"
    assert sas["sp"] == "r"                      # read, nothing else
    assert sas["sr"] == "b"                      # this one blob
    expiry = at(sas["se"])
    assert before + timedelta(minutes=15) <= expiry <= after + timedelta(minutes=15)
    assert at(sas["st"]) <= before
    assert abs(datetime.fromisoformat(entry["expiration"].replace("Z", "+00:00"))
               - expiry) < timedelta(seconds=1)
    # The delegation key the link rests on expires with it.
    [(_, key_expiry)] = world.blob_service.key_requests
    assert key_expiry - after <= timedelta(minutes=15)


def test_each_request_mints_its_own_link(world):
    world.grant("read")
    world.generated.rows[(COMP, "p.pdf")] = {
        "PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf"}
    details(user())
    details(user())
    assert len(world.blob_service.key_requests) == 2


def test_a_stored_legacy_link_is_ignored_and_scrubbed(world):
    world.grant("read")
    world.generated.rows[(COMP, "old.pdf")] = {
        "PartitionKey": COMP, "RowKey": "old.pdf", "FileName": "old.pdf",
        "Url": OLD_URL, "ExpirationDate": "2099-01-01T00:00:00",
        "Description": "Competition protocol (PDF)", "FileSize": 10}
    body = json.loads(details(user()).get_body())
    [entry] = body["generatedFiles"]
    assert "stolen" not in json.dumps(body) and "elsewhere" not in json.dumps(body)
    path, sas = sas_of(entry["url"])
    assert path == f"/{sh.CONTAINER_NAME}/{world.folder}/protocols/old.pdf"
    assert at(sas["se"]) - datetime.now(timezone.utc) <= timedelta(minutes=15)
    row = world.generated.rows[(COMP, "old.pdf")]
    assert "Url" not in row and "ExpirationDate" not in row
    assert row["FileName"] == "old.pdf" and row["FileSize"] == 10


class _Read(dict):
    """A table entity as the SDK returns it: a dict with `.metadata["etag"]`."""

    def __init__(self, row, etag):
        super().__init__(row)
        self.metadata = {"etag": etag}


def test_a_scrub_never_recreates_a_protocol_deleted_meanwhile():
    """details read the legacy row, then delete_protocol removed it before the
    scrub ran: the scrub must not bring the row back."""
    table = FakeTable()
    stale = _Read({"PartitionKey": COMP, "RowKey": "old.pdf", "FileName": "old.pdf",
                   "Url": OLD_URL, "ExpirationDate": "2099-01-01T00:00:00"}, "W/1")
    sh.scrub_stored_link(table, stale)
    assert table.rows == {} and table.upserts == []


def test_a_scrub_never_overwrites_a_row_rewritten_meanwhile():
    table = FakeTable([{"PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf",
                        "FileSize": 99, "etag": "W/2"}])
    stale = _Read({"PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf",
                   "FileSize": 10, "Url": OLD_URL}, "W/1")
    sh.scrub_stored_link(table, stale)
    assert table.rows[(COMP, "p.pdf")]["FileSize"] == 99


def test_a_scrub_with_the_current_etag_drops_the_link():
    row = {"PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf",
           "Url": OLD_URL, "ExpirationDate": "2099-01-01T00:00:00", "etag": "W/1"}
    table = FakeTable([row])
    sh.scrub_stored_link(table, _Read(row, "W/1"))
    stored = table.rows[(COMP, "p.pdf")]
    assert "Url" not in stored and "ExpirationDate" not in stored


def test_the_blob_is_derived_inside_the_competition_folder(world):
    world.grant("read")
    world.generated.rows[(COMP, "x")] = {
        "PartitionKey": COMP, "RowKey": "x", "FileName": "../../other-comp/protocols/x.pdf"}
    [entry] = json.loads(details(user()).get_body())["generatedFiles"]
    path, _ = sas_of(entry["url"])
    assert path == f"/{sh.CONTAINER_NAME}/{world.folder}/protocols/x.pdf"
    assert entry["fileName"] == "x.pdf"


@pytest.mark.parametrize("grant", [None, "revoked"])
def test_no_link_is_minted_without_access(world, grant):
    world.generated.rows[(COMP, "p.pdf")] = {
        "PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf"}
    if grant == "revoked":
        world.grant("read")
        assert details(user()).status_code == 200
        world.revoke()
        pa.clear_cache()   # stands in for the 30 s expiry
        world.blob_service.key_requests.clear()
    response = details(user())
    assert response.status_code == 404
    assert json.loads(response.get_body())["error"] == "platform_not_found"
    assert world.blob_service.key_requests == []


def test_a_link_that_cannot_be_signed_is_null(world, monkeypatch):
    world.grant("read")
    world.generated.rows[(COMP, "p.pdf")] = {
        "PartitionKey": COMP, "RowKey": "p.pdf", "FileName": "p.pdf"}

    def boom(start, expiry):
        raise RuntimeError("no delegation key")

    monkeypatch.setattr(world.blob_service, "get_user_delegation_key", boom)
    response = details(user())
    assert response.status_code == 200
    [entry] = json.loads(response.get_body())["generatedFiles"]
    assert entry["url"] is None and entry["expiration"] is None
    assert entry["fileName"] == "p.pdf"


def test_an_account_key_signs_the_same_short_read_link(monkeypatch):
    """Local development (Azurite connection string) takes the account-key path."""
    monkeypatch.delenv("AzureWebJobsStorage__accountName", raising=False)
    monkeypatch.setenv("AzureWebJobsStorage",
                       "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
                       f"AccountKey={base64.b64encode(b'x' * 64).decode()};"
                       "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;")
    service = FakeBlobService(None)
    service.url = "http://127.0.0.1:10000/devstoreaccount1"
    now = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
    urls, expiry = sh.mint_read_urls(service, ["Spring Trophy-abc/protocols/p.pdf"], now=now)
    assert expiry == now + timedelta(minutes=15)
    [url] = urls.values()
    path, sas = sas_of(url)
    assert path == f"/devstoreaccount1/{sh.CONTAINER_NAME}/Spring Trophy-abc/protocols/p.pdf"
    assert sas["sp"] == "r" and sas["se"] == "2026-10-17T12:15:00Z"
    assert sas["st"] == "2026-10-17T11:55:00Z"
    assert service.key_requests == []


def test_nothing_to_sign_without_credentials(monkeypatch):
    monkeypatch.delenv("AzureWebJobsStorage__accountName", raising=False)
    monkeypatch.delenv("AzureWebJobsStorage", raising=False)
    assert sh.mint_read_urls(FakeBlobService(None), ["a/protocols/p.pdf"]) == ({}, None)
    assert sh.mint_read_urls(None, ["a/protocols/p.pdf"]) == ({}, None)
