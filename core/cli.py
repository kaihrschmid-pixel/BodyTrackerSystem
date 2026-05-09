"""
core/cli.py
===========
Command-line interface for body-tracker.

Commands:
    body-tracker measure          Run a single measurement session
    body-tracker daemon           Start persistent daemon (waits for scale)
    body-tracker import <file>    Import historical data from CSV
    body-tracker history          Print recent sessions
    body-tracker adapters         List available adapters
    body-tracker init             Create default config.yaml

Usage:
    body-tracker --config path/to/config.yaml measure
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path


def setup_logging(verbose: bool = False) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
        datefmt="%H:%M:%S",
    )
    # Silence noisy third-party loggers
    for name in ("bleak", "httpx", "httpcore", "asyncio"):
        logging.getLogger(name).setLevel(logging.WARNING)


def load_config(path: str) -> dict:
    import yaml
    config_path = Path(path)
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        print("Run 'body-tracker init' to create one.")
        sys.exit(1)
    with config_path.open() as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

async def cmd_measure(config: dict, notes: str | None) -> None:
    from core.scheduler import Scheduler
    scheduler = Scheduler(config)
    await scheduler.setup()
    summary = await scheduler.run_session(notes=notes)
    print("\n── Measurement complete ──────────────────")
    print(f"  Session:  #{summary['session_id']}")
    print(f"  Weight:   {summary['weight_kg']} kg")
    print(f"  Metrics:  {', '.join(summary['available_metrics']) or '(weight only)'}")
    print(f"  Photo:    {summary['photo_path']}")
    if summary["wearable_sources"]:
        print(f"  Context:  {', '.join(summary['wearable_sources'])}")
    if summary["ai_summary"]:
        print(f"\n  AI:  {summary['ai_summary']}")
    print()


async def cmd_daemon(config: dict) -> None:
    from core.scheduler import Scheduler
    scheduler = Scheduler(config)
    print("Starting body-tracker daemon. Press Ctrl+C to stop.")
    await scheduler.start_daemon()


async def cmd_import(config: dict, csv_path: str) -> None:
    """Bulk-import historical measurements from CSV."""
    from core.database import Database
    from adapters.scales.manual import ManualAdapter

    db = Database(config["storage"]["db_path"])
    await db.init()
    profile_id = await db.get_or_create_profile(
        config.get("profile", {}).get("name", "Default")
    )

    adapter = ManualAdapter({"mode": "csv", "csv_path": csv_path})
    await adapter.connect()

    count = 0
    while True:
        try:
            reading = await adapter.read()
            await db.save_session(profile_id, reading, trigger_source="import")
            count += 1
            if count % 10 == 0:
                print(f"  Imported {count} rows…")
        except RuntimeError:
            break  # CSV exhausted

    await db.close()
    print(f"Import complete: {count} measurements added.")


async def cmd_history(config: dict, limit: int) -> None:
    from core.database import Database
    db = Database(config["storage"]["db_path"])
    await db.init()
    profile_id = await db.get_or_create_profile(
        config.get("profile", {}).get("name", "Default")
    )
    sessions = await db.get_sessions(profile_id, limit=limit)
    await db.close()

    if not sessions:
        print("No sessions recorded yet.")
        return

    print(f"\n{'Date':<22} {'Weight':>8} {'Fat%':>6} {'Muscle':>8} {'Phase°':>7}")
    print("─" * 56)
    for s in sessions:
        date = s["recorded_at"][:16].replace("T", " ")
        weight = f"{s['weight_kg']:.1f} kg"
        fat = f"{s['body_fat_pct']:.1f}%" if s.get("body_fat_pct") else "  —"
        muscle = f"{s['muscle_mass_kg']:.1f} kg" if s.get("muscle_mass_kg") else "    —"
        phase = f"{s['phase_angle']:.1f}°" if s.get("phase_angle") else "   —"
        print(f"{date:<22} {weight:>8} {fat:>6} {muscle:>8} {phase:>7}")
    print()


def cmd_adapters() -> None:
    from adapters.registry import list_available_adapters
    adapters = list_available_adapters()
    print("\nAvailable adapters:")
    for category, names in adapters.items():
        print(f"  {category:<12} {', '.join(names)}")
    print()


async def cmd_auth(service: str, config: dict) -> None:
    """
    Run the OAuth 2.0 authorisation flow for a cloud service.

    Starts a temporary local HTTP server, opens the browser at the provider's
    auth URL, waits for the redirect, exchanges the code for tokens and saves
    them to the path configured in config.yaml.

    Supported services: withings, fitbit, whoop
    """
    service = service.lower()
    if service == "withings":
        await _auth_withings(config)
    elif service == "fitbit":
        await _auth_fitbit(config)
    elif service == "whoop":
        await _auth_whoop(config)
    else:
        print(f"Unknown service '{service}'. Supported: withings, fitbit, whoop")
        sys.exit(1)


# ---------------------------------------------------------------------------
# OAuth helpers — shared infrastructure
# ---------------------------------------------------------------------------

async def _run_local_oauth_server(
    auth_url: str,
    port: int = 9753,
) -> dict:
    """
    Open `auth_url` in the browser, start a one-shot HTTP server on localhost:{port},
    wait for the OAuth redirect, and return the parsed query parameters.

    The provider must be configured with redirect_uri = http://localhost:{port}/callback
    """
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, HTTPServer

    result: dict = {}

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            params = urllib.parse.parse_qs(parsed.query)
            result.update({k: v[0] for k, v in params.items()})

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                "<html><body style='font-family:sans-serif;padding:2em'>"
                "<h2>body-tracker: authorisation complete!</h2>"
                "<p>You can close this tab and return to the terminal.</p>"
                "</body></html>".encode("utf-8")
            )

        def log_message(self, *_):
            pass   # suppress default access log

    server = HTTPServer(("127.0.0.1", port), _Handler)
    server.timeout = 120   # 2-minute window to complete the browser flow

    import webbrowser
    print(f"\nOpening browser for authorisation…")
    print(f"If it doesn't open automatically, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    # Poll in a thread so we don't block the event loop
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, server.handle_request)
    server.server_close()

    if not result:
        raise RuntimeError(
            "No callback received within 2 minutes. "
            "Make sure you completed the authorisation in the browser."
        )
    if "error" in result:
        raise RuntimeError(f"Provider returned error: {result['error']}")
    return result


def _save_token_file(token_path: str, tokens: dict) -> None:
    """Write tokens dict as JSON to token_path, creating parent dirs."""
    from adapters.base import utcnow
    path = Path(token_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tokens["updated_at"] = utcnow().isoformat()
    path.write_text(__import__("json").dumps(tokens, indent=2))
    print(f"  Tokens saved → {path}")


# ---------------------------------------------------------------------------
# Withings OAuth 2.0
# ---------------------------------------------------------------------------

async def _auth_withings(config: dict) -> None:
    """
    Withings OAuth 2.0 flow.
    redirect_uri must be registered at developer.withings.com as:
        http://localhost:9753/callback
    """
    import urllib.parse
    import json

    scale_cfg = config.get("hardware", {}).get("scale", {})
    client_id = scale_cfg.get("client_id", "")
    client_secret = scale_cfg.get("client_secret", "")
    token_path = scale_cfg.get("token_path", "data/withings_token.json")

    if not client_id or not client_secret:
        print("Error: withings client_id / client_secret missing in config.yaml → hardware.scale")
        sys.exit(1)

    redirect_uri = "http://localhost:9753/callback"
    auth_url = (
        "https://account.withings.com/oauth2_user/authorize2?"
        + urllib.parse.urlencode({
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "scope": "user.metrics",
            "state": "body-tracker",
        })
    )

    params = await _run_local_oauth_server(auth_url, port=9753)
    code = params.get("code")
    if not code:
        raise RuntimeError(f"No code in callback params: {params}")

    print("  Exchanging code for tokens…")
    import httpx
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://wbsapi.withings.net/v2/oauth2",
            data={
                "action": "requesttoken",
                "grant_type": "authorization_code",
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        resp.raise_for_status()
        body = resp.json()

    if body.get("status") != 0:
        raise RuntimeError(f"Withings token error: {body}")

    token_body = body["body"]
    _save_token_file(token_path, {
        "access_token": token_body["access_token"],
        "refresh_token": token_body["refresh_token"],
    })
    print("\n✅  Withings authorisation complete.")
    print(f"    Update your config.yaml → hardware.scale.token_path: {token_path}")


# ---------------------------------------------------------------------------
# Fitbit OAuth 2.0
# ---------------------------------------------------------------------------

async def _auth_fitbit(config: dict) -> None:
    """
    Fitbit OAuth 2.0 PKCE flow.
    redirect_uri must be registered at dev.fitbit.com as:
        http://localhost:9753/callback
    """
    import base64
    import hashlib
    import os
    import urllib.parse
    import json

    wearable_cfg = config.get("wearables", {}).get("fitbit", {})
    client_id = wearable_cfg.get("client_id", "")
    client_secret = wearable_cfg.get("client_secret", "")
    token_path = wearable_cfg.get("token_path", "data/fitbit_token.json")

    if not client_id:
        print("Error: fitbit client_id missing in config.yaml → wearables.fitbit")
        sys.exit(1)

    # PKCE: code_verifier + code_challenge
    code_verifier = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    redirect_uri = "http://localhost:9753/callback"
    auth_url = (
        "https://www.fitbit.com/oauth2/authorize?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "scope": "sleep activity heartrate",
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        })
    )

    params = await _run_local_oauth_server(auth_url, port=9753)
    code = params.get("code")
    if not code:
        raise RuntimeError(f"No code in callback params: {params}")

    print("  Exchanging code for tokens…")
    credentials = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()

    import httpx
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.fitbit.com/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            headers={
                "Authorization": f"Basic {credentials}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp.raise_for_status()
        token = resp.json()

    _save_token_file(token_path, {
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
    })
    print("\n✅  Fitbit authorisation complete.")
    print(f"    Update your config.yaml → wearables.fitbit.token_path: {token_path}")


# ---------------------------------------------------------------------------
# WHOOP OAuth 2.0
# ---------------------------------------------------------------------------

async def _auth_whoop(config: dict) -> None:
    """
    WHOOP OAuth 2.0 flow.
    redirect_uri must be registered at developer.whoop.com as:
        http://localhost:9753/callback
    """
    import urllib.parse
    import json

    wearable_cfg = config.get("wearables", {}).get("whoop", {})
    client_id = wearable_cfg.get("client_id", "")
    client_secret = wearable_cfg.get("client_secret", "")
    token_path = wearable_cfg.get("token_path", "data/whoop_token.json")

    if not client_id or not client_secret:
        print("Error: whoop client_id / client_secret missing in config.yaml → wearables.whoop")
        sys.exit(1)

    redirect_uri = "http://localhost:9753/callback"
    auth_url = (
        "https://api.prod.whoop.com/oauth/oauth2/auth?"
        + urllib.parse.urlencode({
            "client_id": client_id,
            "response_type": "code",
            "scope": "read:recovery read:sleep read:workout read:cycles read:body_measurement offline",
            "redirect_uri": redirect_uri,
            "state": "body-tracker",
        })
    )

    params = await _run_local_oauth_server(auth_url, port=9753)
    code = params.get("code")
    if not code:
        raise RuntimeError(f"No code in callback params: {params}")

    print("  Exchanging code for tokens…")
    import httpx
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(
            "https://api.prod.whoop.com/oauth/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        token = resp.json()

    _save_token_file(token_path, {
        "access_token": token["access_token"],
        "refresh_token": token["refresh_token"],
    })
    print("\n✅  WHOOP authorisation complete.")
    print(f"    Update your config.yaml → wearables.whoop.token_path: {token_path}")


def cmd_init(config_path: str) -> None:
    """Copy starter config to the given path."""
    import shutil
    source = Path(__file__).parent.parent / "kits" / "starter" / "config.yaml"
    dest = Path(config_path)
    if dest.exists():
        print(f"Config already exists: {dest}")
        return
    if not source.exists():
        print("Starter config template not found.")
        sys.exit(1)
    shutil.copy(source, dest)
    print(f"Config created: {dest}")
    print("Edit it to set your hardware, then run: body-tracker measure")


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="body-tracker",
        description="Open-source daily body tracking system",
    )
    parser.add_argument(
        "--config", "-c",
        default="config.yaml",
        help="Path to config.yaml (default: ./config.yaml)",
    )
    parser.add_argument("--verbose", "-v", action="store_true")

    sub = parser.add_subparsers(dest="command")

    # measure
    p_measure = sub.add_parser("measure", help="Run a single measurement session")
    p_measure.add_argument("--notes", "-n", default=None, help="Optional session notes")

    # daemon
    sub.add_parser("daemon", help="Start persistent daemon (waits for scale trigger)")

    # import
    p_import = sub.add_parser("import", help="Import historical data from CSV")
    p_import.add_argument("file", help="Path to CSV file")

    # history
    p_hist = sub.add_parser("history", help="Print recent sessions")
    p_hist.add_argument("--limit", "-n", type=int, default=20)

    # adapters
    sub.add_parser("adapters", help="List all available hardware adapters")

    # init
    p_init = sub.add_parser("init", help="Create a default config.yaml")
    p_init.add_argument(
        "--output", "-o", default="config.yaml",
        help="Where to write config (default: ./config.yaml)"
    )

    # auth
    p_auth = sub.add_parser(
        "auth",
        help="Authorise a cloud service (withings | fitbit | whoop)",
    )
    p_auth.add_argument(
        "service",
        choices=["withings", "fitbit", "whoop"],
        help="Which service to authorise",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    setup_logging(args.verbose)

    if args.command == "adapters":
        cmd_adapters()
        return

    if args.command == "init":
        cmd_init(args.output)
        return

    if not args.command:
        parser.print_help()
        return

    config = load_config(args.config)

    if args.command == "measure":
        asyncio.run(cmd_measure(config, getattr(args, "notes", None)))
    elif args.command == "daemon":
        asyncio.run(cmd_daemon(config))
    elif args.command == "import":
        asyncio.run(cmd_import(config, args.file))
    elif args.command == "history":
        asyncio.run(cmd_history(config, args.limit))
    elif args.command == "auth":
        asyncio.run(cmd_auth(args.service, config))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
