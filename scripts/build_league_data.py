"""Build the Brojees shared league data files.

Run by the GitHub Action in .github/workflows/league-data.yml (or by hand):

    python scripts/build_league_data.py [--league LEAGUE_ID] [--out data]

Writes three things the app reads from the site itself:

  data/history.json                    every completed week, this season and past seasons
  data/records.json                    the all-time record book and manager career stats
  data/projections/<season>_<week>.json  this week's projections, frozen before kickoff

Only the Python standard library is used, so the Action needs no installs. Files are
only rewritten when their contents change, so quiet runs make no commits.

The math mirrors index.html (projectedPointsFromMap, loadSeasonHistory, optimalLineup),
so the app shows the same numbers whether it reads these files or works them out itself.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.sleeper.app/v1"
DEFAULT_LEAGUE_ID = "1388977201008877568"
ESPN_SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
USER_AGENT = "BrojeesFantasyMenu-data/1.0 (+https://raschella001.github.io/Brojees-Fantasy-Menu/)"
MAX_PAST_SEASONS = 6
TOP_N = 5
# Projections are refreshed once more inside this window before the week's first kickoff.
FINAL_REFRESH_HOURS = 6
PLAYERS_CACHE_HOURS = 24  # Sleeper asks for /players/nfl at most once a day.

SLOT_ELIGIBILITY = {
    "FLEX": ["RB", "WR", "TE"], "WRRB_FLEX": ["WR", "RB"], "REC_FLEX": ["WR", "TE"],
    "SUPER_FLEX": ["QB", "RB", "WR", "TE"],
    "IDP_FLEX": ["DL", "DE", "DT", "LB", "DB", "CB", "S", "SS", "FS"],
    "DL": ["DL", "DE", "DT"], "DB": ["DB", "CB", "S", "SS", "FS"],
}


# ---------- helpers ----------

def log(msg):
    print(msg, flush=True)


def fetch_json(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as err:
            last = err
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not load {url}: {last}")


def fetch_or(url, fallback):
    try:
        return fetch_json(url)
    except RuntimeError as err:
        log(f"  warning: {err}")
        return fallback


def r2(x):
    """Round to 2 decimals the way JavaScript's Math.round does (halves go up)."""
    return math.floor(float(x) * 100 + 0.5) / 100


def num(value):
    """JavaScript Number(): None -> 0, numeric strings -> float, anything else -> None (NaN)."""
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if math.isfinite(n) else None


def js_or(*values):
    """JavaScript a || b || c for the values Sleeper sends (objects are always truthy)."""
    for v in values:
        if isinstance(v, (dict, list)) or v:
            return v
    return values[-1]


def now_utc():
    return dt.datetime.now(dt.timezone.utc)


def iso(moment):
    return moment.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def write_if_changed(path, payload, volatile=("generated_at",)):
    """Write JSON only when something other than the timestamp changed. Returns True if written."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, json.JSONDecodeError):
        existing = None
    strip = lambda obj: {k: v for k, v in obj.items() if k not in volatile} if isinstance(obj, dict) else obj
    if existing is not None and strip(existing) == strip(payload):
        log(f"  unchanged: {path}")
        return False
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(payload, fh, separators=(",", ":"), ensure_ascii=False)
        fh.write("\n")
    log(f"  wrote: {path}")
    return True


# ---------- players (positions only, for bench math) ----------

def load_player_positions(cache_dir):
    path = os.path.join(cache_dir, "player_positions.json")
    try:
        if time.time() - os.path.getmtime(path) < PLAYERS_CACHE_HOURS * 3600:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    except (OSError, json.JSONDecodeError):
        pass
    log("Loading the Sleeper player list (positions)...")
    players = fetch_or(f"{API}/players/nfl", {})
    positions = {}
    for pid, p in (players or {}).items():
        if not isinstance(p, dict):
            continue
        listed = p.get("fantasy_positions") or [p.get("position")]
        listed = [str(x).upper() for x in listed if x]
        if listed:
            positions[str(pid)] = listed
    if positions:
        os.makedirs(cache_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(positions, fh, separators=(",", ":"))
    return positions


# ---------- lineup math (mirrors optimalLineup / benchLeftForRow) ----------

def starter_slots(league):
    return [p for p in (league.get("roster_positions") or []) if p not in ("BN", "IR", "TAXI")]


def slot_eligibility(slot):
    return SLOT_ELIGIBILITY.get(slot, [slot])


def optimal_total(slots, pool, value_of, positions):
    used = set()
    order = sorted(range(len(slots)), key=lambda i: len(slot_eligibility(slots[i])))
    candidates = list(dict.fromkeys(str(x) for x in pool if x and str(x) != "0"))
    total = 0.0
    for i in order:
        allowed = slot_eligibility(slots[i])
        best, best_value = None, -math.inf
        for pid in candidates:
            if pid in used or not any(pos in allowed for pos in positions.get(pid, [])):
                continue
            value = value_of(pid)
            if value > best_value:
                best, best_value = pid, value
        if best:
            used.add(best)
            total += best_value
    return r2(total)


def bench_left(row, slots, positions):
    if not row.get("players"):
        return None
    pp = row.get("pp") or {}

    def value_of(pid):
        v = num(pp.get(pid)) if pid in pp else None
        return v if v is not None else 0.0

    best = optimal_total(slots, row["players"], value_of, positions)
    actual = r2(sum(value_of(str(pid)) for pid in row.get("starters") or [] if pid and str(pid) != "0"))
    return {"best": best, "actual": actual, "left": max(0.0, r2(best - actual))}


# ---------- seasons (mirrors loadSeasonHistory) ----------

def team_name(user):
    meta = user.get("metadata") or {}
    return meta.get("team_name") or user.get("display_name") or user.get("username") or "Manager"


def load_season(league_id, through_week):
    league = fetch_json(f"{API}/league/{league_id}")
    rosters = fetch_or(f"{API}/league/{league_id}/rosters", [])
    users = fetch_or(f"{API}/league/{league_id}/users", [])
    settings = league.get("settings") or {}
    start = max(1, int(num(settings.get("start_week")) or 1))
    last = min(18, int(through_week or 0))
    weeks = []
    for week in range(start, last + 1):
        raw = fetch_or(f"{API}/league/{league_id}/matchups/{week}", [])
        rows = []
        for r in raw if isinstance(raw, list) else []:
            if not r or r.get("roster_id") is None:
                continue
            pts = r.get("custom_points")
            if pts is None:
                pts = r.get("points")
            rows.append({
                "rid": str(r["roster_id"]),
                "mid": r.get("matchup_id"),
                "pts": r2(num(pts) or 0),
                "starters": [str(x) for x in r.get("starters") or []],
                "players": [str(x) for x in r.get("players") or []],
                "pp": r.get("players_points") or {},
            })
        if any(row["pts"] > 0 for row in rows):
            weeks.append({"week": week, "rows": rows})
    prev = league.get("previous_league_id")
    bracket = fetch_or(f"{API}/league/{league_id}/winners_bracket", []) if league.get("status") == "complete" else []
    return {
        "league_id": str(league.get("league_id")),
        "season": str(league.get("season") or ""),
        "status": league.get("status"),
        "playoffStart": int(num(settings.get("playoff_week_start")) or 0),
        "slots": starter_slots(league),
        "owners": {str(r.get("roster_id")): str(r.get("owner_id") or "") for r in rosters or []},
        "names": {str(u.get("user_id")): team_name(u) for u in users or []},
        "managers": {str(u.get("user_id")): u.get("display_name") or u.get("username") or "Manager" for u in users or []},
        "previous_league_id": str(prev) if prev and str(prev) != "0" else None,
        "weeks": weeks,
        "bracket": bracket if isinstance(bracket, list) else [],
        "league": league,
    }


def public_season(season, detailed):
    """The shape index.html's loadSeasonHistory returns."""
    weeks = []
    for w in season["weeks"]:
        rows = []
        for row in w["rows"]:
            out = {"rid": row["rid"], "mid": row["mid"], "pts": row["pts"]}
            if detailed:
                out.update(starters=row["starters"], players=row["players"], pp=row["pp"])
            rows.append(out)
        weeks.append({"week": w["week"], "rows": rows})
    return {k: season[k] for k in ("league_id", "season", "playoffStart", "slots", "owners", "names", "previous_league_id")} | {"weeks": weeks}


# ---------- record book ----------

def games_of(season):
    """Every head-to-head game: pairs of rows sharing a matchup id."""
    out = []
    for w in season["weeks"]:
        groups = {}
        for row in w["rows"]:
            if row["mid"] is not None:
                groups.setdefault(str(row["mid"]), []).append(row)
        for pair in groups.values():
            if len(pair) == 2:
                playoff = bool(season["playoffStart"] and w["week"] >= season["playoffStart"])
                out.append({"week": w["week"], "a": pair[0], "b": pair[1], "playoff": playoff})
    return out


def build_records(seasons, positions):
    """seasons: oldest first."""
    entries = {k: [] for k in ("high_score", "low_score", "blowout", "closest", "high_loss", "low_win", "player_week", "bench_blunder")}
    season_pf, season_record = [], []
    managers = {}
    timeline = {}  # owner -> chronological results for streaks
    champions = []

    def who(season, rid):
        owner = season["owners"].get(str(rid), "")
        return owner, season["names"].get(owner, f"Team {rid}")

    def manager(season, owner):
        m = managers.setdefault(owner, {"owner": owner, "w": 0, "l": 0, "t": 0, "pf": 0.0, "games": 0, "seasons": [],
                                        "playoffs": 0, "titles": 0, "runner_up": 0, "high": None})
        if season["season"] not in m["seasons"]:
            m["seasons"].append(season["season"])
        m["team"] = season["names"].get(owner, m.get("team", "Manager"))
        m["name"] = season["managers"].get(owner, m.get("name", "Manager"))
        return m

    for season in seasons:
        yr = season["season"]
        games = games_of(season)
        reg_stats = {}
        for g in games:
            a, b = g["a"], g["b"]
            for me, opp in ((a, b), (b, a)):
                owner, team = who(season, me["rid"])
                opp_owner, opp_team = who(season, opp["rid"])
                base = {"season": yr, "week": g["week"], "playoff": g["playoff"], "owner": owner, "team": team,
                        "opp_owner": opp_owner, "opp_team": opp_team, "score": me["pts"], "opp_score": opp["pts"]}
                if me["pts"] > 0:
                    entries["high_score"].append(base | {"value": me["pts"]})
                    entries["low_score"].append(base | {"value": me["pts"]})
                result = "W" if me["pts"] > opp["pts"] else "L" if me["pts"] < opp["pts"] else "T"
                if result == "W":
                    entries["blowout"].append(base | {"value": r2(me["pts"] - opp["pts"])})
                    entries["closest"].append(base | {"value": r2(me["pts"] - opp["pts"])})
                    entries["low_win"].append(base | {"value": me["pts"]})
                elif result == "L":
                    entries["high_loss"].append(base | {"value": me["pts"]})
                timeline.setdefault(owner, []).append(base | {"result": result})
                m = manager(season, owner)
                if not m["high"] or me["pts"] > m["high"]["value"]:
                    m["high"] = {"value": me["pts"], "season": yr, "week": g["week"]}
                if not g["playoff"]:
                    s = reg_stats.setdefault(owner, {"w": 0, "l": 0, "t": 0, "pf": 0.0, "games": 0, "team": team})
                    s[result.lower()] += 1
                    s["pf"] += me["pts"]
                    s["games"] += 1
                    m[result.lower()] += 1
                    m["pf"] += me["pts"]
                    m["games"] += 1
        complete = season["status"] == "complete"
        for owner, s in reg_stats.items():
            pct = (s["w"] + s["t"] / 2) / s["games"] if s["games"] else 0
            row = {"season": yr, "owner": owner, "team": s["team"], "w": s["w"], "l": s["l"], "t": s["t"],
                   "pf": r2(s["pf"]), "games": s["games"], "pct": round(pct, 4), "in_progress": not complete}
            season_pf.append(row | {"value": r2(s["pf"])})
            season_record.append(row | {"value": round(pct, 4)})

        # Player and bench records only count teams that actually played a game that week.
        played = {(g["week"], g[side]["rid"]) for g in games for side in ("a", "b")}
        for w in season["weeks"]:
            playoff = bool(season["playoffStart"] and w["week"] >= season["playoffStart"])
            for row in w["rows"]:
                if (w["week"], row["rid"]) not in played:
                    continue
                owner, team = who(season, row["rid"])
                base = {"season": yr, "week": w["week"], "playoff": playoff, "owner": owner, "team": team}
                for pid in row["starters"]:
                    pts = num(row["pp"].get(pid)) if pid in row["pp"] else None
                    if pid and pid != "0" and pts is not None:
                        entries["player_week"].append(base | {"value": r2(pts), "player_id": pid})
                bench = bench_left(row, season["slots"], positions) if positions else None
                if bench and bench["left"] > 0.05:
                    entries["bench_blunder"].append(base | {"value": bench["left"], "actual": bench["actual"], "best": bench["best"]})

        # Playoff appearances and titles from the winners bracket.
        in_bracket = set()
        for match in season["bracket"]:
            for key in ("t1", "t2"):
                if isinstance(match.get(key), int):
                    in_bracket.add(str(match[key]))
            if match.get("p") == 1 and match.get("w") is not None:
                champ_owner, champ_team = who(season, match["w"])
                runner_owner, runner_team = who(season, match.get("l")) if match.get("l") is not None else ("", "")
                champions.append({"season": yr, "owner": champ_owner, "team": champ_team, "runner_owner": runner_owner, "runner_team": runner_team})
                if champ_owner:
                    manager(season, champ_owner)["titles"] += 1
                if runner_owner:
                    manager(season, runner_owner)["runner_up"] += 1
        for rid in in_bracket:
            owner, _ = who(season, rid)
            if owner:
                manager(season, owner)["playoffs"] += 1

    def top(items, key=lambda e: e["value"], reverse=True):
        return sorted(items, key=key, reverse=reverse)[:TOP_N]

    def streaks(kind):
        best = []
        for owner, games in timeline.items():
            games.sort(key=lambda g: (int(g["season"] or 0), g["week"]))
            run = []
            for g in games + [None]:
                if g and g["result"] == kind:
                    run.append(g)
                    continue
                if run:
                    best.append({"owner": owner, "team": run[-1]["team"], "value": len(run),
                                 "from": {"season": run[0]["season"], "week": run[0]["week"]},
                                 "to": {"season": run[-1]["season"], "week": run[-1]["week"]},
                                 "active": g is None})
                run = []
        return sorted(best, key=lambda s: (-s["value"], s["to"]["season"]), reverse=False)[:TOP_N]

    records = {
        "high_score": top(entries["high_score"]),
        "low_score": top(entries["low_score"], reverse=False),
        "blowout": top(entries["blowout"]),
        "closest": top(entries["closest"], reverse=False),
        "high_loss": top(entries["high_loss"]),
        "low_win": top(entries["low_win"], reverse=False),
        "player_week": top(entries["player_week"]),
        "bench_blunder": top(entries["bench_blunder"]),
        "season_points": top(season_pf),
        "season_record": top(season_record, key=lambda e: (e["value"], e["pf"])),
        "win_streak": streaks("W"),
        "losing_streak": streaks("L"),
    }
    career = []
    for m in managers.values():
        games = m["w"] + m["l"] + m["t"]
        career.append({
            "owner": m["owner"], "name": m.get("name", "Manager"), "team": m.get("team", "Manager"),
            "seasons": len(m["seasons"]), "w": m["w"], "l": m["l"], "t": m["t"],
            "pct": round((m["w"] + m["t"] / 2) / games, 4) if games else 0,
            "pf": r2(m["pf"]), "avg": r2(m["pf"] / m["games"]) if m["games"] else 0,
            "playoffs": m["playoffs"], "titles": m["titles"], "runner_up": m["runner_up"], "high": m["high"],
        })
    career.sort(key=lambda c: (-c["titles"], -c["pct"], -c["pf"]))
    champions.sort(key=lambda c: c["season"], reverse=True)
    return records, career, champions


# ---------- frozen projections (mirrors loadOrCreateOriginalProjectionSnapshot) ----------

def normalize_projections(raw):
    out = {}
    if isinstance(raw, list):
        for row in raw:
            if not isinstance(row, dict):
                continue
            player = row.get("player") if isinstance(row.get("player"), dict) else {}
            pid = row.get("player_id") or player.get("player_id") or player.get("id")
            if pid:
                out[str(pid)] = js_or(row.get("stats"), row.get("projection"), row)
    elif isinstance(raw, dict):
        for pid, row in raw.items():
            row = row if isinstance(row, dict) else {}
            out[str(pid)] = js_or(row.get("stats"), row.get("projection"), row, {})
    return out


def fetch_week_projections(season, week):
    positions = "&".join(f"position[]={p}" for p in ("QB", "RB", "WR", "TE", "K", "DEF"))
    for url in (f"{API}/projections/nfl/regular/{season}/{week}",
                f"https://api.sleeper.com/projections/nfl/{season}/{week}?season_type=regular&{positions}&order_by=pts_ppr"):
        try:
            normalized = normalize_projections(fetch_json(url))
            if normalized:
                return normalized
        except RuntimeError as err:
            log(f"  warning: {err}")
    return {}


def projected_points(stats, scoring):
    if not isinstance(stats, dict):
        return None
    total, matched = 0.0, 0
    for key, mult_raw in (scoring or {}).items():
        mult = num(mult_raw)
        stat = num(stats[key]) if key in stats else None
        if mult is None or stat is None:
            continue
        total += mult * stat
        matched += 1
    if matched:
        return r2(total)
    rec = num((scoring or {}).get("rec", 1))
    fallback = stats.get("pts_std") if rec == 0 else stats.get("pts_half_ppr") if rec == 0.5 else stats.get("pts_ppr")
    n = num(fallback) if fallback is not None else None
    return r2(n) if n is not None else None


def week_kickoffs(season, week):
    raw = fetch_or(f"{ESPN_SCOREBOARD}?season={season}&seasontype=2&week={week}", {})
    times, started = [], False
    for event in raw.get("events") or []:
        comp = (event.get("competitions") or [{}])[0]
        when = parse_time(event.get("date") or comp.get("date"))
        if when:
            times.append(when)
        state = ((comp.get("status") or event.get("status") or {}).get("type") or {}).get("state")
        if state in ("in", "post"):
            started = True
    return (min(times) if times else None), started


def freeze_projections(league, state, out_dir):
    season = str(state.get("season") or "")
    week = int(num(state.get("week") or state.get("leg")) or 0)
    if str(state.get("season_type") or "regular").lower() != "regular" or not week:
        log("Projections: not the regular season, skipping.")
        return
    if str(league.get("season")) != season or league.get("status") not in ("in_season", "drafted", "post_season"):
        log("Projections: league is not in season, skipping.")
        return
    path = os.path.join(out_dir, "projections", f"{season}_{week}.json")
    try:
        with open(path, encoding="utf-8") as fh:
            existing = json.load(fh)
    except (OSError, json.JSONDecodeError):
        existing = None
    if existing and existing.get("locked"):
        log(f"Projections: week {week} already locked.")
        return

    first_kickoff, started = week_kickoffs(season, week)
    now = now_utc()
    if existing and started:
        write_if_changed(path, existing | {"locked": True, "locked_at": iso(now)})
        return
    if existing and not started:
        saved = parse_time(existing.get("saved_at"))
        window = first_kickoff - dt.timedelta(hours=FINAL_REFRESH_HOURS) if first_kickoff else None
        if not window or now < window or (saved and saved >= window):
            log(f"Projections: week {week} saved, waiting for the final pre-kickoff refresh.")
            return

    league_id = str(league["league_id"])
    projections = fetch_week_projections(season, week)
    if not projections:
        log("Projections: Sleeper returned none, skipping.")
        return
    scoring = league.get("scoring_settings") or {}
    player_points = {}
    for pid, stats in projections.items():
        pts = projected_points(stats, scoring)
        if pts is not None:
            player_points[pid] = pts
    team_totals = {}
    for m in fetch_or(f"{API}/league/{league_id}/matchups/{week}", []) or []:
        total = sum(player_points.get(str(pid), 0) for pid in m.get("starters") or [])
        team_totals[str(m.get("roster_id"))] = r2(total)
    write_if_changed(path, {
        "league_id": league_id, "season": int(season), "week": week,
        "saved_at": iso(now), "first_kickoff": iso(first_kickoff) if first_kickoff else None,
        "locked": bool(started), "late": bool(started),
        "source": "github-action",
        "player_points": player_points, "team_totals": team_totals,
    }, volatile=())


# ---------- main ----------

def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--league", default=os.environ.get("BROJEES_LEAGUE_ID", DEFAULT_LEAGUE_ID))
    parser.add_argument("--out", default="data")
    parser.add_argument("--cache", default=".cache")
    args = parser.parse_args()

    log(f"League {args.league}")
    state = fetch_json(f"{API}/state/nfl")
    league = fetch_json(f"{API}/league/{args.league}")
    current_week = max(1, int(num(state.get("week") or state.get("leg")) or 1))
    through = 18 if league.get("status") == "complete" else current_week - 1

    log(f"Loading this season through week {through}...")
    current = load_season(args.league, through)
    past = []
    prev = current["previous_league_id"]
    while prev and len(past) < MAX_PAST_SEASONS:
        log(f"Loading past season {prev}...")
        try:
            season = load_season(prev, 18)
        except RuntimeError as err:
            log(f"  warning: {err}")
            break
        past.append(season)
        prev = season["previous_league_id"]

    generated = iso(now_utc())
    write_if_changed(os.path.join(args.out, "history.json"), {
        "generated_at": generated, "league_id": str(args.league), "season": current["season"],
        "through_week": through,
        "current": public_season(current, detailed=True),
        "past": [public_season(s, detailed=False) for s in past],
    })

    positions = load_player_positions(args.cache)
    records, career, champions = build_records(list(reversed(past)) + [current], positions)
    write_if_changed(os.path.join(args.out, "records.json"), {
        "generated_at": generated, "league_id": str(args.league),
        "seasons": [s["season"] for s in [current] + past],
        "through": {"season": current["season"], "week": through},
        "records": records, "managers": career, "champions": champions,
    })

    freeze_projections(league, state, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
