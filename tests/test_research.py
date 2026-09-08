"""Player briefs: parsing, sampling, and the gate on the numeric estimate."""

from __future__ import annotations

import json
import types

import pytest

from dfs.research.player_brief import (
    MAX_STDEV_FOR_MULTIPLIER,
    PlayerBriefError,
    _extract_json,
    _parse_brief,
    aggregate_samples,
    apply_briefs_to_pool,
    brief_player,
)

PLAYER = {"player_id": "p1", "name": "Player One", "team": "AAA", "opponent": "BBB"}


def _brief(status="Active", multiplier=1.0, opportunity=30.0, confidence=70):
    estimate = (
        None
        if multiplier is None
        else {
            "multiplier": multiplier,
            "opportunity": opportunity,
            "confidence": confidence,
            "reasoning": "because",
        }
    )
    return _parse_brief(
        {
            "status": status,
            "situation": "what the reports say",
            "role_change": "starting",
            "what_would_settle_it": ["the official inactives list"],
            "estimate": estimate,
        },
        PLAYER,
        "2026-09-01",
        "test-model",
    )


def test_bare_json_is_parsed():
    assert _extract_json('{"status": "Active"}')["status"] == "Active"


def test_json_after_narration_is_parsed():
    text = 'I searched three sources and found this.\n\n{"status": "Out"}'
    assert _extract_json(text)["status"] == "Out"


def test_fenced_json_is_parsed():
    text = '```json\n{"status": "Questionable"}\n```'
    assert _extract_json(text)["status"] == "Questionable"


def test_nested_objects_do_not_confuse_the_parser():
    text = '{"status": "Active", "estimate": {"multiplier": 1.0}}'
    assert _extract_json(text)["estimate"]["multiplier"] == 1.0


def test_a_response_without_json_raises():
    with pytest.raises(PlayerBriefError, match="No JSON"):
        _extract_json("I could not find anything about this player.")


def test_an_unknown_status_falls_back_rather_than_raising():
    assert _brief(status="Probably Fine").status == "Unknown"


def test_multipliers_are_clamped_to_the_allowed_range():
    assert _brief(multiplier=5.0).multiplier == 2.0
    assert _brief(multiplier=-1.0).multiplier == 0.0


def test_agreeing_samples_keep_the_median():
    result = aggregate_samples([_brief(multiplier=m) for m in (1.0, 1.1, 1.05)])
    assert result.multiplier == pytest.approx(1.05)
    assert result.samples == 3


def test_a_single_outlier_is_rejected_by_the_median():
    result = aggregate_samples([_brief(multiplier=m) for m in (1.0, 1.02, 1.05)])
    assert result.multiplier == pytest.approx(1.02)


def test_disagreeing_samples_keep_the_brief_and_drop_the_number():
    """The contract inherited from the prediction-market project."""

    result = aggregate_samples(
        [_brief(status="Questionable", multiplier=m) for m in (0.2, 1.4, 0.9)]
    )
    assert result.multiplier is None
    assert result.situation
    assert result.role_change
    assert result.multiplier_stdev > MAX_STDEV_FOR_MULTIPLIER


def test_a_minority_of_estimates_is_not_enough():
    result = aggregate_samples(
        [_brief(multiplier=None), _brief(multiplier=None), _brief(multiplier=1.0)]
    )
    assert result.multiplier is None


def test_a_majority_of_estimates_is_enough():
    result = aggregate_samples(
        [_brief(multiplier=1.0), _brief(multiplier=1.05), _brief(multiplier=None)]
    )
    assert result.multiplier is not None


def test_no_estimates_at_all_still_returns_a_brief():
    result = aggregate_samples([_brief(multiplier=None)] * 3)
    assert result.multiplier is None
    assert result.situation


def test_status_ties_resolve_pessimistically():
    """Being wrong that a player is out costs one roster spot; the reverse
    costs the whole lineup."""

    result = aggregate_samples([_brief(status="Out", multiplier=0.0),
                                _brief(status="Active", multiplier=1.0)])
    assert result.status == "Out"
    assert result.is_excluded


def test_aggregating_nothing_raises():
    with pytest.raises(PlayerBriefError):
        aggregate_samples([])


def test_an_excluded_player_is_zeroed_out_of_the_pool():
    pool = [{"player_id": "p1", "projected_points": 20.0, "ceiling": 30.0,
             "floor": 12.0, "stdev": 6.0}]
    brief = _brief(status="Out", multiplier=0.0)
    apply_briefs_to_pool(pool, [brief])

    assert pool[0]["projected_points"] == 0.0
    assert pool[0]["ceiling"] == 0.0


def test_a_multiplier_scales_every_projection_field():
    pool = [{"player_id": "p1", "projected_points": 20.0, "ceiling": 30.0,
             "floor": 12.0, "stdev": 6.0}]
    apply_briefs_to_pool(pool, [_brief(status="Confirmed Starter", multiplier=1.5)])

    assert pool[0]["projected_points"] == 30.0
    assert pool[0]["ceiling"] == 45.0
    assert pool[0]["stdev"] == 9.0


def test_a_gated_multiplier_leaves_the_projection_alone():
    """An unreproducible number must not quietly move a projection."""

    pool = [{"player_id": "p1", "projected_points": 20.0, "ceiling": 30.0}]
    gated = aggregate_samples([_brief(multiplier=m) for m in (0.2, 1.4, 0.9)])
    apply_briefs_to_pool(pool, [gated])

    assert pool[0]["projected_points"] == 20.0
    assert pool[0]["research_status"]


def test_players_without_a_brief_are_untouched():
    pool = [{"player_id": "other", "projected_points": 20.0}]
    apply_briefs_to_pool(pool, [_brief()])
    assert pool[0]["projected_points"] == 20.0


class _StubClient:
    """Stands in for the Anthropic client, returning scripted multipliers."""

    def __init__(self, multipliers):
        self.multipliers = list(multipliers)
        self.calls = 0
        self.messages = self

    def create(self, **_kwargs):
        multiplier = self.multipliers[min(self.calls, len(self.multipliers) - 1)]
        self.calls += 1
        payload = {
            "status": "Questionable",
            "situation": "s",
            "role_change": "r",
            "what_would_settle_it": ["report"],
            "estimate": {
                "multiplier": multiplier, "opportunity": 28,
                "confidence": 60, "reasoning": "r",
            },
        }
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=json.dumps(payload))]
        )


def test_the_default_sample_count_is_drawn():
    client = _StubClient([1.0, 1.02, 1.05])
    result = brief_player(PLAYER, "2026-09-01", "NBA", client=client)

    assert client.calls == 3
    assert result.samples == 3


def test_a_borderline_player_earns_extra_samples():
    """Only players near the gate are worth more money."""

    borderline = [0.85, 1.15, 1.20, 1.0, 1.1]
    client = _StubClient(borderline)
    brief_player(PLAYER, "2026-09-01", "NBA", client=client)
    assert client.calls > 3
