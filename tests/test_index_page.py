"""The demo playground page is served at GET /."""

from __future__ import annotations


def test_index_served(client) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    assert "GridWise" in resp.text


def test_index_not_in_openapi(client) -> None:
    """The page route is hidden from the schema so /docs stays API-only."""
    schema = client.get("/openapi.json").json()
    assert "/" not in schema["paths"]
    assert "/health" in schema["paths"]
    assert "/optimize-energy" in schema["paths"]


def test_index_does_not_shadow_api(client) -> None:
    assert client.get("/health").json() == {"status": "ok"}
