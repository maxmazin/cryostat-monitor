"""Shared fixtures for ingest tests.

Unit tests run the FastAPI app through TestClient with the DB layer faked, so
they exercise auth, validation, timezone handling, and non-finite filtering
without a live PostgreSQL.
"""
from __future__ import annotations

import contextlib

import pytest
from fastapi.testclient import TestClient

from ingest import app as app_module
from ingest import db

DEFAULT_TOKENS = '{"host-token": "bluefors_1"}'
DEFAULT_MAINTENANCE_TOKENS = '["maint-token"]'


class FakeDB:
    """Records calls the endpoints make into the DB layer."""

    def __init__(self) -> None:
        self.readings_calls: list[tuple[str, list]] = []
        self.maintenance_calls: list[tuple[str, int, str | None, str | None]] = []

    def insert_readings(self, fridge: str, rows: list) -> int:
        self.readings_calls.append((fridge, rows))
        return len(rows)  # pretend every passed row was newly inserted

    def insert_maintenance(self, fridge, minutes, reason, set_by) -> None:
        self.maintenance_calls.append((fridge, minutes, reason, set_by))


@pytest.fixture
def fake_db(monkeypatch):
    fake = FakeDB()
    monkeypatch.setattr(db, "init_pool", lambda *a, **k: None)
    monkeypatch.setattr(db, "close_pool", lambda *a, **k: None)
    monkeypatch.setattr(db, "insert_readings", fake.insert_readings)
    monkeypatch.setattr(db, "insert_maintenance", fake.insert_maintenance)
    return fake


def _set_or_del(monkeypatch, var: str, val: str | None) -> None:
    if val is None:
        monkeypatch.delenv(var, raising=False)
    else:
        monkeypatch.setenv(var, val)


@pytest.fixture
def make_client(monkeypatch, fake_db):
    """Factory that builds a TestClient with controllable config.

    Config is read in the app's lifespan, which fires when the TestClient
    context is entered — so env set here takes effect per client. Clients are
    exited automatically at the end of the test.
    """
    with contextlib.ExitStack() as stack:
        def _make(*, tokens: str | None = DEFAULT_TOKENS,
                  maint_tokens: str | None = DEFAULT_MAINTENANCE_TOKENS,
                  max_minutes: str | None = None) -> TestClient:
            monkeypatch.setenv("CRYO_DB_DSN", "postgresql://unused-in-unit-tests")
            _set_or_del(monkeypatch, "CRYO_TOKENS", tokens)
            _set_or_del(monkeypatch, "CRYO_MAINTENANCE_TOKENS", maint_tokens)
            _set_or_del(monkeypatch, "CRYO_MAX_MAINTENANCE_MINUTES", max_minutes)
            monkeypatch.delenv("CRYO_TOKENS_FILE", raising=False)
            monkeypatch.delenv("CRYO_MAINTENANCE_TOKENS_FILE", raising=False)
            return stack.enter_context(TestClient(app_module.app))

        yield _make


@pytest.fixture
def client(make_client):
    """One host token (-> bluefors_1) and one maintenance token configured."""
    return make_client()


@pytest.fixture
def client_no_maint(make_client):
    """Host token configured but NO maintenance tokens (fail-closed case)."""
    return make_client(maint_tokens=None)
