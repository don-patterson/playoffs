#!/usr/bin/env python3
"""
scorer.py — Score playoff pool picks against real NHL results.

Scoring:
  +2 pts  correct team pick for a round
  +1 pt   correct game count (only when team pick is also correct)
  +3 pts  bonus for correctly picking the Stanley Cup winner

Data source: NHL API  https://api-web.nhle.com/v1/playoff-series/carousel/{season}

"""

import csv
import json
import urllib.request
import sys

# ---------------------------------------------------------------------------
# NHL abbreviation → canonical team name (must match playoffs.py aliases)
# ---------------------------------------------------------------------------

ABBREV_TO_NAME: dict[str, str] = {
    "BUF": "Buffalo",
    "BOS": "Bruins",
    "TBL": "Lightning",
    "MTL": "Canadiens",
    "CAR": "Hurricanes",
    "OTT": "Senators",
    "PIT": "Penguins",
    "PHI": "Flyers",
    "COL": "Avalanche",
    "LAK": "Kings",
    "DAL": "Stars",
    "MIN": "Wild",
    "VGK": "Golden Knights",
    "UTA": "Mammoth",
    "EDM": "Oilers",
    "ANA": "Ducks",
}

# ---------------------------------------------------------------------------
# Bracket structure
#
# Defines which two Round-1 winners feed each later-round series, and which
# Round-2 winners feed the conference finals, etc.
#
# Format: matchup_id → (feeder_matchup_A, feeder_matchup_B)
# ---------------------------------------------------------------------------

BRACKET_FEEDERS: dict[str, tuple[str, str]] = {
    # East Round 2
    "east_r2_upper": ("east_r1_buf_bos", "east_r1_tbl_mtl"),
    "east_r2_lower": ("east_r1_car_ott", "east_r1_pit_phi"),
    # East Conference Final
    "east_r3": ("east_r2_upper", "east_r2_lower"),
    # West Round 2
    "west_r2_upper": ("west_r1_avs_kings", "west_r1_stars_wild"),
    "west_r2_lower": ("west_r1_gk_mammoth", "west_r1_oilers_ducks"),
    # West Conference Final
    "west_r3": ("west_r2_upper", "west_r2_lower"),
    # Stanley Cup Final
    "cup": ("east_r3", "west_r3"),
}

# Round-1 series letters → matchup_id (fixed at bracket seeding)
R1_LETTER_TO_MATCHUP: dict[str, str] = {
    "A": "east_r1_buf_bos",
    "B": "east_r1_tbl_mtl",
    "C": "east_r1_car_ott",
    "D": "east_r1_pit_phi",
    "E": "west_r1_avs_kings",
    "F": "west_r1_stars_wild",
    "G": "west_r1_gk_mammoth",
    "H": "west_r1_oilers_ducks",
}


# ---------------------------------------------------------------------------
# Load picks from CSV
# ---------------------------------------------------------------------------

def load_picks_csv(path: str = "picks.csv") -> dict:
    """
    Read picks.csv (produced by playoffs.py) and return the same nested dict
    structure as extract_picks():
      {person -> {matchup_id -> {description, teams, pick, games}}}
    """
    picks: dict = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            person = row["person"]
            mid = row["matchup_id"]
            if person not in picks:
                picks[person] = {}
            games_raw = row["games"]
            picks[person][mid] = {
                "description": row["description"],
                "teams": (row["team1"] or None, row["team2"] or None),
                "pick": row["pick"] or None,
                "games": int(games_raw) if games_raw else None,
            }
    return picks


# ---------------------------------------------------------------------------
# NHL API helpers
# ---------------------------------------------------------------------------

def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "playoff-pool-scorer/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def fetch_series_results(season: int = 20252026) -> dict[str, dict]:
    """
    Return a dict keyed by matchup_id with series result data for every
    series that has started (completed or in-progress).

    For each entry:
      team1, team2     : canonical team names
      team1_wins, team2_wins : current wins
      total_games      : games played so far
      winner           : canonical name of winner, or None if in progress
      complete         : bool
    """
    data = _fetch_json(
        f"https://api-web.nhle.com/v1/playoff-series/carousel/{season}"
    )
    json.dump(data, sys.stderr, indent=2)

    # First pass: collect all series from the API keyed by (round, letter)
    raw_series: dict[str, dict] = {}  # series_letter → raw data
    for round_data in data.get("rounds", []):
        for series in round_data.get("series", []):
            letter = series["seriesLetter"]
            top = series["topSeed"]
            bot = series["bottomSeed"]
            needed = series["neededToWin"]
            top_wins = top["wins"]
            bot_wins = bot["wins"]
            top_name = ABBREV_TO_NAME.get(top["abbrev"], top["abbrev"])
            bot_name = ABBREV_TO_NAME.get(bot["abbrev"], bot["abbrev"])

            total = top_wins + bot_wins
            if top_wins == needed:
                winner = top_name
            elif bot_wins == needed:
                winner = bot_name
            else:
                winner = None

            raw_series[letter] = {
                "round": round_data["roundNumber"],
                "team1": top_name,
                "team2": bot_name,
                "team1_wins": top_wins,
                "team2_wins": bot_wins,
                "total_games": total,
                "winner": winner,
                "complete": winner is not None,
            }

    # Second pass: map series letters → matchup_ids
    results: dict[str, dict] = {}

    # Round 1: use hardcoded letter mapping
    for letter, mid in R1_LETTER_TO_MATCHUP.items():
        if letter in raw_series:
            results[mid] = raw_series[letter]

    # Round 2+: match by finding which series teams match the expected feeders
    # Build winner lookup from what we have so far
    def get_winner(mid: str) -> str | None:
        return results.get(mid, {}).get("winner")

    # Iterate feeder map in dependency order (R2 before R3 before Cup)
    ordered = [
        "east_r2_upper", "east_r2_lower",
        "west_r2_upper", "west_r2_lower",
        "east_r3", "west_r3",
        "cup",
    ]
    for mid in ordered:
        if mid not in BRACKET_FEEDERS:
            continue
        feeder_a, feeder_b = BRACKET_FEEDERS[mid]
        winner_a = get_winner(feeder_a)
        winner_b = get_winner(feeder_b)
        if winner_a is None or winner_b is None:
            continue  # feeder series not yet complete

        # Find the carousel series whose teams are {winner_a, winner_b}
        expected = {winner_a, winner_b}
        for series in raw_series.values():
            if {series["team1"], series["team2"]} == expected:
                results[mid] = series
                break

    return results


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

POINTS_CORRECT_PICK = 2
POINTS_CORRECT_GAMES = 1
POINTS_CUP_WINNER = 3


def score_picks(picks: dict, results: dict[str, dict]) -> dict[str, dict]:
    """
    Score each person's picks against completed series results.

    Returns:
      {
        person: {
          "total": int,
          "breakdown": {
            matchup_id: {
              "description": str,
              "pick": str,
              "games_pick": int | None,
              "actual_winner": str | None,
              "actual_games": int | None,
              "complete": bool,
              "pts_team": int,
              "pts_games": int,
              "pts_cup_bonus": int,
              "pts_total": int,
            }
          }
        }
      }
    """
    scores: dict = {}

    for person, person_picks in picks.items():
        breakdown: dict = {}
        total = 0

        for mid, pick_data in person_picks.items():
            result = results.get(mid)

            pts_team = 0
            pts_games = 0
            pts_cup_bonus = 0

            actual_winner = result["winner"] if result else None
            actual_games = result["total_games"] if result and result["complete"] else None
            complete = result["complete"] if result else False

            pick_team = pick_data.get("pick")
            pick_games = pick_data.get("games")

            if complete and actual_winner:
                if pick_team == actual_winner:
                    pts_team = POINTS_CORRECT_PICK
                    if pick_games is not None and pick_games == actual_games:
                        pts_games = POINTS_CORRECT_GAMES
                    if mid == "cup":
                        pts_cup_bonus = POINTS_CUP_WINNER

            pts_total = pts_team + pts_games + pts_cup_bonus
            total += pts_total

            breakdown[mid] = {
                "description": pick_data["description"],
                "pick": pick_team,
                "games_pick": pick_games,
                "actual_winner": actual_winner,
                "actual_games": actual_games,
                "complete": complete,
                "pts_team": pts_team,
                "pts_games": pts_games,
                "pts_cup_bonus": pts_cup_bonus,
                "pts_total": pts_total,
            }

        scores[person] = {"total": total, "breakdown": breakdown}

    return scores


# ---------------------------------------------------------------------------
# HTML output
# ---------------------------------------------------------------------------

# One colour per NHL team (approx. primary jersey colour)
TEAM_COLOURS: dict[str, str] = {
    "Avalanche":      "#6F263D",
    "Kings":          "#111111",
    "Stars":          "#006847",
    "Wild":           "#154734",
    "Golden Knights": "#B4975A",
    "Mammoth":        "#6EA4D3",
    "Oilers":         "#FF4C00",
    "Ducks":          "#F47A38",
    "Buffalo":        "#003087",
    "Bruins":         "#FFB81C",
    "Lightning":      "#002868",
    "Canadiens":      "#AF1E2D",
    "Hurricanes":     "#CC0000",
    "Senators":       "#C52032",
    "Penguins":       "#FCB514",
    "Flyers":         "#F74902",
}


def _round_of(mid: str) -> int:
    """Return the playoff round number for a matchup_id."""
    if "_r1_" in mid:
        return 1
    if "_r2_" in mid:
        return 2
    if "_r3" in mid:
        return 3
    return 4  # cup


_ROUND_DIVIDER_LABEL: dict[int, str] = {
    2: "Round 2",
    3: "Conference Finals",
    4: "Stanley Cup Final",
}


def _team_badge(team: str | None) -> str:
    if not team:
        return "<span class='badge badge-tbd'>TBD</span>"
    colour = TEAM_COLOURS.get(team, "#555")
    return f"<span class='badge' style='background:{colour}'>{team}</span>"


def render_html(
    scores: dict,
    results: dict,
    season: int = 20252026,
    out_path: str = "index.html",
) -> None:
    from datetime import datetime, timezone
    ranked = sorted(scores.items(), key=lambda x: x[1]["total"], reverse=True)
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # Ordered list of matchup IDs (same order as picks CSV)
    all_mids: list[str] = []
    if ranked:
        all_mids = list(ranked[0][1]["breakdown"].keys())

    # ── scoreboard rows ───────────────────────────────────────────────────
    sb_rows = ""
    medal = ["🥇", "🥈", "🥉"]
    for i, (name, data) in enumerate(ranked):
        rank_cell = medal[i] if i < 3 else str(i + 1)
        sb_rows += (
            f"<tr><td>{rank_cell}</td>"
            f"<td><a href='#{name.lower()}'>{name}</a></td>"
            f"<td class='pts'>{data['total']}</td></tr>\n"
        )

    # ── series results summary ─────────────────────────────────────────────
    series_rows = ""
    for mid in all_mids:
        r = results.get(mid)
        if not r:
            continue
        desc = ranked[0][1]["breakdown"][mid]["description"] if ranked else mid
        if r["complete"]:
            status = f"{_team_badge(r['winner'])} in {r['total_games']}"
        else:
            t1_wins = r["team1_wins"]
            t2_wins = r["team2_wins"]
            status = (
                f"{_team_badge(r['team1'])} {t1_wins} – {t2_wins} "
                f"{_team_badge(r['team2'])} <em>(in progress)</em>"
            )
        series_rows += f"<tr><td>{desc}</td><td>{status}</td></tr>\n"

    # ── per-person pick tables ─────────────────────────────────────────────
    all_mids_by_round = sorted(all_mids, key=_round_of)
    person_sections = ""
    for name, data in ranked:
        pick_rows = ""
        current_round: int | None = None
        for mid in all_mids_by_round:
            r = _round_of(mid)
            if r != current_round:
                if current_round is not None:
                    label = _ROUND_DIVIDER_LABEL.get(r, f"Round {r}")
                    pick_rows += f"<tr class='round-divider'><td colspan='4'>{label}</td></tr>\n"
                current_round = r
            b = data["breakdown"][mid]
            if not b["complete"]:
                pick_str = _team_badge(b["pick"])
                if b["games_pick"]:
                    pick_str += f" in {b['games_pick']}"
                pick_rows += (
                    f"<tr class='pending'>"
                    f"<td>{b['description']}</td>"
                    f"<td>{pick_str}</td>"
                    f"<td class='result-cell'>—</td>"
                    f"<td class='pts'>—</td></tr>\n"
                )
                continue

            row_class = "correct" if b["pts_team"] > 0 else "wrong"
            pick_str = _team_badge(b["pick"])
            if b["games_pick"]:
                pick_str += f" in {b['games_pick']}"
            actual_str = _team_badge(b["actual_winner"])
            if b["actual_games"]:
                actual_str += f" in {b['actual_games']}"
            pts_parts = []
            if b["pts_team"]:
                pts_parts.append(f"+{b['pts_team']} team")
            if b["pts_games"]:
                pts_parts.append(f"+{b['pts_games']} games")
            if b["pts_cup_bonus"]:
                pts_parts.append(f"+{b['pts_cup_bonus']} cup")
            pts_label = ", ".join(pts_parts) if pts_parts else "0"
            pick_rows += (
                f"<tr class='{row_class}'>"
                f"<td>{b['description']}</td>"
                f"<td>{pick_str}</td>"
                f"<td class='result-cell'>{actual_str}</td>"
                f"<td class='pts'>{pts_label}</td></tr>\n"
            )

        person_sections += f"""
<section id='{name.lower()}' class='person-card'>
  <h2>{name} <span class='total-pts'>{data['total']} pts</span></h2>
  <table>
    <thead><tr><th>Series</th><th>Your pick</th><th>Result</th><th>Pts</th></tr></thead>
    <tbody>{pick_rows}</tbody>
  </table>
</section>
"""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>2026 NHL Playoff Pool</title>
<style>
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --correct: #1a3a1a;
    --correct-border: #3fb950;
    --wrong: #3a1a1a;
    --wrong-border: #f85149;
    --pending: #1a1a2a;
    --accent: #58a6ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: system-ui, sans-serif;
          line-height: 1.5; padding: 1.5rem; }}
  h1 {{ font-size: 1.8rem; margin-bottom: 0.25rem; }}
  .subtitle {{ color: var(--muted); margin-bottom: 2rem; font-size: 0.9rem; }}
  h2 {{ font-size: 1.2rem; margin-bottom: 0.75rem; display: flex;
         align-items: baseline; gap: 0.6rem; }}
  .total-pts {{ background: var(--accent); color: #000; border-radius: 999px;
                padding: 0.1rem 0.6rem; font-size: 0.85rem; font-weight: 700; }}
  section {{ margin-bottom: 1rem; }}
  /* Scoreboard */
  #scoreboard table, .person-card table {{
    width: 100%; border-collapse: collapse; margin-bottom: 1rem;
  }}
  #scoreboard {{ max-width: 320px; margin-bottom: 2.5rem; }}
  th, td {{ padding: 0.45rem 0.75rem; text-align: left;
             border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 600; font-size: 0.8rem;
         text-transform: uppercase; letter-spacing: 0.05em; }}
  td.pts {{ text-align: right; font-weight: 700; }}
  /* Series summary */
  #series-summary {{ margin-bottom: 2.5rem; }}
  #series-summary h2 {{ font-size: 1.1rem; color: var(--muted); }}
  /* Pick rows */
  tr.correct {{ background: var(--correct);
                border-left: 3px solid var(--correct-border); }}
  tr.wrong   {{ background: var(--wrong);
                border-left: 3px solid var(--wrong-border); }}
  tr.pending {{ color: var(--muted); }}
  tr.round-divider td {{
    background: #21262d;
    color: var(--muted);
    font-size: 0.72rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-weight: 700;
    padding: 0.3rem 0.75rem;
    border-top: 2px solid var(--border);
    border-bottom: 1px solid var(--border);
  }}
  /* Person cards */
  .person-card {{ background: var(--surface); border: 1px solid var(--border);
                  border-radius: 8px; padding: 1rem 1.25rem; margin-bottom: 1.5rem; }}
  .result-cell {{ color: var(--muted); }}
  /* Badges */
  .badge {{ display: inline-block; padding: 0.15rem 0.55rem;
             border-radius: 999px; font-size: 0.78rem; font-weight: 600;
             color: #fff; white-space: nowrap; }}
  .badge-tbd {{ background: #555; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  @media (max-width: 600px) {{
    th:nth-child(3), td:nth-child(3) {{ display: none; }}
  }}
</style>
</head>
<body>
<h1>🏒 2026 NHL Playoff Pool</h1>
<p class="subtitle">Last updated: {updated} &nbsp;·&nbsp; Season {season}</p>

<section id="scoreboard">
  <h2>Standings</h2>
  <table>
    <thead><tr><th>#</th><th>Name</th><th>Pts</th></tr></thead>
    <tbody>{sb_rows}</tbody>
  </table>
</section>

<section id="series-summary">
  <h2>Series Results</h2>
  <table>
    <thead><tr><th>Series</th><th>Result / Status</th></tr></thead>
    <tbody>{series_rows}</tbody>
  </table>
</section>

{''.join(person_sections.splitlines(keepends=True))}

</body>
</html>
"""

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"→ {out_path} written")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"Fetching results...")
    results = fetch_series_results(20252026)
    picks = load_picks_csv()
    scores = score_picks(picks, results)

    render_html(scores, results, season=20252026, out_path="index.html")
