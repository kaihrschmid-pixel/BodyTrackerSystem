"""
tests/core/test_cli.py
========================
Tests for core/cli.py — argument parsing, command dispatch, OAuth helpers.
Run with: pytest tests/ -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest

from core.cli import build_parser, load_config, _save_token_file


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

class TestBuildParser:
    def setup_method(self):
        self.parser = build_parser()

    def test_measure_command(self):
        args = self.parser.parse_args(["measure"])
        assert args.command == "measure"
        assert args.notes is None

    def test_measure_with_notes(self):
        args = self.parser.parse_args(["measure", "--notes", "post-workout"])
        assert args.notes == "post-workout"

    def test_measure_with_notes_short(self):
        args = self.parser.parse_args(["measure", "-n", "fasted"])
        assert args.notes == "fasted"

    def test_daemon_command(self):
        args = self.parser.parse_args(["daemon"])
        assert args.command == "daemon"

    def test_import_command(self):
        args = self.parser.parse_args(["import", "data/history.csv"])
        assert args.command == "import"
        assert args.file == "data/history.csv"

    def test_history_command_default_limit(self):
        args = self.parser.parse_args(["history"])
        assert args.command == "history"
        assert args.limit == 20

    def test_history_command_custom_limit(self):
        args = self.parser.parse_args(["history", "--limit", "5"])
        assert args.limit == 5

    def test_history_command_limit_short(self):
        args = self.parser.parse_args(["history", "-n", "10"])
        assert args.limit == 10

    def test_adapters_command(self):
        args = self.parser.parse_args(["adapters"])
        assert args.command == "adapters"

    def test_init_command_default_output(self):
        args = self.parser.parse_args(["init"])
        assert args.command == "init"
        assert args.output == "config.yaml"

    def test_init_command_custom_output(self):
        args = self.parser.parse_args(["init", "--output", "my_config.yaml"])
        assert args.output == "my_config.yaml"

    def test_auth_withings(self):
        args = self.parser.parse_args(["auth", "withings"])
        assert args.command == "auth"
        assert args.service == "withings"

    def test_auth_fitbit(self):
        args = self.parser.parse_args(["auth", "fitbit"])
        assert args.service == "fitbit"

    def test_auth_whoop(self):
        args = self.parser.parse_args(["auth", "whoop"])
        assert args.service == "whoop"

    def test_auth_invalid_service_exits(self):
        with pytest.raises(SystemExit):
            self.parser.parse_args(["auth", "strava"])

    def test_global_verbose_flag(self):
        args = self.parser.parse_args(["--verbose", "measure"])
        assert args.verbose is True

    def test_global_config_flag(self):
        args = self.parser.parse_args(["--config", "my.yaml", "measure"])
        assert args.config == "my.yaml"

    def test_global_config_short_flag(self):
        args = self.parser.parse_args(["-c", "alt.yaml", "measure"])
        assert args.config == "alt.yaml"

    def test_no_command_returns_none(self):
        args = self.parser.parse_args([])
        assert args.command is None


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_loads_valid_yaml(self, tmp_path):
        cfg_file = tmp_path / "config.yaml"
        cfg_file.write_text("storage:\n  db_path: data/tracker.db\n")
        config = load_config(str(cfg_file))
        assert config["storage"]["db_path"] == "data/tracker.db"

    def test_exits_if_file_missing(self, tmp_path):
        with pytest.raises(SystemExit):
            load_config(str(tmp_path / "nonexistent.yaml"))


# ---------------------------------------------------------------------------
# _save_token_file
# ---------------------------------------------------------------------------

class TestSaveTokenFile:
    def test_creates_file_with_tokens(self, tmp_path):
        token_path = str(tmp_path / "tokens.json")
        _save_token_file(token_path, {
            "access_token": "aaa",
            "refresh_token": "bbb",
        })
        data = json.loads(Path(token_path).read_text())
        assert data["access_token"] == "aaa"
        assert data["refresh_token"] == "bbb"
        assert "updated_at" in data

    def test_creates_parent_directories(self, tmp_path):
        token_path = str(tmp_path / "sub" / "dir" / "tokens.json")
        _save_token_file(token_path, {"access_token": "x", "refresh_token": "y"})
        assert Path(token_path).exists()


# ---------------------------------------------------------------------------
# cmd_measure dispatch
# ---------------------------------------------------------------------------

class TestCmdMeasure:
    @pytest.mark.asyncio
    async def test_cmd_measure_calls_scheduler(self, tmp_path):
        from core.cli import cmd_measure

        config = {
            "storage": {"db_path": str(tmp_path / "db")},
            "hardware": {"scale": {"adapter": "manual"}, "camera": {"adapter": "mock"}},
            "wearables": {},
            "profile": {"name": "Test"},
            "ai": {"enabled": False},
        }

        mock_scheduler = AsyncMock()
        mock_scheduler.setup = AsyncMock()
        mock_scheduler.run_session = AsyncMock(return_value={
            "session_id": 1,
            "weight_kg": 75.0,
            "available_metrics": [],
            "photo_path": "/tmp/photo.jpg",
            "wearable_sources": [],
            "ai_summary": None,
        })

        with patch("core.cli.Scheduler", return_value=mock_scheduler):
            await cmd_measure(config, notes=None)

        mock_scheduler.setup.assert_awaited_once()
        mock_scheduler.run_session.assert_awaited_once_with(notes=None)

    @pytest.mark.asyncio
    async def test_cmd_measure_passes_notes(self, tmp_path):
        from core.cli import cmd_measure

        config = {
            "storage": {"db_path": str(tmp_path / "db")},
            "hardware": {"scale": {"adapter": "m"}, "camera": {"adapter": "m"}},
            "wearables": {},
            "profile": {},
            "ai": {},
        }
        mock_scheduler = AsyncMock()
        mock_scheduler.run_session = AsyncMock(return_value={
            "session_id": 1, "weight_kg": 80.0, "available_metrics": [],
            "photo_path": "x", "wearable_sources": [], "ai_summary": None,
        })

        with patch("core.cli.Scheduler", return_value=mock_scheduler):
            await cmd_measure(config, notes="fasted")

        mock_scheduler.run_session.assert_awaited_once_with(notes="fasted")


# ---------------------------------------------------------------------------
# cmd_history
# ---------------------------------------------------------------------------

class TestCmdHistory:
    @pytest.mark.asyncio
    async def test_cmd_history_no_sessions(self, tmp_path, capsys):
        from core.cli import cmd_history

        config = {
            "storage": {"db_path": str(tmp_path / "db.sqlite")},
            "profile": {"name": "Test"},
        }

        await cmd_history(config, limit=20)
        captured = capsys.readouterr()
        assert "No sessions recorded yet" in captured.out

    @pytest.mark.asyncio
    async def test_cmd_history_shows_sessions(self, tmp_path, capsys):
        from core.cli import cmd_history
        from core.database import Database
        from adapters.base import ScaleReading

        db_path = tmp_path / "db.sqlite"
        db = Database(db_path)
        await db.init()
        pid = await db.get_or_create_profile("Test")
        await db.save_session(pid, ScaleReading(
            recorded_at=__import__("adapters.base", fromlist=["utcnow"]).utcnow(),
            weight_kg=78.5, adapter_name="manual"
        ))
        await db.close()

        config = {
            "storage": {"db_path": str(db_path)},
            "profile": {"name": "Test"},
        }
        await cmd_history(config, limit=20)
        captured = capsys.readouterr()
        assert "78.5" in captured.out


# ---------------------------------------------------------------------------
# cmd_adapters
# ---------------------------------------------------------------------------

class TestCmdAdapters:
    def test_cmd_adapters_prints_list(self, capsys):
        from core.cli import cmd_adapters

        mock_adapters = {
            "scales": ["manual", "withings"],
            "cameras": ["mock", "v4l2"],
            "wearables": ["oura", "garmin"],
        }

        with patch("core.cli.list_available_adapters", return_value=mock_adapters):
            cmd_adapters()

        captured = capsys.readouterr()
        assert "manual" in captured.out
        assert "oura" in captured.out


# ---------------------------------------------------------------------------
# OAuth helpers — _run_local_oauth_server
# ---------------------------------------------------------------------------

class TestRunLocalOAuthServer:
    @pytest.mark.asyncio
    async def test_returns_parsed_params(self, tmp_path):
        """
        Mock HTTPServer.handle_request to immediately inject a callback
        with code=abc123 into the result dict.
        """
        from core.cli import _run_local_oauth_server

        def fake_handle_request(server_instance):
            # Directly populate the result dict by simulating a GET /callback?code=abc123
            import urllib.parse
            from http.server import BaseHTTPRequestHandler
            # Inject the result by calling do_GET on a mock request
            params = {"code": "abc123", "state": "body-tracker"}
            server_instance.__dict__.get("_test_inject", {}).update(params)

        class MockServer:
            def __init__(self, addr, handler_class):
                self.addr = addr
                self._handler = handler_class
                self.timeout = 120

            def handle_request(self):
                # Simulate the handler receiving a redirect
                import urllib.parse
                handler = object.__new__(self._handler)
                handler.path = "/callback?code=abc123&state=body-tracker"
                parsed = urllib.parse.urlparse(handler.path)
                params = urllib.parse.parse_qs(parsed.query)
                # result is from closure in the tested function — we can't inject directly
                # Instead, simulate by calling do_GET with a fake wfile
                class FakeWfile:
                    def write(self, data): pass
                handler.wfile = FakeWfile()
                handler.send_response = lambda code: None
                handler.send_header = lambda k, v: None
                handler.end_headers = lambda: None
                handler.do_GET()

            def server_close(self):
                pass

        # Since the closure `result` in _run_local_oauth_server is local, we test
        # the function end-to-end with a patch on HTTPServer + webbrowser
        import io

        captured_result = {}

        original_func = _run_local_oauth_server

        # Patch just the HTTPServer and webbrowser — the handler class handles params internally
        with (
            patch("core.cli.webbrowser") as mock_wb,
            patch("core.cli.asyncio.get_event_loop") as mock_loop,
        ):
            mock_executor = AsyncMock()

            async def fake_run_in_executor(executor, fn):
                fn()

            mock_event_loop = MagicMock()
            mock_event_loop.run_in_executor = fake_run_in_executor
            mock_loop.return_value = mock_event_loop

            class FakeHTTPServer:
                def __init__(self, addr, handler_class):
                    self._handler = handler_class
                    self.timeout = 120
                    # Immediately simulate a valid callback
                    captured_result["_handler"] = handler_class
                    captured_result["_addr"] = addr

                def handle_request(self):
                    import urllib.parse
                    h = object.__new__(self._handler)
                    h.path = "/callback?code=TOKEN_X&state=ok"

                    class FakeWfile:
                        def write(self, d): pass
                    h.wfile = FakeWfile()
                    h.send_response = lambda c: None
                    h.send_header = lambda k, v: None
                    h.end_headers = lambda: None
                    h.do_GET()

                def server_close(self):
                    pass

            with patch("core.cli.HTTPServer", FakeHTTPServer):
                result = await _run_local_oauth_server("http://example.com/auth", port=9753)

        assert result.get("code") == "TOKEN_X"

    @pytest.mark.asyncio
    async def test_raises_if_no_callback(self):
        from core.cli import _run_local_oauth_server

        class EmptyHTTPServer:
            def __init__(self, addr, handler_class):
                self._handler = handler_class
                self.timeout = 120

            def handle_request(self):
                pass  # Does nothing — simulates timeout

            def server_close(self):
                pass

        with (
            patch("core.cli.webbrowser"),
            patch("core.cli.asyncio.get_event_loop") as mock_loop,
            patch("core.cli.HTTPServer", EmptyHTTPServer),
        ):
            async def fake_run_in_executor(executor, fn):
                fn()

            mock_event_loop = MagicMock()
            mock_event_loop.run_in_executor = fake_run_in_executor
            mock_loop.return_value = mock_event_loop

            with pytest.raises(RuntimeError, match="No callback received"):
                await _run_local_oauth_server("http://example.com/auth")
