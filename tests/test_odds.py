import unittest
from unittest.mock import patch

from soccer_ratings.client import (
    compare_teams_from_ratings,
    dedupe_matches,
    fetch_league_history,
    filter_matches_for_league,
    league_code_from_url,
)
from soccer_ratings.db import import_all_history, import_country_history
from soccer_ratings.odds import (
    apply_shin_margin,
    build_dnb_odds,
    build_match_odds,
    calibrate_probabilities_with_history,
    calculate_dnb_probabilities,
    calculate_match_probabilities,
    summarize_historical_match_context,
)


class OddsModelTests(unittest.TestCase):
    def test_equal_ratings_produce_symmetric_home_and_away_probabilities(self) -> None:
        probabilities = calculate_match_probabilities(2000.0, 2000.0)

        self.assertEqual(probabilities["draw"], 0.3)
        self.assertAlmostEqual(probabilities["home"], probabilities["away"], places=4)

    def test_higher_home_rating_produces_shorter_home_odds(self) -> None:
        odds = build_match_odds(2300.0, 2100.0)

        self.assertLess(odds["home"], odds["away"])
        self.assertGreater(odds["draw"], 0)

    def test_dnb_probabilities_remove_draw_and_normalize(self) -> None:
        dnb_probabilities = calculate_dnb_probabilities(
            {"home": 0.42, "draw": 0.26, "away": 0.32}
        )

        self.assertAlmostEqual(dnb_probabilities["home"] + dnb_probabilities["away"], 1.0, places=4)
        self.assertGreater(dnb_probabilities["home"], dnb_probabilities["away"])

    def test_dnb_odds_favor_stronger_home_team(self) -> None:
        dnb_odds = build_dnb_odds(2300.0, 2100.0)

        self.assertLess(dnb_odds["home"], dnb_odds["away"])

    def test_shin_margin_increases_overround_and_shortens_odds(self) -> None:
        fair_probabilities = {"home": 0.5, "draw": 0.25, "away": 0.25}
        market = apply_shin_margin(fair_probabilities, 6.0)

        self.assertGreater(market["overround"], 1.0)
        self.assertLess(market["odds"]["home"], 2.0)
        self.assertGreater(market["z"], 0.0)


class TeamComparisonTests(unittest.TestCase):
    def test_compare_teams_uses_home_and_away_pools(self) -> None:
        home_rows = [
            {"team": "Arsenal", "rating": 2500.0, "rank": 1},
            {"team": "Liverpool", "rating": 2450.0, "rank": 2},
        ]
        away_rows = [
            {"team": "Chelsea", "rating": 2200.0, "rank": 5},
            {"team": "Liverpool", "rating": 2300.0, "rank": 2},
        ]

        comparison = compare_teams_from_ratings(
            home_rows,
            away_rows,
            home_team="Arsenal",
            away_team="Liverpool",
            margin_percent=5.0,
        )

        self.assertEqual(comparison["home_team"]["team"], "Arsenal")
        self.assertEqual(comparison["away_team"]["team"], "Liverpool")
        self.assertEqual(comparison["rating_gap"], 200.0)
        self.assertLess(comparison["odds"]["home"], comparison["odds"]["away"])
        self.assertLess(comparison["dnb_odds"]["home"], comparison["dnb_odds"]["away"])
        self.assertLess(comparison["market_odds"]["home"], comparison["odds"]["home"])
        self.assertLess(comparison["market_dnb_odds"]["home"], comparison["dnb_odds"]["home"])

    def test_compare_teams_uses_historical_context_when_available(self) -> None:
        home_rows = [{"team": "Alpha", "rating": 2100.0, "rank": 1}]
        away_rows = [{"team": "Beta", "rating": 2000.0, "rank": 2}]
        historical_matches = [
            {
                "home_rating": 2095.0,
                "away_rating": 1995.0,
                "home_goals": 1,
                "away_goals": 1,
            },
            {
                "home_rating": 2105.0,
                "away_rating": 2005.0,
                "home_goals": 0,
                "away_goals": 0,
            },
            {
                "home_rating": 2110.0,
                "away_rating": 2010.0,
                "home_goals": 2,
                "away_goals": 1,
            },
        ]

        comparison = compare_teams_from_ratings(
            home_rows,
            away_rows,
            home_team="Alpha",
            away_team="Beta",
            historical_matches=historical_matches,
        )

        self.assertEqual(comparison["model"], "history-calibrated")
        self.assertIsNotNone(comparison["historical_context"])
        self.assertGreater(comparison["probabilities"]["draw"], comparison["base_probabilities"]["draw"])


class MatchDeduplicationTests(unittest.TestCase):
    def test_dedupe_matches_keeps_one_copy_per_match_identity(self) -> None:
        matches = [
            {
                "date": "22.04.26",
                "competition": "BA1",
                "home_team": "Zrinjski Mostar",
                "away_team": "Borac Banja Luka",
                "result": "1:1",
            },
            {
                "date": "22.04.26",
                "competition": "BA1",
                "home_team": "Zrinjski Mostar",
                "away_team": "Borac Banja Luka",
                "result": "1:1",
            },
            {
                "date": "21.04.26",
                "competition": "BA1",
                "home_team": "Another Team",
                "away_team": "Borac Banja Luka",
                "result": "0:1",
            },
        ]

        deduped = dedupe_matches(matches)

        self.assertEqual(len(deduped), 2)

    def test_filter_matches_for_league_keeps_only_matching_competition(self) -> None:
        matches = [
            {"competition": "UK1", "home_team": "A", "away_team": "B"},
            {"competition": "UKFACUP", "home_team": "A", "away_team": "B"},
        ]

        filtered = filter_matches_for_league(matches, "/England/UK1/")

        self.assertEqual(filtered, [{"competition": "UK1", "home_team": "A", "away_team": "B"}])

    @patch("soccer_ratings.client.discover_league_mode_urls")
    def test_league_code_from_top_flight_country_url_uses_special_link_code(self, mock_discover) -> None:
        mock_discover.return_value = {
            "general": "https://www.soccer-rating.com/England/",
            "home": "https://www.soccer-rating.com/England/UK1/home/",
            "away": "https://www.soccer-rating.com/England/UK1/away/",
        }

        self.assertEqual(league_code_from_url("/England/"), "UK1")


class HistoricalCalibrationTests(unittest.TestCase):
    def test_summarize_historical_match_context_returns_goal_and_draw_features(self) -> None:
        context = summarize_historical_match_context(
            [
                {"home_rating": 2100.0, "away_rating": 2000.0, "home_goals": 1, "away_goals": 1},
                {"home_rating": 2120.0, "away_rating": 2020.0, "home_goals": 2, "away_goals": 0},
                {"home_rating": 2080.0, "away_rating": 1980.0, "home_goals": 0, "away_goals": 0},
            ],
            target_rating_gap=100.0,
        )

        self.assertIsNotNone(context)
        self.assertEqual(context["sample_size"], 3)
        self.assertGreater(context["draw_rate"], 0.0)
        self.assertGreater(context["expected_total_goals"], 0.0)

    def test_calibrate_probabilities_with_history_raises_draw_probability_when_history_is_draw_heavy(self) -> None:
        base_probabilities = {"home": 0.5, "draw": 0.22, "away": 0.28}
        historical_context = {
            "effective_sample_size": 18.0,
            "draw_rate": 0.4,
            "home_share_non_draw": 0.55,
        }

        calibrated = calibrate_probabilities_with_history(base_probabilities, historical_context)

        self.assertGreater(calibrated["draw"], base_probabilities["draw"])
        self.assertAlmostEqual(
            calibrated["home"] + calibrated["draw"] + calibrated["away"],
            1.0,
            places=3,
        )


class ResilienceTests(unittest.TestCase):
    @patch("soccer_ratings.client.fetch_team_history")
    @patch("soccer_ratings.client.fetch_league_ratings")
    def test_fetch_league_history_skips_failed_team_pages(self, mock_fetch_league_ratings, mock_fetch_team_history) -> None:
        mock_fetch_league_ratings.return_value = [
            {"team": "Alpha", "team_path": "/Alpha/", "rating": 2000.0, "rank": 1},
            {"team": "Beta", "team_path": "/Beta/", "rating": 1900.0, "rank": 2},
        ]
        mock_fetch_team_history.side_effect = [
            [{"date": "01.01.26", "competition": "UK1", "home_team": "Alpha", "away_team": "Gamma"}],
            RuntimeError("HTTP 500"),
        ]

        payload = fetch_league_history("/England/UK1/")

        self.assertEqual(payload["team_count"], 1)
        self.assertEqual(len(payload["failed_teams"]), 1)
        self.assertEqual(payload["failed_teams"][0]["team"], "Beta")

    @patch("soccer_ratings.db.fetch_country_leagues")
    @patch("soccer_ratings.db.import_league_history")
    def test_import_country_history_collects_failures_without_aborting(self, mock_import_league_history, mock_fetch_country_leagues) -> None:
        mock_fetch_country_leagues.return_value = [
            {"league": "Premier League", "league_path": "/England/"},
            {"league": "Championship", "league_path": "/England/UK2/"},
        ]
        mock_import_league_history.side_effect = [
            {"league_url": "/England/", "matches_imported": 20, "deduped_match_count": 10},
            RuntimeError("HTTP 500"),
        ]

        payload = import_country_history("/England/")

        self.assertEqual(payload["leagues_processed"], 1)
        self.assertEqual(payload["failure_count"], 1)
        self.assertEqual(payload["failures"][0]["league"], "Championship")

    @patch("soccer_ratings.db.fetch_rankings")
    @patch("soccer_ratings.db.import_country_history")
    def test_import_all_history_collects_country_failures_without_aborting(self, mock_import_country_history, mock_fetch_rankings) -> None:
        mock_fetch_rankings.return_value = [
            {"country": "England", "country_path": "/England/"},
            {"country": "Spain", "country_path": "/Spain/"},
        ]
        mock_import_country_history.side_effect = [
            {"country_url": "/England/", "leagues_processed": 2, "matches_imported": 40, "deduped_match_count": 20},
            RuntimeError("HTTP 500"),
        ]

        payload = import_all_history()

        self.assertEqual(payload["countries_processed"], 1)
        self.assertEqual(payload["failure_count"], 1)
        self.assertEqual(payload["failures"][0]["country"], "Spain")
