from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from urllib.parse import urljoin
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .odds import (
    apply_shin_margin,
    build_dnb_odds,
    build_odds_from_probabilities,
    build_match_odds,
    calibrate_probabilities_with_history,
    calculate_dnb_probabilities,
    calculate_match_probabilities,
    summarize_historical_match_context,
)
from .parser import (
    parse_leagues,
    parse_rankings,
    parse_special_rating_links,
    parse_team_history_matches,
    parse_team_ratings,
)

DEFAULT_URL = "https://www.soccer-rating.com/football-country-ranking/"
BASE_URL = "https://www.soccer-rating.com"
CACHE_DIR = Path(__file__).resolve().parent.parent / ".cache" / "league-history"


def fetch_html(
    url: str = DEFAULT_URL,
    timeout: int = 30,
    retries: int = 3,
    retry_delay: float = 1.0,
) -> str:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/123.0.0.0 Safari/537.36"
            )
        },
    )
    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with urlopen(request, timeout=timeout) as response:
                encoding = response.headers.get_content_charset() or "utf-8"
                return response.read().decode(encoding, errors="replace")
        except HTTPError as exc:
            last_error = exc
            if exc.code not in {429, 500, 502, 503, 504} or attempt >= retries:
                raise
        except URLError as exc:
            last_error = exc
            if attempt >= retries:
                raise

        time.sleep(retry_delay * (attempt + 1))

    if last_error is not None:
        raise last_error
    raise RuntimeError(f"Failed to fetch HTML from {url}")


def fetch_rankings(url: str = DEFAULT_URL) -> list[dict]:
    html = fetch_html(url=url)
    return [row.to_dict() for row in parse_rankings(html)]


def fetch_country_leagues(country_url: str) -> list[dict]:
    html = fetch_html(url=resolve_url(country_url))
    return [row.to_dict() for row in parse_leagues(html)]


def fetch_league_ratings(league_url: str, mode: str = "general") -> list[dict]:
    normalized_url = discover_league_mode_url(league_url, mode)
    html = fetch_html(url=normalized_url)
    return [row.to_dict() for row in parse_team_ratings(html, mode=mode)]


def fetch_league_home_away_ratings(league_url: str) -> dict:
    mode_urls = discover_league_mode_urls(league_url)
    home_rows = parse_team_ratings(fetch_html(mode_urls["home"]), mode="home")
    away_rows = parse_team_ratings(fetch_html(mode_urls["away"]), mode="away")
    return {
        "league_url": mode_urls["general"],
        "home_url": mode_urls["home"],
        "away_url": mode_urls["away"],
        "home": [row.to_dict() for row in home_rows],
        "away": [row.to_dict() for row in away_rows],
    }


def fetch_team_history(team_url: str, focal_team: str | None = None) -> list[dict]:
    resolved_team_url = resolve_url(team_url)
    html = fetch_html(url=resolved_team_url)
    return [
        row.to_dict()
        for row in parse_team_history_matches(
            html,
            focal_team=focal_team,
            source_team_path=resolved_team_url,
        )
    ]


def fetch_league_history(league_url: str) -> dict:
    teams = fetch_league_ratings(league_url, mode="general")
    collected_matches: list[dict] = []
    team_sources: list[dict] = []
    failed_teams: list[dict] = []

    for team in teams:
        team_path = team.get("team_path")
        team_name = team.get("team")
        if not team_path or not team_name:
            continue

        try:
            team_history = fetch_team_history(team_path, focal_team=team_name)
        except Exception as exc:
            failed_teams.append(
                {
                    "team": team_name,
                    "team_path": team_path,
                    "error": str(exc),
                }
            )
            continue

        collected_matches.extend(team_history)
        team_sources.append(
            {
                "team": team_name,
                "team_path": team_path,
                "matches_collected": len(team_history),
            }
        )

    league_matches = filter_matches_for_league(collected_matches, league_url)
    deduped_matches = dedupe_matches(league_matches)
    return {
        "league_url": resolve_url(league_url),
        "team_count": len(team_sources),
        "raw_match_count": len(league_matches),
        "deduped_match_count": len(deduped_matches),
        "teams": team_sources,
        "failed_teams": failed_teams,
        "matches": deduped_matches,
    }


def get_league_history_cache_path(league_url: str) -> Path:
    resolved = resolve_url(league_url)
    digest = hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:16]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return CACHE_DIR / f"{digest}.json"


def load_cached_league_history(league_url: str) -> dict | None:
    cache_path = get_league_history_cache_path(league_url)
    if not cache_path.exists():
        return None

    return json.loads(cache_path.read_text(encoding="utf-8"))


def build_and_cache_league_history(league_url: str, force_refresh: bool = False) -> dict:
    if not force_refresh:
        cached = load_cached_league_history(league_url)
        if cached is not None:
            return cached

    payload = fetch_league_history(league_url)
    cache_path = get_league_history_cache_path(league_url)
    payload = {
        **payload,
        "cache_path": str(cache_path),
        "cached": True,
    }
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return payload


def fetch_country_league_ratings(
    country_url: str,
    include_general: bool = False,
) -> dict:
    country_full_url = resolve_url(country_url)
    leagues = fetch_country_leagues(country_full_url)
    modes = ["home", "away"]
    if include_general:
        modes.insert(0, "general")

    detailed_leagues = []
    for league in leagues:
        league_path = league.get("league_path")
        if not league_path:
            continue

        ratings_by_mode = {
            mode: fetch_league_ratings(league_path, mode=mode) for mode in modes
        }
        detailed_leagues.append({**league, "ratings": ratings_by_mode})

    return {"country_url": country_full_url, "leagues": detailed_leagues}


def fetch_all_country_league_ratings(include_general: bool = False) -> list[dict]:
    countries = fetch_rankings()
    results = []
    for country in countries:
        country_path = country.get("country_path")
        if not country_path:
            continue

        results.append(
            {
                **country,
                "league_ratings": fetch_country_league_ratings(
                    country_path,
                    include_general=include_general,
                )["leagues"],
            }
        )
    return results


def rankings_to_json(url: str = DEFAULT_URL) -> str:
    return json.dumps(fetch_rankings(url=url), indent=2, ensure_ascii=False)


def resolve_url(url_or_path: str) -> str:
    return urljoin(BASE_URL, url_or_path)


def build_league_mode_url(league_url: str, mode: str) -> str:
    normalized_mode = mode.lower()
    if normalized_mode not in {"general", "home", "away"}:
        raise ValueError("mode must be one of: general, home, away")

    absolute_url = resolve_url(league_url)
    if normalized_mode == "general":
        return absolute_url if absolute_url.endswith("/") else absolute_url + "/"

    trimmed = absolute_url.rstrip("/")
    return f"{trimmed}/{normalized_mode}/"


def discover_league_mode_urls(league_url: str) -> dict[str, str]:
    general_url = build_league_mode_url(league_url, "general")
    html = fetch_html(general_url)
    special_links = parse_special_rating_links(html)

    return {
        "general": resolve_url(special_links.general or general_url),
        "home": resolve_url(special_links.home or build_league_mode_url(general_url, "home")),
        "away": resolve_url(special_links.away or build_league_mode_url(general_url, "away")),
    }


def discover_league_mode_url(league_url: str, mode: str) -> str:
    return discover_league_mode_urls(league_url)[mode.lower()]


def compare_teams_from_ratings(
    home_ratings: list[dict],
    away_ratings: list[dict],
    home_team: str,
    away_team: str,
    margin_percent: float = 0.0,
    historical_matches: list[dict] | None = None,
) -> dict:
    home_entry = _find_team_rating(home_ratings, home_team)
    away_entry = _find_team_rating(away_ratings, away_team)

    home_rating = float(home_entry["rating"])
    away_rating = float(away_entry["rating"])
    base_probabilities = calculate_match_probabilities(home_rating, away_rating)
    historical_context = summarize_historical_match_context(
        historical_matches or [],
        target_rating_gap=home_rating - away_rating,
    )
    probabilities = calibrate_probabilities_with_history(base_probabilities, historical_context)
    dnb_probabilities = calculate_dnb_probabilities(probabilities)
    odds = build_odds_from_probabilities(probabilities)
    dnb_odds = build_odds_from_probabilities(dnb_probabilities)
    market = apply_shin_margin(probabilities, margin_percent)
    dnb_market = apply_shin_margin(dnb_probabilities, margin_percent)

    return {
        "home_team": home_entry,
        "away_team": away_entry,
        "rating_gap": round(home_rating - away_rating, 2),
        "margin_percent": round(max(0.0, margin_percent), 2),
        "model": "history-calibrated" if historical_context else "ratings-only",
        "base_probabilities": base_probabilities,
        "base_odds": build_match_odds(home_rating, away_rating),
        "probabilities": probabilities,
        "odds": odds,
        "dnb_probabilities": dnb_probabilities,
        "dnb_odds": dnb_odds,
        "historical_context": historical_context,
        "market_probabilities": market["probabilities"],
        "market_odds": market["odds"],
        "market_dnb_probabilities": dnb_market["probabilities"],
        "market_dnb_odds": dnb_market["odds"],
        "shin": {
            "z": market["z"],
            "overround": market["overround"],
            "dnb_z": dnb_market["z"],
            "dnb_overround": dnb_market["overround"],
        },
    }


def compare_teams_in_league(
    league_url: str,
    home_team: str,
    away_team: str,
    margin_percent: float = 0.0,
) -> dict:
    ratings = fetch_league_home_away_ratings(league_url)
    comparison = compare_teams_from_ratings(
        ratings["home"],
        ratings["away"],
        home_team=home_team,
        away_team=away_team,
        margin_percent=margin_percent,
    )
    return {**ratings, "comparison": comparison}


def dedupe_matches(matches: list[dict]) -> list[dict]:
    deduped: dict[tuple[str, str, str, str], dict] = {}
    for match in matches:
        key = (
            str(match.get("date", "")).strip(),
            str(match.get("competition", "")).strip(),
            str(match.get("home_team", "")).strip(),
            str(match.get("away_team", "")).strip(),
        )
        if key not in deduped:
            deduped[key] = match

    return sorted(
        deduped.values(),
        key=lambda row: (
            str(row.get("date", "")),
            str(row.get("competition", "")),
            str(row.get("home_team", "")),
            str(row.get("away_team", "")),
        ),
        reverse=True,
    )


def filter_matches_for_league(matches: list[dict], league_url: str) -> list[dict]:
    competition_code = league_code_from_url(league_url)
    if not competition_code:
        return matches

    normalized_code = competition_code.upper()
    return [
        match
        for match in matches
        if str(match.get("competition", "")).strip().upper() == normalized_code
    ]


def league_code_from_url(league_url: str) -> str:
    path = resolve_url(league_url).replace(BASE_URL, "").strip("/")
    if not path:
        return ""

    segments = [segment for segment in path.split("/") if segment]
    if len(segments) >= 2:
        return segments[1]

    try:
        mode_urls = discover_league_mode_urls(league_url)
    except Exception:
        return segments[-1]

    home_path = mode_urls["home"].replace(BASE_URL, "").strip("/")
    home_segments = [segment for segment in home_path.split("/") if segment]
    if len(home_segments) >= 2:
        return home_segments[1]
    return segments[-1]


def _find_team_rating(rows: list[dict], team_name: str) -> dict:
    normalized_name = team_name.strip().casefold()
    for row in rows:
        if str(row.get("team", "")).strip().casefold() == normalized_name:
            return row
    raise ValueError(f"Team not found: {team_name}")
