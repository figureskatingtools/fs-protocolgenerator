"""The router gate (PROXY-CONTRACT.md): the shared secret fails CLOSED, and the
caller's identity comes ONLY from the router's x-forwarded-user-* headers.

Every header a browser could forge on its own — Easy Auth's
X-MS-CLIENT-PRINCIPAL(-NAME), a Bearer JWT — must count for nothing: before
this change any of them was accepted as the acting user.
"""
import base64
import json

import azure.functions as func
import pytest

import function_app as fa
import storage_helpers as sh
from conftest import OID, PROXY_SECRET, TID, auth_headers, http_routes


def _request(headers, url="/api/check_user_permission"):
    return func.HttpRequest("GET", url, headers=headers, body=b"")


def _check(headers):
    return fa.check_user_permission._function.get_user_function()(_request(headers))


def _jwt(claims):
    def part(obj):
        return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")
    return f"{part({'alg': 'none'})}.{part(claims)}.sig"


# ── the shared secret ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("configured", [None, ""])
def test_an_unset_secret_rejects_everything(monkeypatch, configured):
    """Local dev used to fail open with the setting empty; now nothing gets in."""
    if configured is None:
        monkeypatch.delenv("PROXY_SHARED_SECRET", raising=False)
    else:
        monkeypatch.setenv("PROXY_SHARED_SECRET", configured)
    assert _check(auth_headers()).status_code == 401
    assert _check(auth_headers(**{"x-proxy-secret": ""})).status_code == 401


@pytest.mark.parametrize("secret", [None, "", "wrong", PROXY_SECRET + "x", PROXY_SECRET[:-1]])
def test_a_wrong_or_missing_secret_is_401(secret):
    headers = auth_headers()
    if secret is None:
        del headers["x-proxy-secret"]
    else:
        headers["x-proxy-secret"] = secret
    assert _check(headers).status_code == 401


def test_the_secret_is_compared_in_constant_time(monkeypatch):
    calls = []
    real = sh.hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(sh.hmac, "compare_digest", spy)
    assert _check(auth_headers()).status_code == 200
    assert calls == [(PROXY_SECRET.encode(), PROXY_SECRET.encode())]


def test_the_header_name_is_case_insensitive():
    headers = auth_headers()
    headers["X-Proxy-Secret"] = headers.pop("x-proxy-secret")
    assert _check(headers).status_code == 200


# ── identity: x-forwarded-user-* only ─────────────────────────────────────────

@pytest.mark.parametrize("missing", ["x-forwarded-user-tid", "x-forwarded-user-oid"])
def test_a_missing_tenant_or_object_id_is_401(missing):
    headers = auth_headers()
    del headers[missing]
    assert _check(headers).status_code == 401


@pytest.mark.parametrize("tid, oid", [
    ("not-a-guid", OID),
    (TID, "12345"),
    (TID, OID + "0"),
    ("", OID),
    (TID, "' or 1 eq 1"),
])
def test_an_invalid_tenant_or_object_id_is_401(tid, oid):
    assert _check(auth_headers(tid=tid, oid=oid)).status_code == 401


@pytest.mark.parametrize("spoof", [
    {"X-MS-CLIENT-PRINCIPAL-NAME": "victim@example.com"},
    {"X-MS-CLIENT-PRINCIPAL": base64.b64encode(json.dumps(
        {"userDetails": "victim@example.com", "userId": OID}).encode()).decode()},
    {"Authorization": "Bearer " + _jwt({"preferred_username": "victim@example.com",
                                        "oid": OID, "tid": TID})},
    {"x-forwarded-user-email": "victim@example.com"},
])
def test_spoofed_identity_headers_give_nothing(spoof):
    """With the right secret but without the router's tid/oid, no other header
    produces an identity — not Easy Auth's, not a token, not an e-mail."""
    headers = {"x-proxy-secret": PROXY_SECRET, **spoof}
    assert _check(headers).status_code == 401
    assert sh.get_caller(_request(headers)) is None


def test_spoofed_headers_never_override_the_forwarded_user():
    headers = auth_headers("organizer@example.com", **{
        "X-MS-CLIENT-PRINCIPAL-NAME": "victim@example.com",
        "Authorization": "Bearer " + _jwt({"preferred_username": "victim@example.com"}),
    })
    assert json.loads(_check(headers).get_body()) == {
        "allowed": True, "email": "organizer@example.com"}


def test_the_caller_is_read_from_the_forwarded_headers():
    headers = auth_headers(" Organizer@Example.COM ", admin=False,
                           tid=TID.upper(), oid=f" {OID} ",
                           **{"x-forwarded-user-name": "Matti%20Meik%C3%A4l%C3%A4inen"})
    caller = sh.get_caller(_request(headers))
    assert caller == sh.Caller(tid=TID, oid=OID, email="organizer@example.com",
                               name="Matti Meikäläinen", is_admin=False)


def test_the_email_is_optional():
    """E-mail is display/audit only; the identity is tid + oid."""
    caller = sh.get_caller(_request(auth_headers(email=None)))
    assert (caller.tid, caller.oid, caller.email) == (TID, OID, "")


@pytest.mark.parametrize("value, admin", [
    ("1", True), ("0", False), ("true", False), ("yes", False), (" 1", False), ("", False),
])
def test_admin_is_exactly_the_router_flag(value, admin):
    headers = auth_headers(admin=False, **{"x-forwarded-user-admin": value})
    assert sh.get_caller(_request(headers)).is_admin is admin


def test_no_admin_header_means_no_admin():
    assert sh.get_caller(_request(auth_headers(admin=False))).is_admin is False


def test_the_old_fallbacks_are_gone():
    assert not hasattr(sh, "get_user_email_from_header")
    assert not hasattr(sh, "_decode_jwt_payload")


# ── every route is gated ──────────────────────────────────────────────────────

@pytest.mark.parametrize("name, handler", sorted(http_routes().items()))
def test_every_route_but_health_rejects_a_request_without_the_secret(name, handler):
    headers = auth_headers()
    del headers["x-proxy-secret"]
    req = func.HttpRequest("POST", f"/api/{name}", headers=headers,
                           params={"id": "abc12345", "competition": "abc12345"}, body=b"{}")
    response = handler(req)
    if name == "health":
        assert response.status_code == 200
    else:
        assert response.status_code == 401, name
