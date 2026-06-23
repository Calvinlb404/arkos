"""Tests for base_module/users.py — demo-login schema, validation, and routes.

The DB-backed `_find_or_create_user` is patched out so the tests run without
a live Postgres. The HTTP route tests cover validation, the success path,
and the DB-error fallback.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

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
    SlackConnectRequest,
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
        assert resp.slack_user_id is None

    def test_slack_user_id_stored(self):
        resp = MeResponse(user_id="u-1", username="alice", slack_user_id="U012AB3CD")
        assert resp.slack_user_id == "U012AB3CD"


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
        from base_module.jwt_utils import issue_token

        token = issue_token("u-42", "alice")
        with patch("base_module.users._get_slack_user_id", return_value=None):
            resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"user_id": "u-42", "username": "alice", "slack_user_id": None}

    def test_returns_slack_user_id_when_linked(self, client):
        from base_module.jwt_utils import issue_token

        token = issue_token("u-42", "alice")
        with patch("base_module.users._get_slack_user_id", return_value="U012AB3CD"):
            resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert resp.json()["slack_user_id"] == "U012AB3CD"

    def test_rejects_missing_token(self, client):
        resp = client.get("/auth/me")
        assert resp.status_code == 401

    def test_rejects_invalid_token(self, client):
        resp = client.get("/auth/me", headers={"Authorization": "Bearer garbage"})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# SlackConnectRequest schema
# ---------------------------------------------------------------------------


class TestSlackConnectRequest:
    @pytest.mark.parametrize("uid", ["U012AB3CD", "UABCDEFGHI", "U1234567890"])
    def test_accepts_valid_slack_ids(self, uid):
        req = SlackConnectRequest(slack_user_id=uid)
        assert req.slack_user_id == uid

    @pytest.mark.parametrize("uid", ["u012ab3cd", "W012AB3CD", "U0123", "notanid", ""])
    def test_rejects_invalid_slack_ids(self, uid):
        with pytest.raises(ValueError):
            SlackConnectRequest(slack_user_id=uid)


# ---------------------------------------------------------------------------
# /auth/slack-connect route
# ---------------------------------------------------------------------------


class TestSlackConnectRoute:
    @pytest.fixture
    def authed_client(self):
        from base_module.jwt_utils import issue_token

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)
        client.headers = {"Authorization": f"Bearer {issue_token('u-1', 'alice')}"}
        return client

    def test_returns_204_on_success(self, authed_client):
        conn = MagicMock()
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value = cur
        with patch("base_module.users._connect", return_value=conn):
            resp = authed_client.post("/auth/slack-connect", json={"slack_user_id": "U012AB3CD"})
        assert resp.status_code == 204
        cur.execute.assert_called_once()
        conn.commit.assert_called_once()

    def test_rejects_bad_slack_id_format(self, authed_client):
        resp = authed_client.post("/auth/slack-connect", json={"slack_user_id": "notanid"})
        assert resp.status_code == 422

    def test_db_error_returns_500(self, authed_client):
        conn = MagicMock()
        conn.cursor.side_effect = psycopg2.OperationalError("connection refused")
        with patch("base_module.users._connect", return_value=conn):
            resp = authed_client.post("/auth/slack-connect", json={"slack_user_id": "U012AB3CD"})
        assert resp.status_code == 500
        assert "db error" in resp.json()["detail"]

    def test_requires_auth(self, client):
        resp = client.post("/auth/slack-connect", json={"slack_user_id": "U012AB3CD"})
        assert resp.status_code == 401
