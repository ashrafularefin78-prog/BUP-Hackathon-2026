"""Shared fixtures: public sample pack loader and API test client."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_PACK = REPO_ROOT / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


@pytest.fixture(scope="session")
def sample_pack() -> dict:
    with open(SAMPLE_PACK, "r", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def sample_cases(sample_pack: dict) -> list[dict]:
    return sample_pack["cases"]


@pytest.fixture(scope="session")
def client() -> TestClient:
    """API client pinned to the deterministic mock interpreter for the session."""
    previous = os.environ.get("LLM_PROVIDER")
    os.environ["LLM_PROVIDER"] = "mock"
    try:
        from app.main import app

        with TestClient(app) as test_client:
            yield test_client
    finally:
        if previous is None:
            os.environ.pop("LLM_PROVIDER", None)
        else:
            os.environ["LLM_PROVIDER"] = previous
