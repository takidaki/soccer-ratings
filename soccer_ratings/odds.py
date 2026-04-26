from __future__ import annotations

import math


def probability_to_decimal_odds(probability: float) -> float:
    if probability <= 0:
        return 0.0
    return round(1.0 / probability, 2)


def calculate_match_probabilities(home_rating: float, away_rating: float) -> dict[str, float]:
    """Convert a rating gap into 1X2 probabilities.

    Assumptions:
    - Home team's home rating is compared against away team's away rating.
    - The win split uses an Elo-style logistic curve.
    - Draw probability is highest when teams are evenly matched and shrinks as the gap grows.
    """

    rating_gap = home_rating - away_rating
    win_share = 1.0 / (1.0 + math.pow(10.0, -rating_gap / 400.0))
    draw_probability = 0.30 * math.exp(-abs(rating_gap) / 500.0)
    draw_probability = min(0.30, max(0.18, draw_probability))

    remaining = 1.0 - draw_probability
    home_probability = remaining * win_share
    away_probability = remaining * (1.0 - win_share)

    return {
        "home": round(home_probability, 4),
        "draw": round(draw_probability, 4),
        "away": round(away_probability, 4),
    }


def build_odds_from_probabilities(probabilities: dict[str, float]) -> dict[str, float]:
    return {
        key: probability_to_decimal_odds(value) for key, value in probabilities.items()
    }


def build_match_odds(home_rating: float, away_rating: float) -> dict[str, float]:
    probabilities = calculate_match_probabilities(home_rating, away_rating)
    return build_odds_from_probabilities(probabilities)


def calculate_dnb_probabilities(probabilities: dict[str, float]) -> dict[str, float]:
    non_draw_probability = probabilities["home"] + probabilities["away"]
    if non_draw_probability <= 0:
        return {"home": 0.0, "away": 0.0}

    return {
        "home": round(probabilities["home"] / non_draw_probability, 4),
        "away": round(probabilities["away"] / non_draw_probability, 4),
    }


def build_dnb_odds(home_rating: float, away_rating: float) -> dict[str, float]:
    dnb_probabilities = calculate_dnb_probabilities(
        calculate_match_probabilities(home_rating, away_rating)
    )
    return build_odds_from_probabilities(dnb_probabilities)


def summarize_historical_match_context(
    historical_matches: list[dict],
    target_rating_gap: float,
    bandwidth: float = 120.0,
) -> dict[str, float] | None:
    completed_matches = [
        match
        for match in historical_matches
        if match.get("home_goals") is not None
        and match.get("away_goals") is not None
        and match.get("home_rating") is not None
        and match.get("away_rating") is not None
    ]
    if not completed_matches:
        return None

    weighted_draws = 0.0
    weighted_home_non_draw = 0.0
    weighted_non_draw = 0.0
    weighted_home_goals = 0.0
    weighted_away_goals = 0.0
    weighted_total = 0.0
    local_match_count = 0
    overall_draws = 0

    for match in completed_matches:
        home_goals = int(match["home_goals"])
        away_goals = int(match["away_goals"])
        rating_gap = float(match["home_rating"]) - float(match["away_rating"])
        weight = math.exp(-abs(rating_gap - target_rating_gap) / bandwidth)

        if abs(rating_gap - target_rating_gap) <= bandwidth:
            local_match_count += 1

        if home_goals == away_goals:
            weighted_draws += weight
            overall_draws += 1
        else:
            weighted_non_draw += weight
            if home_goals > away_goals:
                weighted_home_non_draw += weight

        weighted_home_goals += weight * home_goals
        weighted_away_goals += weight * away_goals
        weighted_total += weight

    if weighted_total <= 0:
        return None

    effective_sample_size = min(len(completed_matches), round(weighted_total, 2))
    draw_rate = weighted_draws / weighted_total
    home_share_non_draw = (
        weighted_home_non_draw / weighted_non_draw if weighted_non_draw > 0 else 0.5
    )

    return {
        "sample_size": len(completed_matches),
        "effective_sample_size": effective_sample_size,
        "local_match_count": local_match_count,
        "draw_rate": round(draw_rate, 4),
        "league_draw_rate": round(overall_draws / len(completed_matches), 4),
        "home_share_non_draw": round(home_share_non_draw, 4),
        "expected_home_goals": round(weighted_home_goals / weighted_total, 3),
        "expected_away_goals": round(weighted_away_goals / weighted_total, 3),
        "expected_total_goals": round((weighted_home_goals + weighted_away_goals) / weighted_total, 3),
    }


def calibrate_probabilities_with_history(
    probabilities: dict[str, float],
    historical_context: dict[str, float] | None,
) -> dict[str, float]:
    if not historical_context:
        return {key: round(value, 4) for key, value in probabilities.items()}

    effective_sample_size = float(historical_context.get("effective_sample_size", 0.0))
    if effective_sample_size <= 0:
        return {key: round(value, 4) for key, value in probabilities.items()}

    draw_weight = min(0.4, effective_sample_size / 24.0 * 0.4)
    win_share_weight = min(0.28, effective_sample_size / 24.0 * 0.28)

    draw_probability = _blend(
        probabilities["draw"],
        float(historical_context["draw_rate"]),
        draw_weight,
    )
    non_draw_probability = max(0.0, 1.0 - draw_probability)

    base_non_draw_probability = probabilities["home"] + probabilities["away"]
    if base_non_draw_probability <= 0:
        base_home_share = 0.5
    else:
        base_home_share = probabilities["home"] / base_non_draw_probability

    calibrated_home_share = _blend(
        base_home_share,
        float(historical_context["home_share_non_draw"]),
        win_share_weight,
    )
    calibrated_home_share = min(0.95, max(0.05, calibrated_home_share))

    home_probability = non_draw_probability * calibrated_home_share
    away_probability = non_draw_probability - home_probability

    return {
        "home": round(home_probability, 4),
        "draw": round(draw_probability, 4),
        "away": round(max(0.0, away_probability), 4),
    }


def apply_shin_margin(probabilities: dict[str, float], margin_percent: float) -> dict[str, object]:
    target_overround = 1.0 + max(0.0, margin_percent) / 100.0
    if target_overround <= 1.0:
        return {
            "probabilities": {key: round(value, 4) for key, value in probabilities.items()},
            "odds": {key: probability_to_decimal_odds(value) for key, value in probabilities.items()},
            "z": 0.0,
            "overround": 1.0,
        }

    max_overround = _calculate_shin_overround(probabilities, 0.999999)
    z = 0.999999 if target_overround >= max_overround else _solve_shin_z(probabilities, target_overround)
    adjusted_probabilities = _calculate_shin_book_probabilities(probabilities, z)

    return {
        "probabilities": adjusted_probabilities,
        "odds": {key: probability_to_decimal_odds(value) for key, value in adjusted_probabilities.items()},
        "z": round(z, 6),
        "overround": round(sum(adjusted_probabilities.values()), 6),
    }


def _blend(base_value: float, target_value: float, weight: float) -> float:
    bounded_weight = min(1.0, max(0.0, weight))
    return (base_value * (1.0 - bounded_weight)) + (target_value * bounded_weight)


def _calculate_shin_book_probabilities(probabilities: dict[str, float], z: float) -> dict[str, float]:
    return {
        key: round(value, 4)
        for key, value in _calculate_shin_book_probabilities_raw(probabilities, z).items()
    }


def _calculate_shin_book_probabilities_raw(probabilities: dict[str, float], z: float) -> dict[str, float]:
    weights = {
        key: math.sqrt(value * (z + (1.0 - z) * value))
        for key, value in probabilities.items()
    }
    scale = sum(weights.values())
    if scale <= 0:
        return {key: 0.0 for key in probabilities}
    return {key: scale * weight for key, weight in weights.items()}


def _calculate_shin_overround(probabilities: dict[str, float], z: float) -> float:
    return sum(_calculate_shin_book_probabilities_raw(probabilities, z).values())


def _solve_shin_z(probabilities: dict[str, float], target_overround: float) -> float:
    low = 0.0
    high = 0.999999
    for _ in range(60):
        mid = (low + high) / 2.0
        overround = _calculate_shin_overround(probabilities, mid)
        if overround < target_overround:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0
