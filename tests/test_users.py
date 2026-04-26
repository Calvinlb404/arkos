"""Tests for base_module/users.py — demo-login schema, validation, and routes.

The DB-backed `_find_or_create_user` is patched out so the tests run without
a live Postgres. The HTTP route tests cover validation, the success path,
and the DB-error fallback.
"""

from __future__ import annotations

from unittest.mock import patch

import psycopg2
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from base_module.jwt_utils import decode_token
from base_module.users import (
    _USERNAME_RE,
    DemoLoginRequest,
    LoginResponse,
    MeResponse,
    router,
)


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Schemas + regex
# ---------------------------------------------------------------------------


class TestUsernameRegex:
    @pytest.mark.parametrize(
        "name",
        ["nate", "alice_99", "bob.smith", "x-y", "ab", "x" * 64, "User_123"],
    )
    def test_accepts_valid(self, name):
        assert _USERNAME_RE.match(name)

    @pytest.mark.parametrize(
        "name",
        [
            "",  # empty
            "a",  # too short
            "x" * 65,  # too long
            "has space",  # space
            "has@sign",  # special char
            "emoji-😀",  # non-ASCII
            "tab\tname",
        ],
    )
    def test_rejects_invalid(self, name):
        assert not _USERNAME_RE.match(name)


class TestDemoLoginRequest:
    def test_minimal_valid(self):
        req = DemoLoginRequest(username="nate")
        assert req.username == "nate"

    def test_min_length_2(self):
        with pytest.raises(ValueError):
            DemoLoginRequest(username="a")

    def test_max_length_64(self):
        with pytest.raises(ValueError):
            DemoLoginRequest(username="x" * 65)


class TestLoginResponse:
    def test_round_trip(self):
        resp = LoginResponse(token="abc.def.ghi", user_id="u-1", username="alice")
        assert resp.token == "abc.def.ghi"
        assert resp.user_id == "u-1"
        assert resp.username == "alice"


class TestMeResponse:
    def test_round_trip(self):
        resp = MeResponse(user_id="u-1", username="alice")
        assert resp.user_id == "u-1"
        assert resp.username == "alice"


# ---------------------------------------------------------------------------
# /auth/demo-login route
# ---------------------------------------------------------------------------


class TestDemoLoginRoute:
    def test_creates_user_and_returns_token(self, client):
        with patch("base_module.users._find_or_create_user", return_value=("uuid-1", "alice")):
            resp = client.post("/auth/demo-login", json={"username": "alice"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["user_id"] == "uuid-1"
        assert body["username"] == "alice"
        # Token must decode and embed the user_id we got back.
        payload = decode_token(body["token"])
        assert payload["sub"] == "uuid-1"
        assert payload["username"] == "alice"

    def test_rejects_invalid_username_format(self, client):
        resp = client.post("/auth/demo-login", json={"username": "bad name"})
        assert resp.status_code == 400
        assert "username must match" in resp.json()["detail"]

    def test_rejects_too_short(self, client):
        # Pydantic catches this first, returns 422
        resp = client.post("/auth/demo-login", json={"username": "a"})
        assert resp.status_code == 422

    def test_strips_whitespace(self, client):
        with patch("base_module.users._find_or_create_user", return_value=("u", "trim")) as mock_fn:
            resp = client.post("/auth/demo-login", json={"username": "  trim  "})
        assert resp.status_code == 200
        # The inner DB call should see the trimmed form.
        mock_fn.assert_called_once_with("trim")

    def test_db_error_returns_500(self, client):
        err = psycopg2.OperationalError("connection refused")
        with patch("base_module.users._find_or_create_user", side_effect=err):
            resp = client.post("/auth/demo-login", json={"username": "alice"})
        assert resp.status_code == 500
        assert "db error" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# /auth/me route
# ---------------------------------------------------------------------------


class TestMeRoute:
    def test_returns_user_from_bearer(self, client):
        # Issue a real token rather than mocking the dependency, so we cover
        # the JWT roundtrip in a single integration-ish unit test.
        from base_module.jwt_utils import issue_token

        token = issue_token("u-42", "alice")
        resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"user_id": "u-42", "username": "alice"}

    def test_rejects_missing_token(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_rejects_invalid_token(self, client):
        resp = client.get("/auth/me", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401
