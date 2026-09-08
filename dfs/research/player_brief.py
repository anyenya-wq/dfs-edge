"""Claude-researched status and role reads for individual players.

This carries over the contract the prediction-market project arrived at
after its estimate-or-refuse design kept throwing away work: the brief
and the number are separate deliverables. Describing what the reporting
establishes about a player, what remains unresolved, and what would
settle it is useful at any confidence level and never requires refusing.
The numeric multiplier stays strictly gated and is simply omitted when
the evidence does not support one, so the discipline is preserved
exactly where it matters -- the calibration record -- without discarding
the written read that led to declining.

What it is for is narrow and specific. Daily fantasy's most reliable
edge is the gap between a late status change and the salary that has not
moved: a starter ruled out at 5:30 hands his minutes to a backup priced
as a backup. Confirmed lineups land roughly an hour before lock in NBA
and soccer, inactives ninety minutes before an NFL kickoff, and
confirmed goalies at variable times before puck drop. That window is
what this reads.

So run it on the handful of players whose status is genuinely in doubt,
not on a whole slate. Each player costs several web searches times the
sample count, and a hundred-player pool of mostly-certain statuses would
be almost entirely wasted money.

Repeated sampling is the same fix for the same problem. A single draw on
an ambiguous injury report is not reproducible -- two runs an hour apart
disagreed enough in the prediction-market case to flip a logged label --
so each player is briefed several times and the median multiplier is
what gets used. When the samples disagree beyond the gate, the brief is
kept and the multiplier is dropped, because disagreement about whether a
player will play is itself the finding.
"""

from __future__ import annotations

import dataclasses
import json
import os
import statistics
from typing import Any

DEFAULT_MODEL = "claude-opus-5"

# Claude runs its own searches. Several are usually needed: the beat
# reporter's latest, the team's official injury report, and often a
# confirmation that the report is today's rather than last week's.
MAX_SEARCHES_PER_PLAYER = 5

WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search",
    "max_uses": MAX_SEARCHES_PER_PLAYER,
}

# Three draws is the smallest count yielding a median with an outlier to
# reject; five is the ceiling for a genuinely undecided player.
SAMPLES_PER_PLAYER = 3
MAX_SAMPLES_PER_PLAYER = 5

# Dispersion above which a multiplier is not reproducible enough to use.
# On a 0-2 scale, 0.20 means the samples disagree by roughly a fifth of
# a player's output -- past that the disagreement is the signal, and
# averaging it into a confident number would be worse than having none.
MAX_STDEV_FOR_MULTIPLIER = 0.20

# How close to the gate counts as undecided and earns extra samples.
ESCALATION_BAND = 0.05

VALID_STATUSES = {
    "Confirmed Starter",
    "Active",
    "Probable",
    "Questionable",
    "Game-Time Decision",
    "Doubtful",
    "Out",
    "Rotation Risk",
    "Unknown",
}

# Statuses that make a player unrosterable regardless of the multiplier.
# Kept explicit because "Out" arriving as a 0.0 multiplier and "Out"
# arriving as a status are different failure modes, and the second is
# the one that must survive a dropped estimate.
EXCLUDING_STATUSES = {"Out", "Doubtful"}

SYSTEM_PROMPT = (
    "You are a daily fantasy sports research analyst. You are given one "
    "player, his team, his opponent, and the date of the slate. Use the web "
    "search tool to establish his availability and expected role for THAT "
    "specific game before answering.\n\n"
    "Search for the team's most recent injury report, the local beat "
    "reporter's latest posts, and any confirmed starting lineup. Check "
    "publication dates carefully: an injury report from a previous week is "
    "worse than no information, because it looks authoritative while being "
    "wrong. Search more than once when the first results are stale or "
    "off-topic.\n\n"
    "Always produce a brief, including when the evidence is thin. Stating "
    "what is known, what is missing, and what would settle it is useful "
    "regardless of your confidence, so those fields are never optional.\n\n"
    "Produce a numeric role estimate ONLY when your searches genuinely "
    "establish the player's availability and expected workload. When they do "
    "not, set \"estimate\" to null. Never infer availability from salary, "
    "from his season averages, or from the absence of news. Absence of a "
    "report is not evidence that a player is healthy.\n\n"
    "After searching, respond with a single JSON object as the final thing "
    "you write, with no markdown or code fences around it. The object must "
    "have exactly these keys:\n"
    "  status (exactly one of, verbatim: \"Confirmed Starter\", \"Active\", "
    "\"Probable\", \"Questionable\", \"Game-Time Decision\", \"Doubtful\", "
    "\"Out\", \"Rotation Risk\", \"Unknown\").\n"
    "  situation (string): what your searches actually establish, with the "
    "date of the most recent report you found. If they turned up nothing "
    "usable, say so plainly rather than speculating.\n"
    "  role_change (string): how his expected workload differs from normal "
    "and why -- who is injured ahead of or behind him, any minutes "
    "restriction, any change in starting role.\n"
    "  what_would_settle_it (list of up to 3 short strings): concrete, "
    "checkable things -- a named report, a confirmed lineup release, a "
    "specific beat reporter -- that would resolve his status.\n"
    "  estimate (an object, or null when unsupported). When present it must "
    "have exactly: opportunity (a number: expected minutes, snaps, plate "
    "appearances or time on ice for this game, or null if you cannot "
    "establish it), multiplier (a number from 0 to 2 scaling his normal "
    "per-game production for this game; 1.0 means a normal workload, 0.0 "
    "means he will not play), confidence (an integer from 0 to 100, never a "
    "0-1 fraction), and reasoning (a string).\n\n"
    "Do not output betting advice or recommendations."
)


class PlayerBriefError(Exception):
    """Raised when a brief cannot be produced or parsed."""


@dataclasses.dataclass
class PlayerBrief:
    player_id: str
    name: str
    team: str | None
    opponent: str | None
    slate_date: str
    status: str
    situation: str
    role_change: str
    what_would_settle_it: list[str]
    model: str
    multiplier: float | None = None
    opportunity: float | None = None
    confidence: int | None = None
    reasoning: str | None = None
    samples: int = 1
    multiplier_stdev: float | None = None
    multiplier_spread: float | None = None

    @property
    def has_estimate(self) -> bool:
        return self.multiplier is not None

    @property
    def is_excluded(self) -> bool:
        """Whether this player should be removed from the pool outright."""

        return self.status in EXCLUDING_STATUSES or self.multiplier == 0.0

    def as_record(self) -> dict[str, Any]:
        return {
            "player_id": self.player_id,
            "status": self.status,
            "situation": self.situation,
            "role_change": self.role_change,
            "what_would_settle_it": "; ".join(self.what_would_settle_it),
            "multiplier": self.multiplier,
            "dispersion": self.multiplier_stdev,
            "sample_count": self.samples,
            "model": self.model,
        }


def _extract_json(text: str) -> dict[str, Any]:
    """Pull the trailing JSON object out of a model response.

    The model is asked to end with bare JSON, but a search-using turn
    often narrates first, and occasionally wraps the object in a code
    fence despite instructions. Scanning back from the last closing
    brace handles both without a fragile regex.
    """

    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1]
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()

    end = cleaned.rfind("}")
    if end == -1:
        raise PlayerBriefError("No JSON object found in the model response.")

    depth = 0
    for index in range(end, -1, -1):
        if cleaned[index] == "}":
            depth += 1
        elif cleaned[index] == "{":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(cleaned[index : end + 1])
                except json.JSONDecodeError as error:
                    raise PlayerBriefError(f"Malformed JSON in response: {error}") from error

    raise PlayerBriefError("Unbalanced JSON braces in the model response.")


def _response_text(response: Any) -> str:
    """Concatenate the text blocks of an Anthropic response.

    A response that used the search tool carries several content blocks;
    only the text ones matter here.
    """

    parts = []
    for block in response.content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts)


def _parse_brief(
    payload: dict[str, Any],
    player: dict[str, Any],
    slate_date: str,
    model: str,
) -> PlayerBrief:
    status = str(payload.get("status", "Unknown")).strip()
    if status not in VALID_STATUSES:
        status = "Unknown"

    settle = payload.get("what_would_settle_it") or []
    if isinstance(settle, str):
        settle = [settle]

    brief = PlayerBrief(
        player_id=str(player["player_id"]),
        name=str(player.get("name", player["player_id"])),
        team=player.get("team"),
        opponent=player.get("opponent"),
        slate_date=slate_date,
        status=status,
        situation=str(payload.get("situation", "")).strip(),
        role_change=str(payload.get("role_change", "")).strip(),
        what_would_settle_it=[str(item).strip() for item in settle][:3],
        model=model,
    )

    estimate = payload.get("estimate")
    if isinstance(estimate, dict):
        multiplier = estimate.get("multiplier")
        if multiplier is not None:
            try:
                # Clamped rather than rejected: a model that answers 2.4
                # has still told us "much bigger role than normal", and
                # the ceiling is what keeps that from running away.
                brief.multiplier = max(0.0, min(2.0, float(multiplier)))
            except (TypeError, ValueError):
                brief.multiplier = None

        opportunity = estimate.get("opportunity")
        if opportunity is not None:
            try:
                brief.opportunity = max(0.0, float(opportunity))
            except (TypeError, ValueError):
                brief.opportunity = None

        confidence = estimate.get("confidence")
        if confidence is not None:
            try:
                brief.confidence = int(round(float(confidence)))
            except (TypeError, ValueError):
                brief.confidence = None

        brief.reasoning = str(estimate.get("reasoning", "")).strip() or None

    return brief


def brief_player_once(
    player: dict[str, Any],
    slate_date: str,
    sport: str,
    client: Any = None,
    model: str = DEFAULT_MODEL,
) -> PlayerBrief:
    """Draw a single brief for one player."""

    if client is None:
        from anthropic import Anthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise PlayerBriefError("ANTHROPIC_API_KEY is not set.")
        client = Anthropic()

    name = player.get("name", player["player_id"])
    team = player.get("team") or "unknown team"
    opponent = player.get("opponent") or "unknown opponent"

    prompt = (
        f"Sport: {sport}\n"
        f"Player: {name}\n"
        f"Team: {team}\n"
        f"Opponent: {opponent}\n"
        f"Slate date: {slate_date}\n\n"
        f"Establish whether {name} will play in this game and what his role "
        f"will be. Report what the most recent sources actually say, with "
        f"their dates."
    )

    response = client.messages.create(
        model=model,
        max_tokens=2000,
        system=SYSTEM_PROMPT,
        tools=[WEB_SEARCH_TOOL],
        messages=[{"role": "user", "content": prompt}],
    )

    payload = _extract_json(_response_text(response))
    return _parse_brief(payload, player, slate_date, model)


def should_escalate(samples: list[PlayerBrief]) -> bool:
    """Whether this player's disagreement warrants extra samples.

    Only players sitting near the reproducibility gate earn more draws.
    One comfortably inside it is settled and one far outside is settled
    the other way; in both cases another sample only costs money.
    """

    multipliers = [float(s.multiplier) for s in samples if s.has_estimate]

    if len(multipliers) < 2:
        return True

    stdev = statistics.stdev(multipliers)
    return abs(stdev - MAX_STDEV_FOR_MULTIPLIER) <= ESCALATION_BAND


def aggregate_samples(samples: list[PlayerBrief]) -> PlayerBrief:
    """Combine repeated briefs of one player into the brief to record.

    The multiplier is the median of the samples that produced one, which
    rejects a single wild draw. The prose is taken whole from the sample
    nearest that median rather than spliced together, so the reasoning
    on the page is the reasoning that produced the number.

    An estimate survives only if a majority of samples made one and they
    agree within the dispersion gate. Either failure keeps the brief and
    drops the number.

    Status is decided by majority vote separately, and the most
    pessimistic status wins a tie. Being wrong about a player being out
    costs a whole lineup; being wrong about him being in costs one
    roster spot.
    """

    if not samples:
        raise PlayerBriefError("Cannot aggregate an empty list of samples")

    estimating = [sample for sample in samples if sample.has_estimate]
    total = len(samples)

    status = _majority_status(samples)

    if not estimating:
        base = dataclasses.replace(samples[0])
        base.samples = total
        base.status = status
        base.multiplier = None
        base.multiplier_stdev = None
        base.multiplier_spread = None
        return base

    multipliers = [float(sample.multiplier) for sample in estimating]
    median = float(statistics.median(multipliers))

    spread = max(multipliers) - min(multipliers)
    stdev = statistics.stdev(multipliers) if len(multipliers) > 1 else None

    representative = min(estimating, key=lambda s: abs(float(s.multiplier) - median))

    aggregated = dataclasses.replace(representative)
    aggregated.samples = total
    aggregated.status = status
    aggregated.multiplier_spread = round(spread, 4)
    aggregated.multiplier_stdev = round(stdev, 4) if stdev is not None else None

    supported_by_majority = len(estimating) * 2 > total
    reproducible = stdev is None or stdev <= MAX_STDEV_FOR_MULTIPLIER

    if not (supported_by_majority and reproducible):
        aggregated.multiplier = None
        aggregated.opportunity = None
        aggregated.confidence = None
        return aggregated

    aggregated.multiplier = round(median, 4)

    opportunities = [
        float(s.opportunity) for s in estimating if s.opportunity is not None
    ]
    aggregated.opportunity = (
        round(float(statistics.median(opportunities)), 2) if opportunities else None
    )

    confidences = [int(s.confidence) for s in estimating if s.confidence is not None]
    aggregated.confidence = (
        int(round(statistics.median(confidences))) if confidences else None
    )

    return aggregated


# Ordered worst to best. A tie in the majority vote resolves to whichever
# status appears earlier here, so ambiguity resolves pessimistically.
_STATUS_SEVERITY = [
    "Out",
    "Doubtful",
    "Game-Time Decision",
    "Questionable",
    "Rotation Risk",
    "Unknown",
    "Probable",
    "Active",
    "Confirmed Starter",
]


def _majority_status(samples: list[PlayerBrief]) -> str:
    counts: dict[str, int] = {}
    for sample in samples:
        counts[sample.status] = counts.get(sample.status, 0) + 1

    best = max(counts.values())
    tied = [status for status, count in counts.items() if count == best]

    return min(tied, key=lambda status: _STATUS_SEVERITY.index(status)
               if status in _STATUS_SEVERITY else len(_STATUS_SEVERITY))


def brief_player(
    player: dict[str, Any],
    slate_date: str,
    sport: str,
    client: Any = None,
    model: str = DEFAULT_MODEL,
    samples: int = SAMPLES_PER_PLAYER,
) -> PlayerBrief:
    """Brief one player several times and return the aggregate.

    A failed draw is tolerated -- transient API errors should not lose a
    player -- but every draw failing raises, so a dead key or exhausted
    credit surfaces as an error rather than as a quiet "no news".
    """

    collected: list[PlayerBrief] = []
    failures = 0

    for index in range(MAX_SAMPLES_PER_PLAYER):
        if index >= samples and not should_escalate(collected):
            break
        if index >= samples and len(collected) >= MAX_SAMPLES_PER_PLAYER:
            break

        try:
            collected.append(brief_player_once(player, slate_date, sport, client, model))
        except PlayerBriefError:
            failures += 1
            continue

    if not collected:
        raise PlayerBriefError(
            f"All {failures} brief attempts failed for {player.get('name', player['player_id'])}."
        )

    return aggregate_samples(collected)


def apply_briefs_to_pool(
    pool: list[dict[str, Any]],
    briefs: list[PlayerBrief],
) -> list[dict[str, Any]]:
    """Fold research findings into a player pool before optimizing.

    An excluded player has his projection zeroed, which removes him from
    the optimizer's candidate set entirely rather than merely making him
    unattractive. A player with a gated multiplier is left untouched: the
    brief is still worth reading, but an unreproducible number must not
    quietly move a projection.
    """

    by_id = {brief.player_id: brief for brief in briefs}

    for player in pool:
        brief = by_id.get(str(player["player_id"]))
        if brief is None:
            continue

        player["research_status"] = brief.status
        player["research_note"] = brief.role_change or brief.situation

        if brief.is_excluded:
            player["projected_points"] = 0.0
            player["ceiling"] = 0.0
            player["floor"] = 0.0
            continue

        if brief.multiplier is not None and brief.multiplier != 1.0:
            for field in ("projected_points", "ceiling", "floor", "stdev"):
                if player.get(field) is not None:
                    player[field] = round(float(player[field]) * brief.multiplier, 3)

    return pool
