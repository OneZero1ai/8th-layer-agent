"""Tests for the cookie-bound web-session module (FO-1c, #191).

Covers ``mint_session_cookie`` / ``read_session_from_cookie`` in
isolation and against the FastAPI app:

* round-trip: mint → read back same username
* expired cookie → None
* wrong-aud cookie (e.g. invite token) → None
* cookie attributes: HttpOnly, SameSite=Lax, Path=/, Domain derived
  from rp_id
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt as pyjwt
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.testclient import TestClient

from cq_server.web_session import (
    COOKIE_NAME,
    SESSION_AUDIENCE,
    clear_session_cookie,
    get_current_user_from_session,
    mint_session_cookie,
    read_session_from_cookie,
)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Standard env vars the helpers need."""
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "ws.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_ENV", "dev")
    # rp_id() resolves to "localhost" by default in dev.
    monkeypatch.delenv("CQ_WEBAUTHN_RP_ID", raising=False)
    yield


@pytest.fixture()
def mini_app(env: None) -> FastAPI:  # noqa: ARG001 — env fixture sets module-level state
    """A tiny FastAPI app that mints + reads cookies via the helpers."""
    app = FastAPI()

    @app.post("/_mint")
    def _mint(response: Response, username: str = "alice") -> dict[str, str]:
        token = mint_session_cookie(response, username=username)
        return {"token": token}

    @app.get("/_read")
    def _read(request: Request) -> dict[str, str | None]:
        sess = read_session_from_cookie(request)
        return {"username": sess.username if sess else None}

    @app.get("/_who", dependencies=[])
    async def _who(username: str = Depends(get_current_user_from_session)) -> dict[str, str]:  # type: ignore[name-defined]
        return {"username": username}

    @app.post("/_clear")
    def _clear(response: Response) -> dict[str, bool]:
        clear_session_cookie(response)
        return {"cleared": True}

    return app


# Late import to keep the fixture signature clean.
from fastapi import Depends  # noqa: E402


class TestMintAndRead:
    def test_round_trip(self, mini_app: FastAPI) -> None:
        with TestClient(mini_app) as client:
            mint = client.post("/_mint?username=alice")
            assert mint.status_code == 200
            assert COOKIE_NAME in mint.cookies
            # The TestClient propagates cookies on subsequent requests.
            read = client.get("/_read")
            assert read.json() == {"username": "alice"}

    def test_cookie_attributes(self, mini_app: FastAPI) -> None:
        with TestClient(mini_app) as client:
            mint = client.post("/_mint")
            set_cookie = mint.headers.get("set-cookie", "")
            assert "HttpOnly" in set_cookie
            # SameSite is normalised to title-case by Starlette.
            assert "samesite=lax" in set_cookie.lower()
            assert "path=/" in set_cookie.lower()

    def test_no_cookie_returns_none(self, mini_app: FastAPI) -> None:
        with TestClient(mini_app) as client:
            read = client.get("/_read")
            assert read.json() == {"username": None}

    def test_get_current_user_from_session_requires_cookie(self, mini_app: FastAPI) -> None:
        with TestClient(mini_app) as client:
            resp = client.get("/_who")
            assert resp.status_code == 401

    def test_get_current_user_from_session_succeeds(self, mini_app: FastAPI) -> None:
        with TestClient(mini_app) as client:
            client.post("/_mint?username=bob")
            resp = client.get("/_who")
            assert resp.status_code == 200
            assert resp.json() == {"username": "bob"}

    def test_clear_cookie_logs_out(self, mini_app: FastAPI) -> None:
        with TestClient(mini_app) as client:
            client.post("/_mint")
            client.post("/_clear")
            resp = client.get("/_who")
            assert resp.status_code == 401


class TestRejection:
    def test_expired_cookie_rejected(self, mini_app: FastAPI) -> None:
        # Mint an already-expired token directly and stuff it into the cookie jar.
        now = datetime.now(UTC)
        expired = pyjwt.encode(
            {
                "sub": "alice",
                "iss": "8th-layer.ai",
                "aud": SESSION_AUDIENCE,
                "iat": now - timedelta(hours=2),
                "exp": now - timedelta(hours=1),
            },
            "test-secret-thirty-two-chars-min!",
            algorithm="HS256",
        )
        with TestClient(mini_app) as client:
            client.cookies.set(COOKIE_NAME, expired)
            resp = client.get("/_read")
            assert resp.json() == {"username": None}

    def test_invite_aud_rejected_at_session_surface(self, mini_app: FastAPI) -> None:
        """A cookie containing an invite token must not authenticate."""
        now = datetime.now(UTC)
        invite_tok = pyjwt.encode(
            {
                "sub": "evil@example.com",
                "iss": "8th-layer.ai",
                "aud": "invite",
                "iat": now,
                "exp": now + timedelta(hours=1),
                "jti": "deadbeef",
            },
            "test-secret-thirty-two-chars-min!",
            algorithm="HS256",
        )
        with TestClient(mini_app) as client:
            client.cookies.set(COOKIE_NAME, invite_tok)
            resp = client.get("/_who")
            assert resp.status_code == 401

    def test_wrong_signature_rejected(self, mini_app: FastAPI) -> None:
        now = datetime.now(UTC)
        bogus = pyjwt.encode(
            {
                "sub": "alice",
                "iss": "8th-layer.ai",
                "aud": SESSION_AUDIENCE,
                "iat": now,
                "exp": now + timedelta(hours=1),
            },
            "different-secret-also-32-chars-!!",
            algorithm="HS256",
        )
        with TestClient(mini_app) as client:
            client.cookies.set(COOKIE_NAME, bogus)
            resp = client.get("/_read")
            assert resp.json() == {"username": None}


class TestDomainAttribute:
    def test_domain_set_when_rp_id_is_a_real_host(
        self,
        env: None,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Cookie's Domain attribute follows ``rp_id`` (FO-1a env)."""
        monkeypatch.setenv("CQ_WEBAUTHN_RP_ID", "8th-layer.ai")
        # Re-build a small app inside this test so it picks up the env.
        app = FastAPI()

        @app.post("/_mint")
        def _mint(response: Response) -> dict[str, str]:
            token = mint_session_cookie(response, username="alice")
            return {"token": token}

        with TestClient(app) as client:
            mint = client.post("/_mint")
            set_cookie = mint.headers.get("set-cookie", "")
            assert "domain=.8th-layer.ai" in set_cookie.lower()

    def test_no_domain_attribute_for_localhost(self, mini_app: FastAPI) -> None:
        """Browsers reject explicit ``Domain=localhost``; helper omits it."""
        with TestClient(mini_app) as client:
            mint = client.post("/_mint")
            set_cookie = mint.headers.get("set-cookie", "")
            assert "domain=" not in set_cookie.lower()


class TestTTLConfig:
    def test_custom_ttl_via_env(
        self,
        env: None,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CQ_SESSION_TTL_HOURS", "1")
        app = FastAPI()

        @app.post("/_mint")
        def _mint(response: Response) -> dict[str, str]:
            return {"token": mint_session_cookie(response, username="alice")}

        with TestClient(app) as client:
            mint = client.post("/_mint")
            set_cookie = mint.headers.get("set-cookie", "")
            # Max-Age = 1h * 3600 = 3600
            assert "max-age=3600" in set_cookie.lower()

    def test_invalid_ttl_falls_back_to_default(
        self,
        env: None,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("CQ_SESSION_TTL_HOURS", "not-a-number")
        app = FastAPI()

        @app.post("/_mint")
        def _mint(response: Response) -> dict[str, str]:
            return {"token": mint_session_cookie(response, username="alice")}

        with TestClient(app) as client:
            mint = client.post("/_mint")
            set_cookie = mint.headers.get("set-cookie", "").lower()
            # Default is 24h * 3600 = 86400
            assert "max-age=86400" in set_cookie


class TestExpiryClock:
    def test_just_expired_returns_none(
        self,
        env: None,  # noqa: ARG002
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A 0-hour TTL token is expired by the time we try to read it."""
        monkeypatch.setenv("CQ_SESSION_TTL_HOURS", "0")
        # The mint helper guards against ttl_hours <= 0 and falls back to default,
        # so for this test we mint a 0-hour token directly.
        now = datetime.now(UTC)
        token = pyjwt.encode(
            {
                "sub": "alice",
                "iss": "8th-layer.ai",
                "aud": SESSION_AUDIENCE,
                "iat": now,
                "exp": now,  # exp == iat → already-expired by the time verify runs
            },
            "test-secret-thirty-two-chars-min!",
            algorithm="HS256",
        )
        time.sleep(0.05)
        app = FastAPI()

        @app.get("/_read")
        def _read(request: Request) -> dict[str, str | None]:
            sess = read_session_from_cookie(request)
            return {"username": sess.username if sess else None}

        with TestClient(app) as client:
            client.cookies.set(COOKIE_NAME, token)
            resp = client.get("/_read")
            assert resp.json() == {"username": None}
