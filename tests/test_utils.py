"""Tests for src.bot._utils helpers."""

from src.bot._utils import extract_symbols


class TestExtractSymbols:
    """extract_symbols must honor the dashboard's per-symbol enabled flag."""

    def test_plain_strings(self):
        assert extract_symbols(["AAPL", "GOOG"]) == ["AAPL", "GOOG"]

    def test_dicts_without_flag_are_enabled(self):
        symbols = [{"symbol": "AAPL", "max_weight": 15}, {"symbol": "GOOG"}]
        assert extract_symbols(symbols) == ["AAPL", "GOOG"]

    def test_disabled_symbol_excluded(self):
        symbols = [
            {"symbol": "XLE", "max_weight": 10},
            {"symbol": "KORU", "max_weight": 7, "enabled": False},
        ]
        assert extract_symbols(symbols) == ["XLE"]

    def test_enabled_true_included(self):
        symbols = [{"symbol": "XLE", "enabled": True}]
        assert extract_symbols(symbols) == ["XLE"]

    def test_all_disabled_returns_empty(self):
        symbols = [{"symbol": "KORU", "enabled": False}]
        assert extract_symbols(symbols) == []
