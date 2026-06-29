"""Shared fixtures for ingest tests.

Unit tests run the FastAPI app through TestClient with the DB layer faked, so
they exercise auth, validation, timezone handling, and non-finite filtering
without a live PostgreSQL.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ingest import app as app_module
from ingest import db


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


def _client(monkeypatch, *, tokens: str | None, maint_tokens: str | None) -> TestClient:
    monkeypatch.setenv("CRYO_DB_DSN", "postgresql://unused-in-unit-tests")
    for var, val in (("CRYO_TOKENS", tokens), ("CRYO_MAINTENANCE_TOKENS", maint_tokens)):
        if val is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, val)
    monkeypatch.delenv("CRYO_TOKENS_FILE", raising=False)
    monkeypatch.delenv("CRYO_MAINTENANCE_TOKENS_FILE", raising=False)
    return TestClient(app_module.app)


@pytest.fixture
def client(monkeypatch, fake_db):
    """One host token (-> bluefors_1) and one maintenance token configured."""
    with _client(monkeypatch, tokens='{"host-token": "bluefors_1"}',
                 maint_tokens='["maint-token"]') as c:
        yield c


@pytest.fixture
def client_no_maint(monkeypatch, fake_db):
    """Host token configured but NO maintenance tokens (fail-closed case)."""
    with _client(monkeypatch, tokens='{"host-token": "bluefors_1"}',
                 maint_tokens=None) as c:
        yield c
