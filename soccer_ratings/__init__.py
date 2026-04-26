"""Soccer country, league, and team rating scraper."""

from .client import (
    build_and_cache_league_history,
    compare_teams_in_league,
    fetch_league_history,
    fetch_all_country_league_ratings,
    fetch_country_leagues,
    fetch_country_league_ratings,
    fetch_league_home_away_ratings,
    fetch_league_ratings,
    fetch_rankings,
    fetch_team_history,
    load_cached_league_history,
)
from .db import (
    import_all_history,
    import_country_rankings,
    import_country_history,
    import_league_history,
    import_league_ratings,
    init_db,
)
from .dashboard import run_dashboard

__all__ = [
    "build_and_cache_league_history",
    "compare_teams_in_league",
    "fetch_league_history",
    "fetch_all_country_league_ratings",
    "fetch_country_leagues",
    "fetch_country_league_ratings",
    "fetch_league_home_away_ratings",
    "fetch_league_ratings",
    "fetch_rankings",
    "fetch_team_history",
    "import_all_history",
    "import_country_rankings",
    "import_country_history",
    "import_league_history",
    "import_league_ratings",
    "init_db",
    "load_cached_league_history",
    "run_dashboard",
]
