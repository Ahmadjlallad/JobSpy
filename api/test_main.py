"""Tests for the JobSpy FastAPI wrapper.

Runs under pytest (`poetry run pytest api/test_main.py`) or standalone without
pytest installed (`poetry run python api/test_main.py`). Only stdlib + already
installed deps (fastapi, httpx via TestClient) are used; the underlying
`scrape_jobs` call is always mocked so no real network scraping happens.
"""

from __future__ import annotations

import threading
import time
from unittest import mock

import pandas as pd
from fastapi.testclient import TestClient

from api import main


def _sample_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "id": "li-1",
                "site": "linkedin",
                "job_url": "https://linkedin.com/jobs/1",
                "title": "Python Developer",
                "company": "Acme",
                "description": "Django role",
                "date_posted": pd.Timestamp("2026-06-30"),
                "min_amount": float("nan"),  # exercises NaN normalization
            }
        ]
    )


def test_health_ok() -> None:
    client = TestClient(main.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_scrape_forwards_all_params() -> None:
    captured: dict = {}

    def fake_scrape(**kwargs):
        captured.update(kwargs)
        return _sample_df()

    payload = {
        "site_names": ["linkedin"],
        "search_term": "python",
        "location": "Amman",
        "country": "Jordan",
        "results_wanted": 5,
        "linkedin_fetch_description": True,
        "easy_apply": True,
        "enforce_annual_salary": True,
        "proxies": ["user:pass@host:8000"],
        "offset": 10,
    }

    with mock.patch.object(main, "API_TOKEN", ""), mock.patch.object(
        main, "scrape_jobs", side_effect=fake_scrape
    ):
        client = TestClient(main.app)
        resp = client.post("/scrape", json=payload)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["count"] == 1
    assert body["jobs"][0]["title"] == "Python Developer"
    assert body["jobs"][0]["min_amount"] is None  # NaN -> None

    # Every param made it through to the underlying engine.
    assert captured["site_name"] == ["linkedin"]
    assert captured["search_term"] == "python"
    assert captured["linkedin_fetch_description"] is True
    assert captured["easy_apply"] is True
    assert captured["enforce_annual_salary"] is True
    assert captured["proxies"] == ["user:pass@host:8000"]
    assert captured["offset"] == 10


def test_scrape_requires_token_when_configured() -> None:
    with mock.patch.object(main, "API_TOKEN", "secret"), mock.patch.object(
        main, "scrape_jobs", return_value=_sample_df()
    ):
        client = TestClient(main.app)

        # Missing token -> 401
        assert client.post("/scrape", json={}).status_code == 401
        # Wrong token -> 401
        assert (
            client.post(
                "/scrape", json={}, headers={"Authorization": "Bearer nope"}
            ).status_code
            == 401
        )
        # Correct token -> 200
        assert (
            client.post(
                "/scrape", json={}, headers={"Authorization": "Bearer secret"}
            ).status_code
            == 200
        )


def test_scrape_open_when_token_empty() -> None:
    with mock.patch.object(main, "API_TOKEN", ""), mock.patch.object(
        main, "scrape_jobs", return_value=_sample_df()
    ):
        client = TestClient(main.app)
        assert client.post("/scrape", json={}).status_code == 200


def test_scrape_maps_value_error_to_400() -> None:
    with mock.patch.object(main, "API_TOKEN", ""), mock.patch.object(
        main, "scrape_jobs", side_effect=ValueError("bad country")
    ):
        client = TestClient(main.app)
        resp = client.post("/scrape", json={})
        assert resp.status_code == 400
        assert "bad country" in resp.json()["detail"]


def test_scrape_maps_unexpected_error_to_502() -> None:
    with mock.patch.object(main, "API_TOKEN", ""), mock.patch.object(
        main, "scrape_jobs", side_effect=RuntimeError("upstream down")
    ):
        client = TestClient(main.app)
        resp = client.post("/scrape", json={})
        assert resp.status_code == 502


def test_scrape_returns_503_when_at_capacity() -> None:
    # Drain the admission semaphore so no slot is available.
    drained = threading.BoundedSemaphore(1)
    drained.acquire()
    with mock.patch.object(main, "API_TOKEN", ""), mock.patch.object(
        main, "_semaphore", drained
    ), mock.patch.object(main, "scrape_jobs", return_value=_sample_df()):
        client = TestClient(main.app)
        resp = client.post("/scrape", json={})
        assert resp.status_code == 503


def test_scrape_returns_504_on_timeout() -> None:
    sem = threading.BoundedSemaphore(1)

    def slow_scrape(**_kwargs):
        time.sleep(0.4)
        return _sample_df()

    with mock.patch.object(main, "API_TOKEN", ""), mock.patch.object(
        main, "_semaphore", sem
    ), mock.patch.object(main, "SCRAPE_TIMEOUT", 0.05), mock.patch.object(
        main, "scrape_jobs", side_effect=slow_scrape
    ):
        client = TestClient(main.app)
        resp = client.post("/scrape", json={})
        assert resp.status_code == 504, resp.status_code
        # The slot is deliberately NOT released synchronously on timeout: the
        # shielded scrape thread is still running, so the slot stays held until
        # it finishes (released via a loop callback that runs continuously under
        # uvicorn). Right after the response the slot must therefore be taken.
        assert sem.acquire(blocking=False) is False
        # Let the shielded thread finish so the executor is idle for teardown.
        time.sleep(0.5)


if __name__ == "__main__":
    # Standalone runner so the suite works without pytest installed.
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {exc!r}")
    raise SystemExit(1 if failures else 0)
