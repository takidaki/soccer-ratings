from __future__ import annotations

import errno
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .client import (
    build_and_cache_league_history,
    compare_teams_from_ratings,
    fetch_country_leagues,
    fetch_league_home_away_ratings,
    fetch_rankings,
    filter_matches_for_league,
    load_cached_league_history,
)
from .db import load_league_history_matches
from .db import (
    load_country_leagues as load_country_leagues_from_db,
)
from .db import (
    load_country_rankings as load_country_rankings_from_db,
)
from .db import (
    load_league_home_away_ratings as load_league_home_away_ratings_from_db,
)


class DashboardBindError(RuntimeError):
    """Raised when the dashboard cannot bind its requested address."""


def create_dashboard_handler() -> type[BaseHTTPRequestHandler]:
    countries_cache: list[dict] | None = None
    leagues_cache: dict[str, list[dict]] = {}
    ratings_cache: dict[str, dict] = {}

    class DashboardHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)

            if parsed.path == "/":
                self._send_html(INDEX_HTML)
                return

            if parsed.path == "/favicon.svg":
                self._send_svg(FAVICON_SVG)
                return

            if parsed.path == "/api/countries":
                nonlocal countries_cache
                if countries_cache is None:
                    try:
                        countries_cache = load_country_rankings_from_db()
                    except Exception:
                        countries_cache = None
                    if not countries_cache:
                        countries_cache = fetch_rankings()
                self._send_json({"countries": countries_cache})
                return

            if parsed.path == "/api/leagues":
                params = parse_qs(parsed.query)
                country_url = _require_query_param(params, "country_url")
                if not country_url:
                    self._send_json(
                        {"error": "Missing required query parameter: country_url"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                if country_url not in leagues_cache:
                    try:
                        leagues_cache[country_url] = load_country_leagues_from_db(country_url)
                    except Exception:
                        leagues_cache[country_url] = []
                    if not leagues_cache[country_url]:
                        leagues_cache[country_url] = fetch_country_leagues(country_url)
                self._send_json({"leagues": leagues_cache[country_url]})
                return

            if parsed.path == "/api/league-ratings":
                params = parse_qs(parsed.query)
                league_url = _require_query_param(params, "league_url")
                if not league_url:
                    self._send_json(
                        {"error": "Missing required query parameter: league_url"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                if league_url not in ratings_cache:
                    try:
                        ratings_cache[league_url] = load_league_home_away_ratings_from_db(league_url)
                    except Exception:
                        ratings_cache[league_url] = None
                    if not ratings_cache[league_url]:
                        ratings_cache[league_url] = fetch_league_home_away_ratings(league_url)
                self._send_json(ratings_cache[league_url])
                return

            if parsed.path == "/api/compare":
                params = parse_qs(parsed.query)
                league_url = _require_query_param(params, "league_url")
                home_team = _require_query_param(params, "home_team")
                away_team = _require_query_param(params, "away_team")
                margin_percent = _parse_float_query_param(params, "margin", default=0.0)
                if not league_url or not home_team or not away_team:
                    self._send_json(
                        {"error": "Missing required query parameters: league_url, home_team, away_team"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                if league_url not in ratings_cache:
                    try:
                        ratings_cache[league_url] = load_league_home_away_ratings_from_db(league_url)
                    except Exception:
                        ratings_cache[league_url] = None
                    if not ratings_cache[league_url]:
                        ratings_cache[league_url] = fetch_league_home_away_ratings(league_url)

                historical_matches: list[dict] = []
                history_source = "none"
                try:
                    historical_matches = load_league_history_matches(league_url)
                    if historical_matches:
                        history_source = "postgres"
                except Exception:
                    historical_matches = []

                if not historical_matches:
                    cached = load_cached_league_history(league_url)
                    if cached is not None:
                        historical_matches = filter_matches_for_league(
                            cached.get("matches", []),
                            league_url,
                        )
                        if historical_matches:
                            history_source = "cache"

                try:
                    comparison = compare_teams_from_ratings(
                        ratings_cache[league_url]["home"],
                        ratings_cache[league_url]["away"],
                        home_team=home_team,
                        away_team=away_team,
                        margin_percent=margin_percent,
                        historical_matches=historical_matches,
                    )
                except ValueError as exc:
                    self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                    return

                comparison["history_source"] = history_source
                self._send_json(comparison)
                return

            if parsed.path == "/api/league-history/status":
                params = parse_qs(parsed.query)
                league_url = _require_query_param(params, "league_url")
                if not league_url:
                    self._send_json(
                        {"error": "Missing required query parameter: league_url"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                cached = load_cached_league_history(league_url)
                if cached is None:
                    self._send_json({"cached": False, "league_url": league_url})
                    return

                self._send_json(
                    {
                        "cached": True,
                        "league_url": cached.get("league_url", league_url),
                        "team_count": cached.get("team_count", 0),
                        "raw_match_count": cached.get("raw_match_count", 0),
                        "deduped_match_count": cached.get("deduped_match_count", 0),
                        "cache_path": cached.get("cache_path", ""),
                    }
                )
                return

            if parsed.path == "/api/league-history/build":
                params = parse_qs(parsed.query)
                league_url = _require_query_param(params, "league_url")
                refresh = _require_query_param(params, "refresh") == "1"
                if not league_url:
                    self._send_json(
                        {"error": "Missing required query parameter: league_url"},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                    return

                payload = build_and_cache_league_history(league_url, force_refresh=refresh)
                self._send_json(payload)
                return

            self._send_json({"error": "Not found"}, status=HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args) -> None:  # noqa: A003
            return

        def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_json(self, data: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _send_svg(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "image/svg+xml; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return DashboardHandler


def run_dashboard(host: str = "127.0.0.1", port: int = 8001) -> None:
    try:
        server = ThreadingHTTPServer((host, port), create_dashboard_handler())
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            next_port = port + 1
            raise DashboardBindError(
                "\n".join(
                    [
                        f"Dashboard address http://{host}:{port} is already in use.",
                        "Stop the existing process or choose another port:",
                        f"  python3 app.py dashboard --port {next_port}",
                        "To find the process on macOS:",
                        f"  lsof -nP -iTCP:{port} -sTCP:LISTEN",
                    ]
                )
            ) from exc
        raise
    print(f"Dashboard running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _require_query_param(params: dict[str, list[str]], name: str) -> str | None:
    values = params.get(name)
    if not values:
        return None
    value = values[0].strip()
    return value or None


def _parse_float_query_param(
    params: dict[str, list[str]],
    name: str,
    default: float = 0.0,
) -> float:
    value = _require_query_param(params, name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Soccer Ratings Dashboard</title>
  <link rel="icon" href="/favicon.svg" type="image/svg+xml">
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Barlow:wght@500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #eef2f6;
      --bg-2: #dde5ee;
      --panel: rgba(255, 255, 255, 0.96);
      --panel-2: rgba(247, 250, 252, 0.98);
      --panel-3: rgba(12, 25, 42, 0.04);
      --line: rgba(16, 24, 40, 0.08);
      --line-strong: rgba(16, 24, 40, 0.14);
      --ink: #111827;
      --muted: #667085;
      --accent: #e11d2e;
      --accent-2: #0ea5e9;
      --accent-3: #1d4ed8;
      --shadow: 0 18px 50px rgba(15, 23, 42, 0.08);
    }

    * {
      box-sizing: border-box;
    }

    body {
      margin: 0;
      font-family: "Inter", sans-serif;
      color: var(--ink);
      background:
        linear-gradient(180deg, #f7f9fc 0%, var(--bg) 55%, #e9eef4 100%);
      min-height: 100vh;
      position: relative;
      overflow-x: hidden;
    }

    body::before {
      content: "";
      position: fixed;
      inset: 0;
      background:
        linear-gradient(rgba(17, 24, 39, 0.018) 1px, transparent 1px),
        linear-gradient(90deg, rgba(17, 24, 39, 0.018) 1px, transparent 1px);
      background-size: 96px 96px;
      mask-image: linear-gradient(180deg, rgba(0, 0, 0, 0.16), transparent 85%);
      pointer-events: none;
    }

    .shell {
      position: relative;
      width: min(1320px, calc(100% - 32px));
      margin: 28px auto;
      padding: 28px;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: linear-gradient(180deg, rgba(255, 255, 255, 0.96), rgba(248, 250, 252, 0.98));
      box-shadow: var(--shadow);
      overflow: hidden;
    }

    .shell::before {
      content: "";
      position: absolute;
      inset: 0;
      background:
        linear-gradient(90deg, rgba(225, 29, 46, 0.04), transparent 22%, transparent 78%, rgba(14, 165, 233, 0.04));
      pointer-events: none;
    }

    .hero {
      position: relative;
      display: grid;
      grid-template-columns: 1.6fr 0.4fr;
      gap: 16px;
      align-items: start;
      margin-bottom: 12px;
    }

    .eyebrow {
      display: inline-block;
      font-size: 11px;
      letter-spacing: 0.18em;
      text-transform: uppercase;
      color: var(--accent);
      margin-bottom: 12px;
      font-weight: 700;
    }

    h1 {
      margin: 0 0 8px;
      max-width: 16ch;
      font-family: "Barlow", sans-serif;
      font-size: clamp(20px, 2.8vw, 32px);
      line-height: 1.02;
      font-weight: 700;
      letter-spacing: -0.03em;
    }

    .subtitle {
      margin: 0;
      max-width: 72ch;
      font-size: 13px;
      line-height: 1.45;
      color: var(--muted);
    }

    .hero-main {
      display: grid;
      gap: 12px;
    }

    .metric-strip {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 10px;
    }

    .metric {
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: linear-gradient(180deg, #ffffff, #f9fbfd);
    }

    .metric-label {
      display: block;
      font-size: 9px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 4px;
      font-weight: 600;
    }

    .metric-value {
      font-family: "Barlow", sans-serif;
      font-size: clamp(16px, 1.8vw, 22px);
      font-weight: 700;
      color: var(--ink);
    }

    .hero-card {
      position: relative;
      padding: 14px 16px;
      border-radius: 16px;
      background:
        linear-gradient(180deg, #101828, #172234),
        linear-gradient(135deg, rgba(225, 29, 46, 0.08), transparent);
      border: 1px solid var(--line-strong);
      min-height: 0;
      overflow: hidden;
    }

    .hero-card::after {
      content: "";
      position: absolute;
      right: -18px;
      bottom: -22px;
      width: 72px;
      height: 72px;
      border: 1px solid rgba(255, 255, 255, 0.12);
      border-radius: 50%;
      box-shadow:
        0 0 0 12px rgba(255, 255, 255, 0.04),
        0 0 0 28px rgba(255, 255, 255, 0.02);
    }

    .hero-card strong {
      position: relative;
      display: block;
      margin-bottom: 6px;
      font-size: 9px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #fda4af;
      font-family: "Barlow", sans-serif;
    }

    .hero-card p {
      position: relative;
      margin: 0;
      line-height: 1.45;
      font-size: 12px;
      color: rgba(255, 255, 255, 0.8);
    }

    .controls {
      position: relative;
      display: grid;
      grid-template-columns: 1fr 1fr auto;
      gap: 18px;
      margin-bottom: 16px;
      padding: 18px;
      border: 1px solid var(--line);
      border-radius: 20px;
      background: linear-gradient(180deg, #ffffff, #f8fafc);
    }

    .control {
      display: flex;
      flex-direction: column;
      gap: 8px;
    }

    label {
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }

    select, button {
      width: 100%;
      min-height: 50px;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      font: inherit;
      font-size: 14px;
      background: #ffffff;
      color: var(--ink);
      outline: none;
      transition: border-color 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }

    select:focus, button:focus {
      border-color: rgba(225, 29, 46, 0.35);
      box-shadow: 0 0 0 4px rgba(225, 29, 46, 0.1);
    }

    button {
      align-self: end;
      border: 0;
      background:
        linear-gradient(135deg, #ef233c, #c1121f);
      color: white;
      font-family: "Barlow", sans-serif;
      font-weight: 700;
      letter-spacing: 0.01em;
      cursor: pointer;
      transition: transform 140ms ease, filter 140ms ease;
    }

    button:hover {
      transform: translateY(-1px);
      filter: brightness(1.04);
    }

    button:disabled, select:disabled {
      opacity: 0.5;
      cursor: not-allowed;
    }

    .status {
      display: flex;
      align-items: center;
      gap: 10px;
      min-height: 24px;
      margin-bottom: 18px;
      color: var(--muted);
      font-size: 14px;
    }

    .status::before {
      content: "";
      width: 10px;
      height: 10px;
      border-radius: 999px;
      background: var(--accent);
      box-shadow: 0 0 0 4px rgba(225, 29, 46, 0.12);
      flex: none;
    }

    .panes {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 20px;
    }

    .matchup {
      margin-bottom: 20px;
      border: 1px solid var(--line);
      border-radius: 22px;
      background: linear-gradient(180deg, #ffffff, #f8fafc);
      overflow: hidden;
    }

    .tabs {
      display: flex;
      gap: 8px;
      padding: 14px 18px 0;
    }

    .tab-button {
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 9px 14px;
      background: white;
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      letter-spacing: 0.04em;
      text-transform: uppercase;
      cursor: pointer;
    }

    .tab-button.is-active {
      background: linear-gradient(135deg, #ef233c, #c1121f);
      color: white;
      border-color: transparent;
    }

    .matchup-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      gap: 12px;
      padding: 16px 18px 12px;
      border-bottom: 1px solid var(--line);
      background: linear-gradient(180deg, rgba(225, 29, 46, 0.06), rgba(225, 29, 46, 0));
    }

    .matchup-header h2 {
      margin: 0;
      font-family: "Barlow", sans-serif;
      font-size: 20px;
    }

    .matchup-subtitle {
      font-size: 12px;
      color: var(--muted);
    }

    .matchup-body {
      display: grid;
      grid-template-columns: 1.2fr 1fr;
      gap: 18px;
      padding: 18px;
    }

    .tab-panel {
      display: none;
    }

    .tab-panel.is-active {
      display: block;
    }

    .matchup-controls {
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 14px;
      align-items: end;
    }

    .matchup-cards {
      display: grid;
      grid-template-columns: repeat(5, minmax(0, 1fr));
      gap: 12px;
    }

    .odds-card {
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fbfcfe;
    }

    .odds-label {
      display: block;
      margin-bottom: 8px;
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      font-weight: 700;
    }

    .odds-prob {
      display: block;
      font-family: "Barlow", sans-serif;
      font-size: 26px;
      font-weight: 700;
      color: var(--ink);
    }

    .odds-decimal {
      display: block;
      margin-top: 4px;
      font-size: 12px;
      color: var(--accent-3);
      font-weight: 700;
    }

    .rating-gap {
      display: flex;
      justify-content: space-between;
      gap: 12px;
      margin-top: 16px;
      padding-top: 14px;
      border-top: 1px solid var(--line);
      font-size: 13px;
      color: var(--muted);
    }

    .rating-gap strong {
      color: var(--ink);
      font-family: "Barlow", sans-serif;
      font-size: 15px;
    }

    input[type="number"] {
      width: 100%;
      min-height: 50px;
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px 14px;
      font: inherit;
      font-size: 14px;
      background: #ffffff;
      color: var(--ink);
      outline: none;
      transition: border-color 140ms ease, box-shadow 140ms ease, background 140ms ease;
    }

    input[type="number"]:focus {
      border-color: rgba(225, 29, 46, 0.35);
      box-shadow: 0 0 0 4px rgba(225, 29, 46, 0.1);
    }

    .market-meta {
      margin-top: 10px;
      font-size: 12px;
      color: var(--muted);
    }

    .multi-body {
      padding: 18px;
      display: grid;
      gap: 16px;
    }

    .multi-toolbar {
      display: grid;
      grid-template-columns: 1fr;
      gap: 12px;
      align-items: end;
    }

    .multi-list {
      display: grid;
      gap: 12px;
    }

    .multi-row {
      display: grid;
      grid-template-columns: 1.2fr 1.2fr repeat(5, minmax(88px, 1fr));
      gap: 10px;
      align-items: end;
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: #fbfcfe;
    }

    .multi-leg-odds {
      min-height: 50px;
      padding: 8px 10px;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      border: 1px solid var(--line);
      border-radius: 14px;
      background: white;
      text-align: center;
    }

    .multi-leg-odds strong {
      font-size: 10px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 4px;
    }

    .multi-leg-odds span {
      font-family: "Barlow", sans-serif;
      font-size: 20px;
      font-weight: 700;
      color: var(--accent-3);
    }

    .multi-summary {
      display: grid;
      grid-template-columns: 1fr 1fr 1fr;
      gap: 12px;
    }

    .summary-card {
      padding: 14px;
      border: 1px solid var(--line);
      border-radius: 16px;
      background: white;
    }

    .summary-card strong {
      display: block;
      margin-bottom: 6px;
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
    }

    .summary-card span {
      font-family: "Barlow", sans-serif;
      font-size: 24px;
      font-weight: 700;
      color: var(--ink);
    }

    .multi-help {
      font-size: 12px;
      color: var(--muted);
    }

    .pane {
      border: 1px solid var(--line);
      border-radius: 22px;
      overflow: hidden;
      background: linear-gradient(180deg, #ffffff, #f8fafc);
    }

    .pane-header {
      display: flex;
      justify-content: space-between;
      align-items: baseline;
      padding: 18px 20px 14px;
      background:
        linear-gradient(180deg, rgba(14, 165, 233, 0.08), rgba(14, 165, 233, 0)),
        linear-gradient(90deg, rgba(255, 255, 255, 0.6), transparent);
      border-bottom: 1px solid var(--line);
    }

    .pane-header h2 {
      margin: 0;
      font-family: "Barlow", sans-serif;
      font-size: 20px;
    }

    .count {
      font-size: 11px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--accent-2);
      font-weight: 700;
    }

    table {
      width: 100%;
      border-collapse: collapse;
    }

    th, td {
      text-align: left;
      padding: 13px 16px;
      border-bottom: 1px solid rgba(16, 24, 40, 0.07);
      font-size: 14px;
    }

    th {
      font-size: 10px;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: var(--muted);
      background: #f8fafc;
      font-weight: 700;
    }

    tbody tr:nth-child(odd) {
      background: #fcfdff;
    }

    tbody tr:hover {
      background: #eef6ff;
    }

    td:first-child {
      width: 74px;
      font-family: "Barlow", sans-serif;
      font-weight: 700;
      color: var(--accent-2);
    }

    td:last-child {
      text-align: right;
      font-family: "Barlow", sans-serif;
      font-weight: 700;
    }

    .table-wrap {
      max-height: 720px;
      overflow: auto;
    }

    .table-wrap::-webkit-scrollbar {
      width: 10px;
      height: 10px;
    }

    .table-wrap::-webkit-scrollbar-thumb {
      background: rgba(14, 165, 233, 0.22);
      border-radius: 999px;
    }

    .empty {
      padding: 32px 20px;
      color: var(--muted);
    }

    .empty strong {
      display: block;
      margin-bottom: 6px;
      font-family: "Barlow", sans-serif;
      font-size: 16px;
      color: var(--ink);
    }

    .status-bar {
      display: grid;
      grid-template-columns: 1fr auto;
      gap: 18px;
      align-items: center;
      margin-bottom: 22px;
    }

    .history-cache {
      display: grid;
      grid-template-columns: auto 1fr;
      gap: 12px;
      align-items: center;
      margin-bottom: 18px;
      padding: 14px 16px;
      border: 1px solid var(--line);
      border-radius: 18px;
      background: white;
    }

    .history-cache-meta {
      font-size: 12px;
      color: var(--muted);
    }

    .selection-pill {
      justify-self: end;
      padding: 9px 12px;
      border: 1px solid var(--line);
      border-radius: 999px;
      background: white;
      font-size: 11px;
      color: var(--accent-3);
      letter-spacing: 0.06em;
      text-transform: uppercase;
      font-weight: 700;
    }

    @media (max-width: 920px) {
      .hero, .controls, .panes, .metric-strip, .status-bar, .matchup-body, .matchup-controls, .matchup-cards, .multi-toolbar, .multi-row, .multi-summary, .history-cache {
        grid-template-columns: 1fr;
      }

      .selection-pill {
        justify-self: start;
      }

      .hero-card {
        display: none;
      }
    }
  </style>
</head>
<body>
  <main class="shell">
    <section class="hero">
      <div class="hero-main">
        <div>
          <span class="eyebrow">Soccer Rating Explorer</span>
          <h1>Home and away ratings by league.</h1>
          <p class="subtitle">
            Choose a country and league, then compare venue splits side by side.
          </p>
        </div>
        <div class="metric-strip">
          <div class="metric">
            <span class="metric-label">Selection State</span>
            <span class="metric-value" id="metric-selection">Idle</span>
          </div>
          <div class="metric">
            <span class="metric-label">Home Teams</span>
            <span class="metric-value" id="metric-home">0</span>
          </div>
          <div class="metric">
            <span class="metric-label">Away Teams</span>
            <span class="metric-value" id="metric-away">0</span>
          </div>
        </div>
      </div>
      <aside class="hero-card">
        <strong>Quick View</strong>
        <p>
          Compare club strength at home versus away without leaving the dashboard.
        </p>
      </aside>
    </section>

    <section class="controls">
      <div class="control">
        <label for="country">Country</label>
        <select id="country" disabled>
          <option>Loading countries...</option>
        </select>
      </div>
      <div class="control">
        <label for="league">League</label>
        <select id="league" disabled>
          <option>Select a country first</option>
        </select>
      </div>
      <div class="control">
        <label for="build-history">League History</label>
        <button id="build-history" disabled>Build Cache</button>
      </div>
    </section>

    <div class="status-bar">
      <div class="status" id="status">Preparing dashboard...</div>
      <div class="selection-pill" id="selection-pill">No league selected</div>
    </div>

    <div class="history-cache">
      <div class="history-cache-meta" id="history-cache-meta">No league history cache yet.</div>
      <div class="history-cache-meta" id="history-cache-path"></div>
    </div>

    <section class="matchup">
      <div class="tabs">
        <button class="tab-button is-active" data-tab-target="single">Single Match</button>
        <button class="tab-button" data-tab-target="multi">Multi Match</button>
      </div>
      <div class="matchup-header">
        <h2>Team Comparison</h2>
        <span class="matchup-subtitle">Use one match or build a multi from the selected league.</span>
      </div>
      <div class="tab-panel is-active" data-tab-panel="single">
        <div class="matchup-body">
          <div>
            <div class="matchup-controls">
              <div class="control">
                <label for="home-team">Home Team</label>
                <select id="home-team" disabled>
                  <option>Load league ratings first</option>
                </select>
              </div>
              <div class="control">
                <label for="away-team">Away Team</label>
                <select id="away-team" disabled>
                  <option>Load league ratings first</option>
                </select>
              </div>
              <div class="control">
                <label for="margin">Margin %</label>
                <input id="margin" type="number" min="0" step="0.1" value="0.0">
              </div>
            </div>
            <div class="rating-gap">
              <span>Home team home rating: <strong id="selected-home-rating">-</strong></span>
              <span>Away team away rating: <strong id="selected-away-rating">-</strong></span>
              <span>Rating gap: <strong id="rating-gap">-</strong></span>
            </div>
            <div class="market-meta" id="market-meta">Fair odds with 0.00% margin.</div>
          </div>
          <div class="matchup-cards" id="matchup-cards">
            <div class="odds-card">
              <span class="odds-label">Home Win</span>
              <span class="odds-prob" id="home-win-prob">-</span>
              <span class="odds-decimal" id="home-win-odds">Odds -</span>
            </div>
            <div class="odds-card">
              <span class="odds-label">Draw</span>
              <span class="odds-prob" id="draw-prob">-</span>
              <span class="odds-decimal" id="draw-odds">Odds -</span>
            </div>
            <div class="odds-card">
              <span class="odds-label">Away Win</span>
              <span class="odds-prob" id="away-win-prob">-</span>
              <span class="odds-decimal" id="away-win-odds">Odds -</span>
            </div>
            <div class="odds-card">
              <span class="odds-label">Home DNB</span>
              <span class="odds-prob" id="home-dnb-prob">-</span>
              <span class="odds-decimal" id="home-dnb-odds">Odds -</span>
            </div>
            <div class="odds-card">
              <span class="odds-label">Away DNB</span>
              <span class="odds-prob" id="away-dnb-prob">-</span>
              <span class="odds-decimal" id="away-dnb-odds">Odds -</span>
            </div>
          </div>
        </div>
      </div>
      <div class="tab-panel" data-tab-panel="multi">
        <div class="multi-body">
          <div class="multi-toolbar">
            <div class="control">
              <label for="multi-margin">Multi Margin %</label>
              <input id="multi-margin" type="number" min="0" step="0.1" value="0.0">
            </div>
          </div>
          <div class="multi-list" id="multi-list">
            <div class="empty">
              <strong>No rows yet</strong>
              Load a league and rows will be created automatically from the league size.
            </div>
          </div>
          <div class="multi-summary">
            <div class="summary-card">
              <strong>Rows Created</strong>
              <span id="multi-rows-created">0</span>
            </div>
            <div class="summary-card">
              <strong>Rows Ready</strong>
              <span id="multi-rows-ready">0</span>
            </div>
            <div class="summary-card">
              <strong>Margin</strong>
              <span id="multi-margin-display">0.00%</span>
            </div>
          </div>
          <div class="multi-help" id="multi-help">Each row shows `1`, `X`, `2`, `Home DNB`, and `Away DNB` for one matchup from the selected league.</div>
        </div>
      </div>
    </section>

    <section class="panes">
      <article class="pane">
        <div class="pane-header">
          <h2>Home Ratings</h2>
          <span class="count" id="home-count">0 teams</span>
        </div>
        <div id="home-table"></div>
      </article>
      <article class="pane">
        <div class="pane-header">
          <h2>Away Ratings</h2>
          <span class="count" id="away-count">0 teams</span>
        </div>
        <div id="away-table"></div>
      </article>
    </section>
  </main>

  <script>
    const state = {
      countries: [],
      leagues: [],
      selectedCountry: "",
      selectedLeague: "",
      currentRatings: null,
      loadingLeagueRatings: false,
      loadingComparison: false,
      activeTab: "single",
      multiRows: []
    };

    const countrySelect = document.getElementById("country");
    const leagueSelect = document.getElementById("league");
    const buildHistoryButton = document.getElementById("build-history");
    const statusEl = document.getElementById("status");
    const homeTable = document.getElementById("home-table");
    const awayTable = document.getElementById("away-table");
    const homeCount = document.getElementById("home-count");
    const awayCount = document.getElementById("away-count");
    const metricSelection = document.getElementById("metric-selection");
    const metricHome = document.getElementById("metric-home");
    const metricAway = document.getElementById("metric-away");
    const selectionPill = document.getElementById("selection-pill");
    const homeTeamSelect = document.getElementById("home-team");
    const awayTeamSelect = document.getElementById("away-team");
    const selectedHomeRating = document.getElementById("selected-home-rating");
    const selectedAwayRating = document.getElementById("selected-away-rating");
    const ratingGapEl = document.getElementById("rating-gap");
    const homeWinProb = document.getElementById("home-win-prob");
    const drawProb = document.getElementById("draw-prob");
    const awayWinProb = document.getElementById("away-win-prob");
    const homeWinOdds = document.getElementById("home-win-odds");
    const drawOdds = document.getElementById("draw-odds");
    const awayWinOdds = document.getElementById("away-win-odds");
    const homeDnbProb = document.getElementById("home-dnb-prob");
    const awayDnbProb = document.getElementById("away-dnb-prob");
    const homeDnbOdds = document.getElementById("home-dnb-odds");
    const awayDnbOdds = document.getElementById("away-dnb-odds");
    const marginInput = document.getElementById("margin");
    const marketMeta = document.getElementById("market-meta");
    const tabButtons = Array.from(document.querySelectorAll("[data-tab-target]"));
    const tabPanels = Array.from(document.querySelectorAll("[data-tab-panel]"));
    const multiMarginInput = document.getElementById("multi-margin");
    const multiList = document.getElementById("multi-list");
    const multiRowsCreated = document.getElementById("multi-rows-created");
    const multiRowsReady = document.getElementById("multi-rows-ready");
    const multiMarginDisplay = document.getElementById("multi-margin-display");
    const multiHelp = document.getElementById("multi-help");
    const historyCacheMeta = document.getElementById("history-cache-meta");
    const historyCachePath = document.getElementById("history-cache-path");

    function setStatus(message) {
      statusEl.textContent = message;
    }

    function updateSummary() {
      const country = state.countries.find((item) => item.country_path === state.selectedCountry);
      const league = state.leagues.find((item) => item.league_path === state.selectedLeague);
      if (country && league) {
        selectionPill.textContent = `${country.country} • ${league.league}`;
        metricSelection.textContent = "Live";
        return;
      }
      if (country) {
        selectionPill.textContent = `${country.country} selected`;
        metricSelection.textContent = "Country";
        return;
      }
      selectionPill.textContent = "No league selected";
      metricSelection.textContent = "Idle";
    }

    function setActiveTab(tabName) {
      state.activeTab = tabName;
      for (const button of tabButtons) {
        button.classList.toggle("is-active", button.dataset.tabTarget === tabName);
      }
      for (const panel of tabPanels) {
        panel.classList.toggle("is-active", panel.dataset.tabPanel === tabName);
      }
    }

    function renderSelect(select, items, placeholder, labelKey, valueKey) {
      select.innerHTML = "";

      if (!items.length) {
        const option = document.createElement("option");
        option.textContent = placeholder;
        option.value = "";
        select.appendChild(option);
        return;
      }

      const placeholderOption = document.createElement("option");
      placeholderOption.textContent = placeholder;
      placeholderOption.value = "";
      select.appendChild(placeholderOption);

      for (const item of items) {
        const option = document.createElement("option");
        option.textContent = item[labelKey];
        option.value = item[valueKey];
        select.appendChild(option);
      }
    }

    function renderTeamSelect(select, rows, placeholder) {
      renderSelect(select, rows, placeholder, "team", "team");
    }

    function resetHistoryCacheStatus() {
      historyCacheMeta.textContent = "No league history cache yet.";
      historyCachePath.textContent = "";
    }

    function cloneRows(rows) {
      return (rows || []).map((row) => ({ ...row }));
    }

    function resetComparison() {
      renderTeamSelect(homeTeamSelect, [], "Load league ratings first");
      renderTeamSelect(awayTeamSelect, [], "Load league ratings first");
      homeTeamSelect.disabled = true;
      awayTeamSelect.disabled = true;
      selectedHomeRating.textContent = "-";
      selectedAwayRating.textContent = "-";
      ratingGapEl.textContent = "-";
      homeWinProb.textContent = "-";
      drawProb.textContent = "-";
      awayWinProb.textContent = "-";
      homeDnbProb.textContent = "-";
      awayDnbProb.textContent = "-";
      homeWinOdds.textContent = "Odds -";
      drawOdds.textContent = "Odds -";
      awayWinOdds.textContent = "Odds -";
      homeDnbOdds.textContent = "Odds -";
      awayDnbOdds.textContent = "Odds -";
      marketMeta.textContent = `Fair odds with ${Number(marginInput.value || 0).toFixed(2)}% margin.`;
    }

    function resetMultiBuilder() {
      state.multiRows = [];
      renderMultiRows();
    }

    function initializeMultiRows(homeRows, awayRows) {
      const rowCount = Math.floor(Math.min(homeRows.length, awayRows.length) / 2);
      state.multiRows = Array.from({ length: rowCount }, (_, index) => ({
        id: `row-${index + 1}`,
        homeTeam: "",
        awayTeam: "",
        odds: null,
        status: "pending"
      }));
      renderMultiRows();
    }

    function renderMultiRows() {
      const homeRows = cloneRows(state.currentRatings?.home || []);
      const awayRows = cloneRows(state.currentRatings?.away || []);

      if (!state.multiRows.length) {
        multiList.innerHTML = `
          <div class="empty">
            <strong>No rows yet</strong>
            Load a league and rows will be created automatically from the league size.
          </div>
        `;
        updateMultiSummary();
        return;
      }

      multiList.innerHTML = state.multiRows.map((row, index) => `
        <div class="multi-row" data-row-id="${row.id}">
          <div class="control">
            <label>Home Team ${index + 1}</label>
            <select data-field="homeTeam">
              ${buildOptions(homeRows, row.homeTeam, "Choose home team", "team", "team")}
            </select>
          </div>
          <div class="control">
            <label>Away Team ${index + 1}</label>
            <select data-field="awayTeam">
              ${buildOptions(awayRows, row.awayTeam, "Choose away team", "team", "team")}
            </select>
          </div>
          ${buildMultiOddsCell("1", formatMultiOdds(row, "1"))}
          ${buildMultiOddsCell("X", formatMultiOdds(row, "X"))}
          ${buildMultiOddsCell("2", formatMultiOdds(row, "2"))}
          ${buildMultiOddsCell("Home DNB", formatMultiOdds(row, "DNB1"))}
          ${buildMultiOddsCell("Away DNB", formatMultiOdds(row, "DNB2"))}
        </div>
      `).join("");

      updateMultiSummary();
    }

    function buildMultiOddsCell(label, value) {
      return `
        <div class="multi-leg-odds">
          <strong>${label}</strong>
          <span>${value}</span>
        </div>
      `;
    }

    function buildOptions(items, selectedValue, placeholder, labelKey, valueKey) {
      const placeholderOption = `<option value="">${placeholder}</option>`;
      const options = items.map((item) => {
        const value = String(item[valueKey]);
        const selected = value === String(selectedValue) ? " selected" : "";
        return `<option value="${escapeHtml(value)}"${selected}>${escapeHtml(String(item[labelKey]))}</option>`;
      }).join("");
      return placeholderOption + options;
    }

    function formatMultiOdds(row, market) {
      if (row.status === "loading") {
        return "…";
      }
      if (row.status === "error") {
        return "Err";
      }
      if (row.odds && typeof row.odds[market] === "number" && row.odds[market] > 0) {
        return Number(row.odds[market]).toFixed(2);
      }
      return "-";
    }

    function updateMultiSummary() {
      const readyRows = state.multiRows.filter((row) => row.odds && typeof row.odds["1"] === "number");
      multiRowsCreated.textContent = `${state.multiRows.length}`;
      multiRowsReady.textContent = `${readyRows.length}`;
      multiMarginDisplay.textContent = `${Number(multiMarginInput.value || 0).toFixed(2)}%`;

      if (!state.multiRows.length) {
        multiHelp.textContent = "Each row shows `1`, `X`, `2`, `Home DNB`, and `Away DNB` for one matchup from the selected league.";
        return;
      }

      multiHelp.textContent = `${readyRows.length} of ${state.multiRows.length} rows currently have matchup odds.`;
    }

    async function updateMultiRow(rowId) {
      const row = state.multiRows.find((item) => item.id === rowId);
      if (!row || !state.selectedLeague) {
        return;
      }

      if (!row.homeTeam || !row.awayTeam) {
        row.odds = null;
        row.status = "pending";
        renderMultiRows();
        return;
      }

      if (row.homeTeam === row.awayTeam) {
        row.odds = null;
        row.status = "error";
        renderMultiRows();
        return;
      }

      row.status = "loading";
      renderMultiRows();

      try {
        const data = await fetchJson(
          `/api/compare?league_url=${encodeURIComponent(state.selectedLeague)}&home_team=${encodeURIComponent(row.homeTeam)}&away_team=${encodeURIComponent(row.awayTeam)}&margin=${encodeURIComponent(multiMarginInput.value || "0")}`
        );
        row.odds = {
          "1": Number(data.market_odds.home),
          "X": Number(data.market_odds.draw),
          "2": Number(data.market_odds.away),
          "DNB1": Number(data.market_dnb_odds.home),
          "DNB2": Number(data.market_dnb_odds.away)
        };
        row.status = "ready";
      } catch (error) {
        row.odds = null;
        row.status = "error";
        setStatus(error.message);
      }

      renderMultiRows();
    }

    async function refreshAllMultiRows() {
      for (const row of state.multiRows) {
        await updateMultiRow(row.id);
      }
    }

    function renderTable(container, rows) {
      if (!rows.length) {
        container.innerHTML = `
          <div class="empty">
            <strong>No data yet</strong>
            Load a league to display the current team ratings table.
          </div>
        `;
        return;
      }

      const body = rows.map((row) => `
        <tr>
          <td>${row.rank}</td>
          <td>${escapeHtml(row.team)}</td>
          <td>${Number(row.rating).toFixed(2)}</td>
        </tr>
      `).join("");

      container.innerHTML = `
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Rank</th>
                <th>Team</th>
                <th>Rating</th>
              </tr>
            </thead>
            <tbody>${body}</tbody>
          </table>
        </div>
      `;
    }

    function escapeHtml(value) {
      return value
        .replaceAll("&", "&amp;")
        .replaceAll("<", "&lt;")
        .replaceAll(">", "&gt;")
        .replaceAll('"', "&quot;")
        .replaceAll("'", "&#039;");
    }

    async function fetchJson(url) {
      const response = await fetch(url);
      if (!response.ok) {
        const payload = await response.json().catch(() => ({ error: "Request failed" }));
        throw new Error(payload.error || "Request failed");
      }
      return response.json();
    }

    async function loadCountries() {
      setStatus("Loading countries...");
      const data = await fetchJson("/api/countries");
      state.countries = data.countries;
      renderSelect(countrySelect, state.countries, "Choose a country", "country", "country_path");
      countrySelect.disabled = false;
      updateSummary();
      setStatus("Choose a country to load its leagues.");
    }

    async function loadLeagues(countryUrl) {
      leagueSelect.disabled = true;
      renderSelect(leagueSelect, [], "Loading leagues...", "league", "league_path");
      setStatus("Loading leagues...");

      const data = await fetchJson(`/api/leagues?country_url=${encodeURIComponent(countryUrl)}`);
      state.leagues = data.leagues;
      renderSelect(leagueSelect, state.leagues, "Choose a league", "league", "league_path");
      leagueSelect.disabled = false;
      updateSummary();
      setStatus("Choose a league to view home and away ratings.");
    }

    async function loadRatings(leagueUrl) {
      state.loadingLeagueRatings = true;
      leagueSelect.disabled = true;
      setStatus("Loading home and away ratings...");
      const data = await fetchJson(`/api/league-ratings?league_url=${encodeURIComponent(leagueUrl)}`);
      state.currentRatings = data;
      renderTable(homeTable, data.home || []);
      renderTable(awayTable, data.away || []);
      homeCount.textContent = `${(data.home || []).length} teams`;
      awayCount.textContent = `${(data.away || []).length} teams`;
      metricHome.textContent = `${(data.home || []).length}`;
      metricAway.textContent = `${(data.away || []).length}`;
      renderTeamSelect(homeTeamSelect, data.home || [], "Choose home team");
      renderTeamSelect(awayTeamSelect, data.away || [], "Choose away team");
      homeTeamSelect.disabled = !(data.home || []).length;
      awayTeamSelect.disabled = !(data.away || []).length;
      initializeMultiRows(data.home || [], data.away || []);
      buildHistoryButton.disabled = false;
      leagueSelect.disabled = false;
      state.loadingLeagueRatings = false;
      const league = state.leagues.find((item) => item.league_path === leagueUrl);
      updateSummary();
      await loadHistoryCacheStatus(leagueUrl);
      setStatus(`Showing ratings for ${league ? league.league : "selected league"}.`);
    }

    async function loadHistoryCacheStatus(leagueUrl) {
      if (!leagueUrl) {
        resetHistoryCacheStatus();
        return;
      }

      const data = await fetchJson(`/api/league-history/status?league_url=${encodeURIComponent(leagueUrl)}`);
      if (!data.cached) {
        historyCacheMeta.textContent = "No cached league history dataset for this league yet.";
        historyCachePath.textContent = "";
        return;
      }

      historyCacheMeta.textContent = `Cached dataset: ${data.deduped_match_count} deduped matches from ${data.team_count} teams (${data.raw_match_count} raw rows).`;
      historyCachePath.textContent = data.cache_path || "";
    }

    async function buildLeagueHistoryCache(forceRefresh = true) {
      if (!state.selectedLeague) {
        return;
      }

      buildHistoryButton.disabled = true;
      buildHistoryButton.textContent = forceRefresh ? "Refreshing..." : "Building...";
      setStatus("Building league history cache...");
      const data = await fetchJson(
        `/api/league-history/build?league_url=${encodeURIComponent(state.selectedLeague)}&refresh=${forceRefresh ? "1" : "0"}`
      );
      historyCacheMeta.textContent = `Cached dataset: ${data.deduped_match_count} deduped matches from ${data.team_count} teams (${data.raw_match_count} raw rows).`;
      historyCachePath.textContent = data.cache_path || "";
      buildHistoryButton.textContent = "Refresh Cache";
      buildHistoryButton.disabled = false;
      setStatus("League history cache is ready.");
    }

    async function compareTeams() {
      if (!state.selectedLeague || !homeTeamSelect.value || !awayTeamSelect.value || state.loadingComparison) {
        return;
      }

      state.loadingComparison = true;
      homeTeamSelect.disabled = true;
      awayTeamSelect.disabled = true;
      setStatus("Calculating matchup probabilities and odds...");
      const data = await fetchJson(
        `/api/compare?league_url=${encodeURIComponent(state.selectedLeague)}&home_team=${encodeURIComponent(homeTeamSelect.value)}&away_team=${encodeURIComponent(awayTeamSelect.value)}&margin=${encodeURIComponent(marginInput.value || "0")}`
      );

      selectedHomeRating.textContent = Number(data.home_team.rating).toFixed(2);
      selectedAwayRating.textContent = Number(data.away_team.rating).toFixed(2);
      ratingGapEl.textContent = Number(data.rating_gap).toFixed(2);
      homeWinProb.textContent = `${(Number(data.probabilities.home) * 100).toFixed(1)}%`;
      drawProb.textContent = `${(Number(data.probabilities.draw) * 100).toFixed(1)}%`;
      awayWinProb.textContent = `${(Number(data.probabilities.away) * 100).toFixed(1)}%`;
      homeDnbProb.textContent = `${(Number(data.dnb_probabilities.home) * 100).toFixed(1)}%`;
      awayDnbProb.textContent = `${(Number(data.dnb_probabilities.away) * 100).toFixed(1)}%`;
      homeWinOdds.textContent = `Odds ${Number(data.market_odds.home).toFixed(2)}`;
      drawOdds.textContent = `Odds ${Number(data.market_odds.draw).toFixed(2)}`;
      awayWinOdds.textContent = `Odds ${Number(data.market_odds.away).toFixed(2)}`;
      homeDnbOdds.textContent = `Odds ${Number(data.market_dnb_odds.home).toFixed(2)}`;
      awayDnbOdds.textContent = `Odds ${Number(data.market_dnb_odds.away).toFixed(2)}`;
      const historyContext = data.historical_context;
      const historyMeta = historyContext
        ? ` • ${data.history_source} history • ${historyContext.sample_size} completed matches • local sample ${historyContext.local_match_count} • exp goals ${Number(historyContext.expected_home_goals).toFixed(2)}-${Number(historyContext.expected_away_goals).toFixed(2)}`
        : " • rating-only model";
      marketMeta.textContent = `Shin margin ${Number(data.margin_percent).toFixed(2)}% • 1X2 overround ${(Number(data.shin.overround) * 100).toFixed(2)}% • z ${Number(data.shin.z).toFixed(4)}${historyMeta}`;
      homeTeamSelect.disabled = false;
      awayTeamSelect.disabled = false;
      state.loadingComparison = false;
      setStatus(`Comparison ready for ${data.home_team.team} vs ${data.away_team.team}.`);
    }

    countrySelect.addEventListener("change", async (event) => {
      state.selectedCountry = event.target.value;
      state.selectedLeague = "";
      renderTable(homeTable, []);
      renderTable(awayTable, []);
      homeCount.textContent = "0 teams";
      awayCount.textContent = "0 teams";
      metricHome.textContent = "0";
      metricAway.textContent = "0";
      state.currentRatings = null;
      resetComparison();
      updateSummary();

      if (!state.selectedCountry) {
        state.leagues = [];
        renderSelect(leagueSelect, [], "Select a country first", "league", "league_path");
        leagueSelect.disabled = true;
        buildHistoryButton.disabled = true;
        buildHistoryButton.textContent = "Build Cache";
        resetMultiBuilder();
        resetHistoryCacheStatus();
        setStatus("Choose a country to load its leagues.");
        return;
      }

      try {
        await loadLeagues(state.selectedCountry);
      } catch (error) {
        setStatus(error.message);
      }
    });

    leagueSelect.addEventListener("change", (event) => {
      state.selectedLeague = event.target.value;
      state.currentRatings = null;
      resetComparison();
      resetMultiBuilder();
      resetHistoryCacheStatus();
      buildHistoryButton.disabled = !state.selectedLeague;
      buildHistoryButton.textContent = "Build Cache";
      updateSummary();
      if (state.selectedLeague) {
        loadRatings(state.selectedLeague).catch((error) => {
            state.loadingLeagueRatings = false;
            leagueSelect.disabled = false;
          setStatus(error.message);
        });
      } else {
        setStatus("Choose a league to view home and away ratings.");
      }
    });

    homeTeamSelect.addEventListener("change", () => {
      if (homeTeamSelect.value && awayTeamSelect.value) {
        compareTeams().catch((error) => {
          state.loadingComparison = false;
          homeTeamSelect.disabled = false;
          awayTeamSelect.disabled = false;
          setStatus(error.message);
        });
      }
    });

    awayTeamSelect.addEventListener("change", () => {
      if (homeTeamSelect.value && awayTeamSelect.value) {
        compareTeams().catch((error) => {
          state.loadingComparison = false;
          homeTeamSelect.disabled = false;
          awayTeamSelect.disabled = false;
          setStatus(error.message);
        });
      }
    });

    marginInput.addEventListener("input", () => {
      marketMeta.textContent = `Shin margin ${Number(marginInput.value || 0).toFixed(2)}% pending recalculation.`;
      if (homeTeamSelect.value && awayTeamSelect.value) {
        compareTeams().catch((error) => {
          state.loadingComparison = false;
          homeTeamSelect.disabled = false;
          awayTeamSelect.disabled = false;
          setStatus(error.message);
        });
      }
    });

    multiMarginInput.addEventListener("input", () => {
      updateMultiSummary();
      refreshAllMultiRows().catch((error) => {
        setStatus(error.message);
      });
    });

    multiList.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLSelectElement)) {
        return;
      }

      const rowEl = target.closest("[data-row-id]");
      if (!rowEl) {
        return;
      }

      const row = state.multiRows.find((item) => item.id === rowEl.dataset.rowId);
      if (!row) {
        return;
      }

      const field = target.dataset.field;
      if (!field) {
        return;
      }

      row[field] = target.value;
      row.odds = null;
      row.status = "pending";
      updateMultiRow(row.id).catch((error) => {
        row.status = "error";
        setStatus(error.message);
        renderMultiRows();
      });
    });

    for (const button of tabButtons) {
      button.addEventListener("click", () => {
        setActiveTab(button.dataset.tabTarget || "single");
      });
    }

    buildHistoryButton.addEventListener("click", () => {
      buildLeagueHistoryCache(true).catch((error) => {
        buildHistoryButton.disabled = false;
        buildHistoryButton.textContent = "Build Cache";
        setStatus(error.message);
      });
    });

    renderTable(homeTable, []);
    renderTable(awayTable, []);
    resetComparison();
    resetMultiBuilder();
    resetHistoryCacheStatus();
    setActiveTab("single");
    updateSummary();

    loadCountries().catch((error) => {
      setStatus(error.message);
    });
  </script>
</body>
</html>
"""


FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
  <defs>
    <linearGradient id="bg" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#ef233c" />
      <stop offset="100%" stop-color="#0ea5e9" />
    </linearGradient>
  </defs>
  <rect width="64" height="64" rx="16" fill="#0f172a" />
  <rect x="4" y="4" width="56" height="56" rx="13" fill="url(#bg)" />
  <circle cx="32" cy="32" r="16" fill="#ffffff" />
  <path d="M32 20l6 4-2 7h-8l-2-7 6-4zm-9 15h6l2 6-5 4-6-4 3-6zm18 0h6l3 6-6 4-5-4 2-6zm-9 9 5 4-2 6h-6l-2-6 5-4z" fill="#111827" />
</svg>
"""
