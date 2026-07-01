"""Tests for mobile menu navigation rendering."""

from fastapi.testclient import TestClient

from src.web import app as web_app
from src.web.app import DashboardState


def test_menu_page_lists_navigation_items_without_hamburger():
    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state = DashboardState()

    try:
        response = TestClient(web_app.create_app()).get("/menu")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert response.status_code == 200
    for href in (
        "/",
        "/portfolio",
        "/symbols",
        "/trades",
        "/performance",
        "/analytics",
        "/backtest",
        "/settings",
        "/logout",
    ):
        assert f'href="{href}"' in response.text

    assert 'id="menu-toggle"' not in response.text
    assert 'id="bottom-menu-toggle"' not in response.text
    assert 'id="menu-backdrop"' not in response.text
    assert 'href="/menu" class="bottom-nav-item bottom-nav-menu active"' in response.text


def test_secondary_pages_keep_bottom_menu_active():
    original_auth_password = web_app.AUTH_PASSWORD
    original_mock_mode = web_app.MOCK_MODE
    original_dashboard_state = web_app.dashboard_state

    web_app.AUTH_PASSWORD = "test"
    web_app.MOCK_MODE = True
    web_app.dashboard_state = DashboardState()

    try:
        response = TestClient(web_app.create_app()).get("/performance")
    finally:
        web_app.AUTH_PASSWORD = original_auth_password
        web_app.MOCK_MODE = original_mock_mode
        web_app.dashboard_state = original_dashboard_state

    assert response.status_code == 200
    assert 'href="/menu" class="bottom-nav-item bottom-nav-menu active"' in response.text
