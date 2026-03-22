"""
Integration tests for the FastAPI backend.

These tests require the full Docker stack to be running:
  docker compose up -d

Run from inside the container:
  docker compose exec backend pytest -m integration -v

Or use the helper script (from the host):
  .\\run_tests.ps1 -m integration
"""

import pytest
import httpx


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def base_url() -> str:
    """Base URL for the running backend API."""
    # Inside Docker the backend is reachable on localhost:8086.
    return "http://localhost:8086"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_health_endpoint(base_url: str):
    """GET /health must return 200 — confirms the API is up and reachable."""
    response = httpx.get(f"{base_url}/health", timeout=10)
    assert response.status_code == 200


@pytest.mark.integration
def test_query_endpoint_rejects_empty_question(base_url: str):
    """POST /api/query with an empty question must return 422 (validation error)."""
    response = httpx.post(
        f"{base_url}/api/query",
        json={"question": "", "thread_id": "test-integration"},
        timeout=30,
    )
    assert response.status_code in (400, 422)


@pytest.mark.integration
def test_query_endpoint_rejects_sql_injection(base_url: str):
    """POST /api/query with a DROP TABLE attempt must return 400 (input validation)."""
    response = httpx.post(
        f"{base_url}/api/query",
        json={"question": "DROP TABLE matches;", "thread_id": "test-integration"},
        timeout=30,
    )
    assert response.status_code == 400


@pytest.mark.integration
def test_query_endpoint_returns_answer(base_url: str):
    """POST /api/query with a valid cricket question must return a structured answer."""
    response = httpx.post(
        f"{base_url}/api/query",
        json={"question": "How many matches were played in 2019?", "thread_id": "test-integration"},
        timeout=60,
    )
    assert response.status_code == 200
    body = response.json()
    assert "answer" in body
    assert "sql" in body
    assert isinstance(body["answer"], str)
    assert len(body["answer"]) > 0
