"""Tests for the mtime-derived static asset cache-buster."""

import os
import re

from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web.app import BASE_DIR, DashboardState


def _render_menu() -> str:
    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state = DashboardState()

    try:
        return TestClient(web_app.create_app()).get("/menu").text
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state


def test_asset_version_tracks_style_mtime():
    css = BASE_DIR / "static" / "style.css"
    original_mtime = css.stat().st_mtime

    before = re.search(r"style\.css\?v=(\d+)", _render_menu())
    assert before and before.group(1) != "0"

    try:
        os.utime(css, (original_mtime + 60, original_mtime + 60))
        after = re.search(r"style\.css\?v=(\d+)", _render_menu())
    finally:
        os.utime(css, (original_mtime, original_mtime))

    assert after
    assert after.group(1) != before.group(1)
