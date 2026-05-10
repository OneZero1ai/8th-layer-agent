"""Tests for FO-1d ``GET /api/v1/theme`` (Decision 30).

Covers the resolver's three return shapes:

* No L2 overrides — platform defaults + Enterprise stub + L2 derived
  from the env-pinned ``CQ_GROUP``.
* L2 row present — overrides surface in the response.
* Cache-Control header is set to ``public, max-age=300``.

The endpoint is anonymous; tests do NOT attach an Authorization header.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from cq_server.app import _get_store, app


@pytest.fixture
def client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[TestClient]:
    monkeypatch.setenv("CQ_DB_PATH", str(tmp_path / "theme.db"))
    monkeypatch.setenv("CQ_JWT_SECRET", "test-secret-thirty-two-chars-min!")
    monkeypatch.setenv("CQ_API_KEY_PEPPER", "test-pepper")
    monkeypatch.setenv("CQ_ENTERPRISE", "8th-layer-corp")
    monkeypatch.setenv("CQ_GROUP", "engineering")
    with TestClient(app) as c:
        yield c


def test_theme_returns_platform_defaults_when_no_l2_overrides(
    client: TestClient,
) -> None:
    """A fresh DB has no ``l2_brand`` row; resolver falls back to env defaults."""
    resp = client.get("/api/v1/theme")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    # Platform tier — always platform-fixed, never overridable.
    assert body["platform"]["name"] == "8th-Layer.ai"
    assert "tokens" in body["platform"]
    assert body["platform"]["tokens"]["cyan"] == "#5bd0ff"

    # Enterprise tier — V1 stub from CQ_ENTERPRISE env.
    assert body["enterprise"]["id"] == "8th-layer-corp"
    assert body["enterprise"]["display_name"] == "8th-layer-corp"
    assert body["enterprise"]["logo_url"] is None
    assert body["enterprise"]["accent_hex"] is None

    # L2 tier — fallback to env-pinned CQ_GROUP, no overrides.
    assert body["l2"]["id"] == "8th-layer-corp/engineering"
    assert body["l2"]["label"] == "engineering"
    assert body["l2"]["subaccent_hex"] is None
    assert body["l2"]["hero_motif"] is None


def test_theme_returns_l2_overrides_when_brand_row_present(
    client: TestClient,
) -> None:
    """Inserting a row into ``l2_brand`` surfaces in the response."""
    store = _get_store()
    with store._engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO l2_brand (id, l2_label, subaccent_hex, hero_motif, "
                "updated_at, updated_by) VALUES "
                "(1, :label, :accent, :motif, :ts, NULL)"
            ),
            {
                "label": "Engineering Org",
                "accent": "#a685ff",
                "motif": "gradient.cyan-violet",
                "ts": "2026-05-10T00:00:00+00:00",
            },
        )

    resp = client.get("/api/v1/theme")
    assert resp.status_code == 200
    body = resp.json()

    assert body["l2"]["label"] == "Engineering Org"
    assert body["l2"]["subaccent_hex"] == "#a685ff"
    assert body["l2"]["hero_motif"] == "gradient.cyan-violet"


def test_theme_emits_cache_control_header(client: TestClient) -> None:
    """``Cache-Control: public, max-age=300`` lets browsers revalidate every 5 minutes."""
    resp = client.get("/api/v1/theme")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=300"


def test_theme_endpoint_is_anonymous(client: TestClient) -> None:
    """No auth header required — login screen calls this BEFORE auth."""
    # Explicitly do not attach any Authorization header.
    resp = client.get("/api/v1/theme")
    assert resp.status_code == 200


def test_theme_visible_at_root_prefix_too(client: TestClient) -> None:
    """app mounts ``api_router`` at both / and /api/v1; theme reachable at both."""
    resp_root = client.get("/theme")
    resp_api = client.get("/api/v1/theme")
    assert resp_root.status_code == 200
    assert resp_api.status_code == 200
    assert resp_root.json() == resp_api.json()
