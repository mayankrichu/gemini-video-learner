from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_extension_manifest_contains_required_permissions() -> None:
    manifest = json.loads((ROOT / "extension" / "manifest.json").read_text())
    assert manifest["manifest_version"] == 3
    assert "storage" in manifest["permissions"]
    assert manifest["background"]["service_worker"] == "background.js"
    assert manifest["action"]["default_popup"] == "popup.html"


def test_extension_contains_no_server_secret_placeholders() -> None:
    extension_text = "\n".join(
        path.read_text(errors="ignore") for path in (ROOT / "extension").glob("*") if path.is_file()
    )
    assert "OPENAI_API_KEY" not in extension_text
    assert "STRIPE_SECRET_KEY" not in extension_text
    assert "SUPABASE_SECRET_KEY" not in extension_text


def test_auth_storage_is_not_exposed_to_youtube_content_script() -> None:
    background = (ROOT / "extension" / "background.js").read_text()
    content = (ROOT / "extension" / "content.js").read_text()
    assert 'accessLevel: "TRUSTED_CONTEXTS"' in background
    assert "chrome.storage.local" not in content


def test_sql_contains_idempotency_and_owner_modes() -> None:
    sql = (ROOT / "backend" / "sql" / "001_initial.sql").read_text()
    assert "unique (user_id, video_id, transcript_hash, settings_hash)" in sql
    assert "stripe_event_id text primary key" in sql
    assert "process_stripe_credit_reversal" in sql
    assert "at_cost" in sql


def test_backend_exposes_expected_routes() -> None:
    from app.main import app

    paths = set(app.openapi()["paths"])
    assert "/v1/analyze/free" in paths
    assert "/v1/analyze/ai" in paths
    assert "/v1/account/summary" in paths
    assert "/v1/billing/checkout-session" in paths
    assert "/v1/billing/stripe-webhook" in paths
