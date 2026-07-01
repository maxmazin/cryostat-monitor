"""Guards on the Grafana provisioning artifacts (server/grafana/).

These are deployed as-is to labmanager, so a malformed dashboard JSON or a
datasource-uid mismatch would only surface as a broken dashboard after deploy.
These cheap checks catch that in CI. No DB or Grafana needed.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

GRAFANA = Path(__file__).resolve().parents[1] / "grafana"
DASHBOARD = GRAFANA / "dashboards" / "cryostat-overview.json"
DATASOURCE = GRAFANA / "provisioning" / "datasources" / "cryo-postgres.yaml"


def test_dashboard_is_valid_json():
    json.loads(DASHBOARD.read_text())


def test_provisioning_is_valid_yaml():
    for path in DATASOURCE.parent.parent.rglob("*.yaml"):
        yaml.safe_load(path.read_text())


def test_dashboard_datasource_uid_matches_provisioning():
    # Every panel/target references the datasource by uid; if it doesn't match the
    # provisioned datasource's uid, the panels render "datasource not found".
    ds_uid = yaml.safe_load(DATASOURCE.read_text())["datasources"][0]["uid"]
    dash = json.loads(DASHBOARD.read_text())

    referenced = set()

    def _collect(node):
        if isinstance(node, dict):
            if node.get("type") == "postgres" and "uid" in node:
                referenced.add(node["uid"])
            for v in node.values():
                _collect(v)
        elif isinstance(node, list):
            for v in node:
                _collect(v)

    _collect(dash)
    assert referenced, "dashboard references no postgres datasource"
    assert referenced == {ds_uid}, f"dashboard uses {referenced}, provisioning defines {ds_uid!r}"
