#v2.92
#16oct26
#score adjustments (e.g. week-1 dues bonus/penalty) reconciled with per-week scores; earlier: download ESPN-hosted team logos (they need an ESPN login) into output/logos/, end-of-run WARNINGS block + TOT_WARNINGS_FILE for the weekly email, seasonComplete flag on yearly records, weekly high/low score links (games list), keeper points shown mid-season, retries inside _direct_api + warning when a keeper sweep week still fails, warnings for owners that resolve to None or an unresolved ESPN ID; earlier: per-week scores for every matchup (ESPN pointsByScoringPeriod), 2019 duplicate playoff rows collapsed, single-week score stats, two-week matchup companions, per-week averages, consolation dollars 2020+ regular season only, two-week playoff analysis; earlier: abort (exit 1) if any season fails to fetch, exit 2 on expired cookies, retries + logged week errors, bench points gathered in matchup pass, _direct_api memoized, owner/team lookup helpers, final week from matchup_periods, removed roster_analysis/two_week outputs and unused fields, skip final_standing 0 (in-progress season) in playoff detection
#!/usr/bin/env python3
"""
ESPN Fantasy Football Data Collector using espn-api library
Configured for "the-other-league" (separate league from Monday Morning Tears).

This properly handles all years including 2013-2018.
"""

from espn_api.football import League
import json
from datetime import datetime
from collections import defaultdict, Counter
import os
import argparse
import requests
import sys
import time
import re
import hashlib
from urllib.parse import urlparse
from dotenv import load_dotenv
from espn_api.requests.espn_requests import ESPNAccessDenied

load_dotenv("/home/pi/fantasystats/.env")
load_dotenv()                               # local: finds a .env next to the script

# ──────────────────────────────────────────────────────────────────────────
# Configuration — FILL THESE IN for this league
# ──────────────────────────────────────────────────────────────────────────
LEAGUE_ID = 1517226
OUTPUT_DIR = "output"
MAX_FETCH_ATTEMPTS = 3   # per-season / per-request retries before giving up
LOGO_DIR = os.path.join("output", "logos")   # self-hosted copies of ESPN-uploaded team logos
AUTH_LOGO_HOST = "mystique-api.fantasy.espn.com"   # images uploaded directly to ESPN; need an ESPN login to load
MAX_LOGO_BYTES = 2 * 1024 * 1024
WEEKLY_TOLERANCE = 0.02  # allowed rounding gap between weekly scores and a matchup total

# This league has always run a 4-team, 2-round playoff bracket (per Bill's
# notes). Used only as a fallback when league.settings.playoff_team_count
# is unavailable from the ESPN API for a given season — settings are always
# preferred when present.
LEAGUE_PLAYOFF_TEAM_COUNT = 4

# Manual point overrides for keepers ESPN has scrubbed from historical API data,
# plus season-stats-page notes for those same keepers. Contains real owner/player
# names, so this lives in a separate gitignored file (not committed to GitHub)
# rather than inline here. See tot_keepers.py for the format and how to add new
# entries -- that file must be SCP'd to the Pi manually after editing, the same
# way this collector script used to be before it started being deployed via git.
try:
    from tot_keepers import KEEPER_POINT_OVERRIDES, KEEPER_NOTES
except ImportError as e:
    raise ImportError(
        "tot_keepers.py not found. This file holds KEEPER_POINT_OVERRIDES and "
        "KEEPER_NOTES and is intentionally gitignored (it contains real owner names), "
        "so it must exist alongside tot_collector.py on whatever machine is running it "
        "but is never pulled in by git. Copy it over (e.g. via scp) before running."
    ) from e

# Authentication (required for private leagues and historical data)
SWID = os.environ["TOT_SWID"]
ESPN_S2 = os.environ["TOT_S2"]

# Year range
START_YEAR = 2014  # confirmed league start year
# END_YEAR is set dynamically. During the NFL offseason (Jan-Aug) we cap at
# the prior year since the new season hasn't started yet. During the active
# season (Sep-Dec) we use the current year.
_now = datetime.now()
END_YEAR = _now.year if _now.month >= 9 else _now.year - 1

class ESPNDataCollectorV2:
    def __init__(self, league_id, start_year, end_year, swid=None, espn_s2=None):
        self.league_id = league_id
        self.start_year = start_year
        self.end_year = end_year
        self.swid = swid
        self.espn_s2 = espn_s2
        self.all_seasons = []
        # Manual owner-name corrections keyed by (team_id, year), for teams where
        # ESPN's API returns an empty team.owners array for that season (so
        # get_owner_name_from_team() would otherwise return None). None have been
        # needed for this league so far; add entries here if the end-of-run
        # warning reports a team with no owner.
        self.OWNER_NAME_CORRECTIONS = {}
        # Lookup maps built once after seasons are fetched (_build_owner_maps)
        self.leagues = {}       # year -> League
        self.team_owner = {}    # (year, team_id) -> owner name (corrections applied)
        self.owner_team = {}    # (year, owner) -> Team (first match wins)
        # Memoized successful _direct_api responses; cleared per season in
        # fetch_keeper_data_by_year to keep memory bounded on the Pi.
        self._api_cache = {}
        # Bench points gathered during the matchup pass: year -> {team_id: total}
        self._bench_points = {}
        # Raw schedule data per (year, frozenset of the two team ids): list of
        # {period, tier, weeks, scores{teamId: [per-week]}} - see fetch_schedule_data
        self.sched_entries = {}
        # Weeks whose weekly API sweep still failed after retries, per year -
        # keeper points for that year may be undercounted (see fetch_keeper_data_by_year)
        self._sweep_failed = {}
        # Data-quality warnings collected during the run (printed in a block at the
        # very end and, if TOT_WARNINGS_FILE is set, written there for the notifier)
        self.warnings = []

    def fetch_all_seasons(self):
        """Fetch data for all seasons using espn-api library.

        Each season is retried up to MAX_FETCH_ATTEMPTS times. Expired or
        invalid ESPN cookies (ESPNAccessDenied) are never retried - they
        propagate to main(), which exits with code 2. Returns True only if
        EVERY season in the configured range was fetched: a partial set would
        silently understate all-time records, so the caller aborts instead
        of publishing it.
        """
        print(f"\n{'='*80}")
        print(" Fetching Season Data")
        print(f"{'='*80}")

        failed_years = []

        for year in range(self.start_year, self.end_year + 1):
            league = None
            for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
                try:
                    suffix = f" (attempt {attempt}/{MAX_FETCH_ATTEMPTS})" if attempt > 1 else ""
                    print(f"\n  Fetching {year} season...{suffix}")

                    league = League(
                        league_id=self.league_id,
                        year=year,
                        espn_s2=self.espn_s2,
                        swid=self.swid
                    )
                    break
                except ESPNAccessDenied:
                    raise
                except Exception as e:
                    print(f"    ✗ Error fetching {year} (attempt {attempt}/{MAX_FETCH_ATTEMPTS}): {e}")
                    if attempt < MAX_FETCH_ATTEMPTS:
                        time.sleep(5 * attempt)

            if league is None:
                failed_years.append(year)
                continue

            print(f"    ✓ League: {league.settings.name}")
            print(f"    ✓ Teams: {len(league.teams)}")
            print(f"    ✓ Weeks: {league.current_week if hasattr(league, 'current_week') else 'N/A'}")

            self.all_seasons.append({
                'year': year,
                'league': league
            })

        if failed_years:
            print(f"\n  ✗ Could not fetch season(s): {failed_years}")
            return False

        print(f"\n  ✓ Successfully fetched {len(self.all_seasons)} seasons")
        self._build_owner_maps()
        return True

    def fetch_schedule_data(self):
        """Fetch ESPN's raw schedule for every season (one request each) and
        keep, for every matchup, its real matchup period, bracket tier, the
        scoring weeks it covers and each team's score in each of those weeks
        (pointsByScoringPeriod). This is what lets two-week playoff matchups
        (2014-2019) be split into their real single-week scores.
        Returns False if any season's schedule can't be fetched."""
        print(f"\n{'='*80}")
        print(" Fetching Per-Week Scores (raw schedule)")
        print(f"{'='*80}")
        self.sched_entries = {}
        for season in self.all_seasons:
            year = season['year']
            data = None
            for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
                data = self._direct_api(year, "mMatchupScore", "&view=mScoreboard")
                if data and data.get('schedule'):
                    break
                print(f"    ⚠️  {year}: schedule fetch failed (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
                if attempt < MAX_FETCH_ATTEMPTS:
                    time.sleep(3 * attempt)
            if not data or not data.get('schedule'):
                print(f"  ✗ Could not fetch the schedule for {year}")
                return False

            count = 0
            for m in data['schedule']:
                home = m.get('home') or {}
                away = m.get('away') or {}
                hid, aid = home.get('teamId'), away.get('teamId')
                if hid is None or aid is None:
                    continue    # bye / empty side
                h_pb = {int(k): v for k, v in (home.get('pointsByScoringPeriod') or {}).items()}
                a_pb = {int(k): v for k, v in (away.get('pointsByScoringPeriod') or {}).items()}
                weeks = sorted(h_pb)
                if not weeks or weeks != sorted(a_pb):
                    continue    # not played yet / no weekly data
                self.sched_entries.setdefault((year, frozenset((hid, aid))), []).append({
                    'period': m.get('matchupPeriodId'),
                    'tier': m.get('playoffTierType') or 'NONE',
                    'weeks': weeks,
                    'scores': {hid: [h_pb[w] for w in weeks], aid: [a_pb[w] for w in weeks]},
                    # ESPN's matchup total includes any scoring adjustment (a dues
                    # bonus/penalty, say) but pointsByScoringPeriod does not
                    'adjustments': {hid: home.get('adjustment') or 0.0, aid: away.get('adjustment') or 0.0},
                })
                count += 1
            # Raw responses are large (cumulativeScore blobs); free them now
            self._api_cache.pop((year, "mMatchupScore", "&view=mScoreboard"), None)
            print(f"  ✓ {year}: {count} matchups with weekly scores")
        return True

    def apply_weekly_scores(self, all_matchups):
        """Attach per-week scores to every matchup and collapse duplicates.

        After this, every matchup is exactly ONE record whose 'score' is the
        matchup total (the combined score for a two-week matchup) and which
        also carries:
          weeks            scoring weeks it covers, e.g. [13, 14] (or [5])
          isTwoWeek        True for a two-week matchup
          home/away.weeklyScores   per-week scores, aligned with 'weeks'
          matchupPeriodId  ESPN's real matchup period
          playoffType      ESPN's bracket tier (WINNERS_BRACKET, ...)
        'week' becomes the first scoring week for every year.

        2019 playoffs came from box_scores once per week, giving two identical
        rows per two-week matchup; those are collapsed here (dropped rows are
        counted in the log). Missing or inconsistent data aborts the run
        (exit 1) rather than publishing wrong numbers.
        """
        print(f"\n{'='*80}")
        print(" Applying Per-Week Scores to Matchups")
        print(f"{'='*80}")
        result = []
        seen = set()
        collapsed = 0
        problems = []

        for m in all_matchups:
            year = m['year']
            hid, aid = m['home']['teamId'], m['away']['teamId']
            entries = self.sched_entries.get((year, frozenset((hid, aid))), [])
            if year < 2019:
                # team.schedule order == ESPN matchup period
                entry = next((e for e in entries if e['period'] == m['week']), None)
            else:
                # box_scores are per scoring week
                entry = next((e for e in entries if m['week'] in e['weeks']), None)
            if entry is None:
                problems.append(f"{year} week {m['week']}: no raw schedule entry for teams {hid}/{aid}")
                continue

            key = (year, entry['period'], frozenset((hid, aid)))
            if key in seen:
                collapsed += 1
                continue
            seen.add(key)

            for side, tid in (('home', hid), ('away', aid)):
                weekly = list(entry['scores'][tid])
                total = m[side]['score']
                adjustment = entry['adjustments'].get(tid) or 0.0
                gap = total - sum(weekly)
                if abs(gap) > WEEKLY_TOLERANCE:
                    if adjustment and abs(gap - adjustment) <= WEEKLY_TOLERANCE:
                        # The gap is exactly ESPN's scoring adjustment (e.g. a week-1
                        # dues bonus), which is part of the official score.
                        if len(weekly) == 1:
                            weekly = [total]        # that week's official score
                        else:
                            self._record_warning(
                                f"{year} matchup period {entry['period']} (team ID {tid}): a score adjustment of "
                                f"{adjustment:+.2f} sits in a two-week matchup and can't be assigned to a week - "
                                f"the weekly scores exclude it, the matchup total includes it")
                    else:
                        msg = (f"{year} week {m['week']} {side}: weekly scores sum to {sum(weekly):.2f} "
                               f"but matchup total is {total:.2f}"
                               f"{f' (ESPN adjustment {adjustment:+.2f})' if adjustment else ''}")
                        if year < self.end_year:
                            problems.append(msg)
                        else:
                            # Current season: live scores can shift slightly between
                            # the two requests. Trust the matchup total.
                            print(f"    ⚠️  {msg} (current season - using the matchup total)")
                            self._record_warning(f"{msg} (current season - used the matchup total)")
                            if len(weekly) == 1:
                                weekly = [total]
                m[side]['weeklyScores'] = weekly
                m[side]['adjustment'] = adjustment

            m['weeks'] = list(entry['weeks'])
            m['isTwoWeek'] = len(entry['weeks']) > 1
            m['week'] = entry['weeks'][0]
            m['matchupPeriodId'] = entry['period']
            m['playoffType'] = entry['tier']
            m['id'] = f"{year}_{m['week']}_{hid}_{aid}"
            result.append(m)

        if problems:
            print(f"\n  ✗ {len(problems)} matchup(s) could not be matched to per-week scores:")
            for line in problems[:10]:
                print(f"     {line}")
            print("  Aborting so inconsistent data is not published.")
            sys.exit(1)

        two_week = sum(1 for m in result if m['isTwoWeek'])
        print(f"  ✓ {len(result)} matchups ({two_week} two-week), {collapsed} duplicate rows collapsed")
        return result

    def _get_members(self, league):
        """Return league.members as an id -> member dict ({} if unavailable)."""
        members = {}
        if hasattr(league, 'members') and league.members:
            try:
                if hasattr(league.members, 'items'):
                    members = league.members
                else:
                    members = {m.id: m for m in league.members}
            except (AttributeError, TypeError):
                members = {}
        return members

    def _build_owner_maps(self):
        """Build the year/team/owner lookup maps once, so every stat method
        resolves owners the same way (including OWNER_NAME_CORRECTIONS)
        instead of re-deriving it inline."""
        self.leagues = {}
        self.team_owner = {}
        self.owner_team = {}
        for season in self.all_seasons:
            year = season['year']
            league = season['league']
            self.leagues[year] = league
            members = self._get_members(league)
            for team in league.teams:
                owner = self.OWNER_NAME_CORRECTIONS.get(
                    (team.team_id, year),
                    self.get_owner_name_from_team(team, members))
                self.team_owner[(year, team.team_id)] = owner
                if not owner:
                    msg = (f"{year}: no owner found for team ID {team.team_id} ({team.team_name}) - "
                           f"its games will be SKIPPED in owner/head-to-head stats. "
                           f"Add it to OWNER_NAME_CORRECTIONS if ESPN returns no owner for it.")
                    print(f"  ⚠️  {msg}")
                    self._record_warning(msg)
                else:
                    if str(owner).startswith('{') and str(owner).endswith('}'):
                        msg = (f"{year}: team ID {team.team_id} ({team.team_name}) has an unresolved ESPN "
                               f"member ID as its owner ({owner}) - it will show up as a separate 'owner'.")
                        print(f"  ⚠️  {msg}")
                        self._record_warning(msg)
                    self.owner_team.setdefault((year, owner), team)

    def _record_warning(self, msg):
        """Remember a data-quality warning for the end-of-run summary."""
        self.warnings.append(msg)

    def _warnings_path(self):
        return os.environ.get('TOT_WARNINGS_FILE')

    def _clear_warnings_file(self):
        """Remove last run's warnings file at the start of a run, so a stale
        file can never be mistaken for this run's warnings."""
        path = self._warnings_path()
        if path and os.path.exists(path):
            os.remove(path)

    def report_warnings(self):
        """Print every collected warning in one block at the very end of the
        run (so it is in the trailing log lines) and, if TOT_WARNINGS_FILE is
        set, write the same text there for the weekly notification email.
        No file is written when there are no warnings."""
        print(f"\n{'='*80}")
        if not self.warnings:
            print(" ✓ Run finished with no warnings")
            print(f"{'='*80}")
            return
        print(f" ⚠️  RUN FINISHED WITH {len(self.warnings)} WARNING(S)")
        print(f"{'='*80}")
        for w in self.warnings:
            print(f"  - {w}")
        path = self._warnings_path()
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, 'w') as f:
                f.write(f"{len(self.warnings)} warning(s) from the collector run:\n")
                for w in self.warnings:
                    f.write(f"- {w}\n")
            print(f"  (also written to {path})")

    def warn_missing_owners(self, all_matchups):
        """Matchups with a side that has no owner are silently dropped from
        the owner and head-to-head stats. List every affected team (year,
        team ID, weeks) so a dropped owner is visible in the run log."""
        missing = defaultdict(list)
        for m in all_matchups:
            for side in ('home', 'away'):
                if not m[side].get('owner'):
                    missing[(m['year'], m[side]['teamId'], m[side]['teamName'])].append(m['week'])
        if not missing:
            return
        print(f"\n{'='*80}")
        print(" ⚠️  WARNING: matchups with no owner (skipped in owner/head-to-head stats)")
        print(f"{'='*80}")
        for (year, team_id, name), weeks in sorted(missing.items()):
            print(f"  ⚠️  {year}: team ID {team_id} ({name}) - weeks {sorted(weeks)}")
            self._record_warning(f"{year}: team ID {team_id} ({name}) - weeks {sorted(weeks)} skipped in owner/head-to-head stats (no owner)")
        total = sum(len(w) for w in missing.values())
        print(f"  {total} team-games affected. Fix the owner lookup (OWNER_NAME_CORRECTIONS) and rerun.")

    def _final_week(self, league):
        """Last scoring week of the season (regular season + playoffs), taken
        from league.settings.matchup_periods (matchup period -> scoring
        periods). Falls back to the old reg_season_count + 4 bound (17 if
        reg_season_count is unavailable) if the attribute is missing or
        looks wrong."""
        reg = getattr(league.settings, 'reg_season_count', None)
        fallback = reg + 4 if reg else 17
        try:
            derived = max(int(w) for periods in league.settings.matchup_periods.values() for w in periods)
            if reg and derived < reg:
                return fallback
            return derived
        except Exception:
            return fallback

    def get_owner_name_from_team(self, team, members):
        """Extract owner display name from team object"""
        if not hasattr(team, 'owners') or not team.owners:
            return None
        
        owner_data = team.owners[0]
        
        if isinstance(owner_data, dict):
            if 'firstName' in owner_data and 'lastName' in owner_data:
                first = owner_data.get('firstName', '').strip()
                last = owner_data.get('lastName', '').strip()
                if first or last:
                    return f"{first} {last}".strip()
            
            if 'displayName' in owner_data:
                return owner_data['displayName']
            
            owner_id = owner_data.get('id') or owner_data.get('ownerId')
            if owner_id and owner_id in members:
                member = members[owner_id]
                if hasattr(member, 'firstName') and hasattr(member, 'lastName'):
                    first = getattr(member, 'firstName', '').strip()
                    last = getattr(member, 'lastName', '').strip()
                    if first or last:
                        return f"{first} {last}".strip()
                if hasattr(member, 'display_name'):
                    return member.display_name
            
            return owner_id or str(owner_data)
        else:
            owner_id = owner_data
            
            if owner_id in members:
                member = members[owner_id]
                if hasattr(member, 'firstName') and hasattr(member, 'lastName'):
                    first = getattr(member, 'firstName', '').strip()
                    last = getattr(member, 'lastName', '').strip()
                    if first or last:
                        return f"{first} {last}".strip()
                if hasattr(member, 'display_name'):
                    return member.display_name
            
            return str(owner_id)
            
    def process_all_matchups(self):
        """Process all matchups from all seasons"""
        print(f"\n{'='*80}")
        print(" Processing Matchups")
        print(f"{'='*80}")
        
        all_matchups = []
        
        for season_data in self.all_seasons:
            year = season_data['year']
            league = season_data['league']
            
            print(f"  Processing {year}...")
            week_count = 0
            
            if year < 2019:
                processed_matchups = set()
                
                for team in league.teams:
                    team_owner = self.team_owner.get((year, team.team_id))
                    
                    for week_idx in range(len(team.schedule)):
                        opponent = team.schedule[week_idx]
                        opponent_owner = self.team_owner.get((year, opponent.team_id))
                        week = week_idx + 1
                        
                        matchup_key = f"{year}_{week}_{min(team.team_id, opponent.team_id)}_{max(team.team_id, opponent.team_id)}"
                        
                        if matchup_key in processed_matchups:
                            continue
                        
                        processed_matchups.add(matchup_key)
                        
                        is_home = team.team_id < opponent.team_id
                        
                        if is_home:
                            home_team = team
                            away_team = opponent
                            home_score = team.scores[week_idx]
                            away_score = opponent.scores[week_idx]
                            home_owner = team_owner
                            away_owner = opponent_owner
                        else:
                            home_team = opponent
                            away_team = team
                            home_score = opponent.scores[week_idx]
                            away_score = team.scores[week_idx]
                            home_owner = opponent_owner
                            away_owner = team_owner
                        
                        if home_score > away_score:
                            winner = 'HOME'
                        elif away_score > home_score:
                            winner = 'AWAY'
                        else:
                            winner = 'TIE'
                        
                        is_playoff = False
                        if hasattr(league.settings, 'reg_season_count'):
                            is_playoff = week > league.settings.reg_season_count
                        else:
                            is_playoff = week > 13
                        
                        clean_matchup = {
                            'id': matchup_key,
                            'year': year,
                            'week': week,
                            'matchupPeriodId': week,
                            'isPlayoff': is_playoff,
                            'playoffType': 'WINNERS_BRACKET' if is_playoff else 'NONE',
                            'home': {
                                'teamId': home_team.team_id,
                                'teamName': self._clean_team_name(home_team.team_name),
                                'teamAbbrev': home_team.team_abbrev,
                                'owner': home_owner,
                                'score': home_score
                            },
                            'away': {
                                'teamId': away_team.team_id,
                                'teamName': self._clean_team_name(away_team.team_name),
                                'teamAbbrev': away_team.team_abbrev,
                                'owner': away_owner,
                                'score': away_score
                            },
                            'winner': winner
                        }
                        
                        all_matchups.append(clean_matchup)
                        week_count += 1
                
                print(f"    ✓ Loaded {week_count} matchups from {year} (via team schedules)")
            
            else:
                reg_count = getattr(league.settings, 'reg_season_count', 14)
                total_weeks = self._completed_weeks(league, self._final_week(league))
                print(f"    Scanning weeks 1-{total_weeks} (regular season: {reg_count})")
                
                for week in range(1, total_weeks + 1):
                    try:
                        box_scores = league.box_scores(week)
                        
                        if not box_scores:
                            continue
                        
                        for matchup in box_scores:
                            if not hasattr(matchup, 'home_team') or not hasattr(matchup, 'away_team'):
                                continue
                            
                            if not matchup.home_team or not matchup.away_team:
                                continue
                            
                            home_owner = self.team_owner.get((year, matchup.home_team.team_id))
                            away_owner = self.team_owner.get((year, matchup.away_team.team_id))
                            
                            is_playoff = False
                            if hasattr(league.settings, 'reg_season_count'):
                                is_playoff = week > league.settings.reg_season_count
                            else:
                                is_playoff = week > 14
                            
                            home_score = matchup.home_score
                            away_score = matchup.away_score

                            # Bench points are gathered here from the box scores
                            # already being fetched, so the bench stat needs no
                            # additional API calls (see
                            # calculate_bench_points_by_season). Regular season
                            # weeks only.
                            if week <= reg_count:
                                year_bench = self._bench_points.setdefault(year, defaultdict(float))
                                for side_team, side_lineup in (
                                    (matchup.home_team, matchup.home_lineup),
                                    (matchup.away_team, matchup.away_lineup),
                                ):
                                    for player in side_lineup or []:
                                        if getattr(player, 'slot_position', None) == 'BE':
                                            year_bench[side_team.team_id] += getattr(player, 'points', 0) or 0

                            # Skip matchups that haven't actually finished
                            # yet. league.current_week can advance (e.g. on
                            # ESPN's weekly reset) before any games in that
                            # week have been played, so _completed_weeks()
                            # alone doesn't guarantee real data. A 0-0 check
                            # only catches the case where literally nobody
                            # has played - it does NOT catch a week that's
                            # partially through (e.g. only Thursday night's
                            # game has been played), which still returns
                            # real-looking, non-zero, but incomplete live
                            # scores. Use _matchup_is_final() instead, which
                            # checks each starter's actual game_played flag.
                            if not self._matchup_is_final(matchup):
                                continue

                            clean_matchup = {
                                'id': f"{year}_{week}_{matchup.home_team.team_id}_{matchup.away_team.team_id}",
                                'year': year,
                                'week': week,
                                'matchupPeriodId': week,
                                'isPlayoff': is_playoff,
                                'playoffType': 'WINNERS_BRACKET' if is_playoff else 'NONE',
                                'home': {
                                    'teamId': matchup.home_team.team_id,
                                    'teamName': self._clean_team_name(matchup.home_team.team_name),
                                    'teamAbbrev': matchup.home_team.team_abbrev,
                                    'owner': home_owner,
                                    'score': home_score
                                },
                                'away': {
                                    'teamId': matchup.away_team.team_id,
                                    'teamName': self._clean_team_name(matchup.away_team.team_name),
                                    'teamAbbrev': matchup.away_team.team_abbrev,
                                    'owner': away_owner,
                                    'score': away_score
                                },
                                'winner': 'HOME' if home_score > away_score else 'AWAY' if away_score > home_score else 'TIE'
                            }
                            
                            all_matchups.append(clean_matchup)
                            week_count += 1
                    
                    except ESPNAccessDenied:
                        raise
                    except Exception as e:
                        print(f"    ⚠️  {year} week {week}: skipped due to error: {e}")
                        self._record_warning(f"{year} week {week}: matchups skipped due to error: {e}")
                        continue
                
                print(f"    ✓ Loaded {week_count} matchups from {year} (via box_scores)")
        
        print(f"\n  ✓ Processed {len(all_matchups)} total matchups")
        return all_matchups

    def get_playoff_teams_by_year(self):
        """Determine which teams made playoffs each year based on actual playoff performance"""
        playoff_teams = defaultdict(set)
        
        print(f"\n  Detecting playoff teams by year...")
        
        for season_data in self.all_seasons:
            year = season_data['year']
            league = season_data['league']
            
            year_playoff_owners = []
            
            for team in league.teams:
                if hasattr(team, 'final_standing') and team.final_standing:
                    # This league has always run a 4-team, 2-round playoff bracket.
                    # We still prefer league.settings.playoff_team_count when ESPN
                    # exposes it; LEAGUE_PLAYOFF_TEAM_COUNT is only used if it's missing.
                    num_playoff_teams = getattr(
                        league.settings, 'playoff_team_count', LEAGUE_PLAYOFF_TEAM_COUNT
                    )
                    
                    if team.final_standing <= num_playoff_teams:
                        owner = self.team_owner.get((year, team.team_id))
                        
                        if owner:
                            playoff_teams[year].add(owner)
                            year_playoff_owners.append(f"{owner} (standing: {team.final_standing})")
            
            if year_playoff_owners:
                print(f"    {year}: {len(year_playoff_owners)} playoff teams - {', '.join(year_playoff_owners[:3])}...")
        
        return playoff_teams

    def calculate_team_stats(self, all_matchups):
        """Calculate comprehensive statistics BY OWNER"""
        print(f"\n{'='*80}")
        print(" Calculating Owner Statistics")
        print(f"{'='*80}")
        
        owner_matchups = defaultdict(list)
        owner_info = {}
        
        for matchup in all_matchups:
            home_owner = matchup['home'].get('owner')
            away_owner = matchup['away'].get('owner')
            
            if not home_owner or not away_owner:
                continue
            
            if home_owner not in owner_info:
                owner_info[home_owner] = {
                    'owner': home_owner,
                    'currentTeamName': matchup['home']['teamName'],
                    'currentTeamAbbrev': matchup['home']['teamAbbrev'],
                    'currentTeamId': matchup['home']['teamId'],
                    'firstYear': matchup['year'],
                    'lastYear': matchup['year'],
                    'teamNames': set(),
                    'logoUrl': None
                }
            
            if away_owner not in owner_info:
                owner_info[away_owner] = {
                    'owner': away_owner,
                    'currentTeamName': matchup['away']['teamName'],
                    'currentTeamAbbrev': matchup['away']['teamAbbrev'],
                    'currentTeamId': matchup['away']['teamId'],
                    'firstYear': matchup['year'],
                    'lastYear': matchup['year'],
                    'teamNames': set()
                }
            
            owner_info[home_owner]['lastYear'] = max(owner_info[home_owner]['lastYear'], matchup['year'])
            owner_info[home_owner]['firstYear'] = min(owner_info[home_owner]['firstYear'], matchup['year'])
            owner_info[home_owner]['teamNames'].add(matchup['home']['teamName'])
            
            if matchup['year'] >= owner_info[home_owner]['lastYear']:
                owner_info[home_owner]['currentTeamName'] = matchup['home']['teamName']
                owner_info[home_owner]['currentTeamAbbrev'] = matchup['home']['teamAbbrev']
                owner_info[home_owner]['currentTeamId'] = matchup['home']['teamId']
            
            owner_info[away_owner]['lastYear'] = max(owner_info[away_owner]['lastYear'], matchup['year'])
            owner_info[away_owner]['firstYear'] = min(owner_info[away_owner]['firstYear'], matchup['year'])
            owner_info[away_owner]['teamNames'].add(matchup['away']['teamName'])
            
            if matchup['year'] >= owner_info[away_owner]['lastYear']:
                owner_info[away_owner]['currentTeamName'] = matchup['away']['teamName']
                owner_info[away_owner]['currentTeamAbbrev'] = matchup['away']['teamAbbrev']
                owner_info[away_owner]['currentTeamId'] = matchup['away']['teamId']
            
            owner_matchups[home_owner].append({
                **matchup,
                'isHome': True,
                'teamScore': matchup['home']['score'],
                'opponentScore': matchup['away']['score'],
                'teamWeekly': matchup['home']['weeklyScores'],
                'opponentWeekly': matchup['away']['weeklyScores'],
                'opponentOwner': away_owner,
                'opponentName': matchup['away']['teamName']
            })
            
            owner_matchups[away_owner].append({
                **matchup,
                'isHome': False,
                'teamScore': matchup['away']['score'],
                'opponentScore': matchup['home']['score'],
                'teamWeekly': matchup['away']['weeklyScores'],
                'opponentWeekly': matchup['home']['weeklyScores'],
                'opponentOwner': home_owner,
                'opponentName': matchup['home']['teamName']
            })
            
        playoff_teams_by_year = self.get_playoff_teams_by_year()
        
        owner_stats = {}
                
        for owner, matches in owner_matchups.items():
            if owner not in owner_info:
                continue
            
            matches.sort(key=lambda m: (m['year'], m['week']))
            
            info = owner_info[owner]
            info['foundedYear'] = info['firstYear']
            info['lastActiveYear'] = info['lastYear']
            info['isActive'] = info['lastYear'] == self.end_year
            info['totalSeasons'] = info['lastYear'] - info['firstYear'] + 1
            info['teamId'] = info['currentTeamId']
            info['teamName'] = info['currentTeamName']
            info['abbrev'] = info['currentTeamAbbrev']
            info['allTeamNames'] = list(info['teamNames'])
            del info['teamNames']
            info['playoffYears'] = list({year for year in playoff_teams_by_year if owner in playoff_teams_by_year[year]})
            
            stats = self.calculate_single_team_stats(owner, matches, info, owner_matchups)
            owner_stats[owner] = stats

        # Attach each owner's logo from their most recent season
        for owner, stats in owner_stats.items():
            team = self.owner_team.get((stats['lastActiveYear'], owner))
            if team is not None:
                stats['logoUrl'] = team.logo_url if hasattr(team, 'logo_url') and team.logo_url else None
        
        print(f"  ✓ Calculated stats for {len(owner_stats)} owners")
        return owner_stats
        
    def _sniff_image_ext(self, data):
        """File extension from the image's own bytes (ESPN labels JPEGs
        'image/jpg', so the content type isn't trusted). None if not an image."""
        if data.startswith(b'\xff\xd8\xff'):
            return '.jpg'
        if data.startswith(b'\x89PNG'):
            return '.png'
        if data.startswith(b'GIF8'):
            return '.gif'
        if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
            return '.webp'
        head = data[:1000].lstrip().lower()
        if head.startswith(b'<svg') or (head.startswith(b'<?xml') and b'<svg' in head):
            return '.svg'
        return None

    def _download_image(self, url):
        """GET an image with the collector's ESPN cookies, retrying transient
        failures like _get_with_retry. Returns the bytes or None."""
        cookies = {"SWID": self.swid, "espn_s2": self.espn_s2}
        headers = {"Accept": "image/*,*/*;q=0.8"}
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            retryable = True
            try:
                resp = requests.get(url, cookies=cookies, headers=headers, timeout=20)
                if resp.status_code == 200:
                    return resp.content
                retryable = resp.status_code >= 500
                print(f"    ⚠️  logo {url.rsplit('/', 1)[-1]}: HTTP {resp.status_code} (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
            except Exception as e:
                print(f"    ⚠️  logo {url.rsplit('/', 1)[-1]}: {e} (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
            if not retryable:
                return None
            if attempt < MAX_FETCH_ATTEMPTS:
                time.sleep(2 * attempt)
        return None

    def localize_logos(self, owner_stats):
        """Logos uploaded directly to ESPN (mystique-api.fantasy.espn.com) need
        an ESPN login, so a visitor's browser gets a 401 and the page shows
        nothing. Download them with the collector's cookies into output/logos/
        and point logoUrl at the local copy. Files are named by ESPN's image id,
        so an unchanged logo is never downloaded again (no weekly commit churn)
        and a replaced logo gets a new file; files no owner uses are deleted.
        Logos on public hosts (ESPN clipart, imgur) are left as they are. If a
        download fails the remote URL is kept and a warning is recorded."""
        print(f"\n{'='*80}")
        print(" Localizing ESPN-hosted team logos")
        print(f"{'='*80}")
        os.makedirs(LOGO_DIR, exist_ok=True)
        keep = set()
        downloaded = reused = failed = 0
        for owner, stats in sorted(owner_stats.items()):
            url = stats.get('logoUrl')
            if not url or urlparse(url).netloc != AUTH_LOGO_HOST:
                continue
            key = re.sub(r'[^A-Za-z0-9_-]', '', url.rstrip('/').rsplit('/', 1)[-1]) \
                or hashlib.sha1(url.encode()).hexdigest()
            existing = [f for f in os.listdir(LOGO_DIR) if os.path.splitext(f)[0] == key]
            if existing:
                filename = existing[0]
                reused += 1
            else:
                data = self._download_image(url)
                ext = self._sniff_image_ext(data) if data and len(data) <= MAX_LOGO_BYTES else None
                if not ext:
                    failed += 1
                    msg = f"logo for {owner} could not be downloaded ({url}) - keeping the ESPN URL, which visitors cannot load"
                    print(f"  ⚠️  {msg}")
                    self._record_warning(msg)
                    continue
                filename = key + ext
                with open(os.path.join(LOGO_DIR, filename), 'wb') as f:
                    f.write(data)
                downloaded += 1
                print(f"  ✓ {owner}: saved {filename} ({len(data)} bytes)")
            keep.add(filename)
            stats['logoUrl'] = f"{LOGO_DIR.replace(os.sep, '/')}/{filename}"
        removed = 0
        for f in os.listdir(LOGO_DIR):
            if f not in keep:
                os.remove(os.path.join(LOGO_DIR, f))
                removed += 1
        print(f"  ✓ {downloaded} downloaded, {reused} already saved, {failed} failed, {removed} unused file(s) removed")
        return owner_stats

    def calculate_head_to_head_stats(self, all_matchups, owner_stats):
        """Calculate head-to-head records between all owners"""
        print(f"\n{'='*80}")
        print(" Calculating Head-to-Head Records")
        print(f"{'='*80}")
        
        h2h_data = {}
        all_owners = list(owner_stats.keys())
        
        for owner in all_owners:
            h2h_data[owner] = {}
            
            for opponent in all_owners:
                if owner == opponent:
                    continue
                
                matchups = []
                for m in all_matchups:
                    home_owner = m['home'].get('owner')
                    away_owner = m['away'].get('owner')
                    
                    if (home_owner == owner and away_owner == opponent) or \
                       (home_owner == opponent and away_owner == owner):
                        matchups.append(m)
                
                if not matchups:
                    continue
                
                wins = 0
                losses = 0
                ties = 0
                points_for = []
                points_against = []
                weekly_for = []
                weekly_against = []
                win_margins = []
                current_streak = {'type': None, 'length': 0}
                longest_win_streak = 0
                current_win_streak = 0
                
                matchups.sort(key=lambda x: (x['year'], x['week']))
                
                for match in matchups:
                    if match['home']['owner'] == owner:
                        my_score = match['home']['score']
                        opp_score = match['away']['score']
                        weekly_for.extend(match['home']['weeklyScores'])
                        weekly_against.extend(match['away']['weeklyScores'])
                        i_won = match['winner'] == 'HOME'
                        i_lost = match['winner'] == 'AWAY'
                    else:
                        my_score = match['away']['score']
                        opp_score = match['home']['score']
                        weekly_for.extend(match['away']['weeklyScores'])
                        weekly_against.extend(match['home']['weeklyScores'])
                        i_won = match['winner'] == 'AWAY'
                        i_lost = match['winner'] == 'HOME'
                    
                    points_for.append(my_score)
                    points_against.append(opp_score)
                    
                    if i_won:
                        wins += 1
                        margin = my_score - opp_score
                        win_margins.append(margin)
                        current_win_streak += 1
                        longest_win_streak = max(longest_win_streak, current_win_streak)
                        
                        if current_streak['type'] == 'WIN':
                            current_streak['length'] += 1
                        else:
                            current_streak = {'type': 'WIN', 'length': 1}
                    elif i_lost:
                        losses += 1
                        current_win_streak = 0
                        
                        if current_streak['type'] == 'LOSS':
                            current_streak['length'] += 1
                        else:
                            current_streak = {'type': 'LOSS', 'length': 1}
                    else:
                        ties += 1
                        current_win_streak = 0
                        current_streak = {'type': None, 'length': 0}
                
                h2h_record = {
                    'wins': wins,
                    'losses': losses,
                    'ties': ties,
                    'winPercentage': wins / (wins + losses) if (wins + losses) > 0 else 0,
                    'totalGames': len(matchups),
                    'pointsFor': sum(points_for),
                    'pointsAgainst': sum(points_against),
                    'avgPointsFor': sum(weekly_for) / len(weekly_for) if weekly_for else 0,
                    'avgPointsAgainst': sum(weekly_against) / len(weekly_against) if weekly_against else 0,
                    'highestScore': max(weekly_for) if weekly_for else 0,
                    'lowestScore': min(weekly_for) if weekly_for else 0,
                    'biggestWin': max(win_margins) if win_margins else 0,
                    'closestWin': min(win_margins) if win_margins else 0,
                    'longestWinStreak': longest_win_streak,
                    'currentStreak': current_streak,
                    'matchups': [self._slim_matchup(m) for m in matchups]
                }
                
                h2h_data[owner][opponent] = h2h_record
        
        print(f"  ✓ Calculated head-to-head for {len(all_owners)} owners")
        return h2h_data

    def _slim_matchup(self, m):
        """Matchup record trimmed to the fields head-to-head.html reads
        (drops id, playoffType and teamAbbrev, which only matchups.json needs)."""
        def side(t):
            return {'teamId': t['teamId'], 'teamName': t['teamName'],
                    'owner': t['owner'], 'score': t['score'],
                    'weeklyScores': t['weeklyScores']}
        return {
            'year': m['year'],
            'week': m['week'],
            'weeks': m['weeks'],
            'isTwoWeek': m['isTwoWeek'],
            'matchupPeriodId': m['matchupPeriodId'],
            'isPlayoff': m['isPlayoff'],
            'home': side(m['home']),
            'away': side(m['away']),
            'winner': m['winner']
        }

    def calculate_single_team_stats(self, owner, matches, info, all_owner_data):
        """Calculate all stats for a single team"""
        stats = {
            **info,
            'seasonsPlayed': len(set(m['year'] for m in matches)),
            'totalGames': len(matches)
        }
        
        playoff_years = set(info.get('playoffYears', []))

        regular_season = [m for m in matches if not m['isPlayoff'] or m['year'] not in playoff_years]
        playoffs = [m for m in matches if m['isPlayoff'] and m['year'] in playoff_years]

        stats['regularSeason'] = self.calculate_record(regular_season)
        stats['playoffs'] = self.calculate_record(playoffs)
        stats['overall'] = self.calculate_record(matches)
        stats['playoffAppearances'] = len(playoff_years)
        
        # Calculate championships
        stats['championships'] = 0
        stats['championshipAppearances'] = 0
        championship_years = []

        for year in playoff_years:
            team = self.owner_team.get((year, owner))
            if team is not None and getattr(team, 'final_standing', None) is not None:
                if team.final_standing == 1:
                    stats['championships'] += 1
                    stats['championshipAppearances'] += 1
                    championship_years.append(year)
                elif team.final_standing == 2:
                    stats['championshipAppearances'] += 1

        stats['championshipYears'] = championship_years
        
        # Track acquisitions/moves and trades per season
        acquisitions_by_season = {}
        total_trades = None

        for year in set(m['year'] for m in matches):
            team = self.owner_team.get((year, owner))
            if team is None:
                continue

            # Check for acquisitions attribute
            if hasattr(team, 'acquisitions'):
                acquisitions_by_season[year] = team.acquisitions
            elif hasattr(team, 'moves'):
                acquisitions_by_season[year] = team.moves
            elif hasattr(team, 'transactions'):
                acquisitions_by_season[year] = len(team.transactions) if isinstance(team.transactions, list) else team.transactions

            # Check for trades (only reliable 2019+)
            if year >= 2019 and hasattr(team, 'trades') and isinstance(team.trades, int):
                total_trades = (total_trades or 0) + team.trades

        if acquisitions_by_season:
            most_moves_year = max(acquisitions_by_season.items(), key=lambda x: x[1])
            stats['mostMovesSeason'] = most_moves_year[1]
            stats['mostMovesSeasonYear'] = most_moves_year[0]
        else:
            stats['mostMovesSeason'] = None
            stats['mostMovesSeasonYear'] = None
        
        # Totals are summed over matchup totals (each matchup once). Averages
        # are per WEEK played, since a two-week matchup holds two weeks of
        # points; highest/lowest are true single-week scores.
        all_scores = [m['teamScore'] for m in matches]
        weekly_scores = [w for m in matches for w in m['teamWeekly']]
        weeks_played = len(weekly_scores)
        stats['weeksPlayed'] = weeks_played
        stats['totalPointsFor'] = sum(all_scores)
        stats['totalPointsAgainst'] = sum(m['opponentScore'] for m in matches)
        stats['avgPointsFor'] = stats['totalPointsFor'] / weeks_played if weeks_played else 0
        stats['avgPointsAgainst'] = stats['totalPointsAgainst'] / weeks_played if weeks_played else 0
        stats['highestScore'] = max(weekly_scores) if weekly_scores else 0
        stats['lowestScore'] = min(weekly_scores) if weekly_scores else 0
        stats['totalTrades'] = total_trades
        
        wins = [m for m in matches if self.is_winner(m)]
        losses = [m for m in matches if self.is_loser(m)]
        
        if wins:
            win_margins = [m['teamScore'] - m['opponentScore'] for m in wins]
            stats['biggestWin'] = max(win_margins)
            stats['closestWin'] = min(win_margins)
        else:
            stats['biggestWin'] = 0
            stats['closestWin'] = 0
        
        if losses:
            loss_margins = [m['opponentScore'] - m['teamScore'] for m in losses]
            stats['biggestLoss'] = max(loss_margins)
            stats['closestLoss'] = min(loss_margins)
        else:
            stats['biggestLoss'] = 0
            stats['closestLoss'] = 0
        
        streaks = self.calculate_streaks(matches)
        stats['longestWinStreak'] = streaks['longestWin']
        stats['longestLossStreak'] = streaks['longestLoss']
        stats['currentStreak'] = streaks['current']
        stats['yearlyRecords'] = self.calculate_yearly_records(matches)
        
        # Calculate best and worst opponents (only active owners with 3+ games)
        opponent_records = defaultdict(lambda: {'wins': 0, 'losses': 0, 'games': 0})

        for match in matches:
            opp = match['opponentOwner']
            opponent_records[opp]['games'] += 1
            
            if self.is_winner(match):
                opponent_records[opp]['wins'] += 1
            elif self.is_loser(match):
                opponent_records[opp]['losses'] += 1

        qualified_opponents = {}
        for opp, rec in opponent_records.items():
            if rec['games'] >= 3 and opp in all_owner_data:
                opp_matches = all_owner_data.get(opp, [])
                is_active = any(m['year'] == self.end_year for m in opp_matches)
                if is_active:
                    qualified_opponents[opp] = rec

        if qualified_opponents:
            best_opp = max(qualified_opponents.items(), 
                           key=lambda x: x[1]['wins'] / x[1]['games'] if x[1]['games'] > 0 else 0)
            stats['bestOpponent'] = best_opp[0]
            stats['bestOpponentRecord'] = f"{best_opp[1]['wins']}-{best_opp[1]['losses']}"
            stats['bestOpponentWinPct'] = best_opp[1]['wins'] / best_opp[1]['games'] if best_opp[1]['games'] > 0 else 0
            
            worst_opp = min(qualified_opponents.items(), 
                            key=lambda x: x[1]['wins'] / x[1]['games'] if x[1]['games'] > 0 else 0)
            stats['worstOpponent'] = worst_opp[0]
            stats['worstOpponentRecord'] = f"{worst_opp[1]['wins']}-{worst_opp[1]['losses']}"
            stats['worstOpponentWinPct'] = worst_opp[1]['wins'] / worst_opp[1]['games'] if worst_opp[1]['games'] > 0 else 0
        else:
            stats['bestOpponent'] = None
            stats['worstOpponent'] = None
        
        # Calculate best/worst seasons
        complete_seasons = {}
        for year, record in stats['yearlyRecords'].items():
            total_games = record['regularSeasonWins'] + record['regularSeasonLosses']
            # A past season is always complete; the current one only counts
            # once all of THAT season's regular-season games (ESPN's
            # reg_season_count for that year) have been played.
            season_league = self.leagues.get(year)
            season_reg_games = getattr(season_league.settings, 'reg_season_count', None) if season_league else None
            season_complete = year < self.end_year or (
                season_reg_games is not None and total_games >= season_reg_games)
            if season_complete:
                if total_games > 0:
                    reg_win_pct = record['regularSeasonWins'] / total_games
                    complete_seasons[year] = {
                        'winPercentage': reg_win_pct,
                        'wins': record['regularSeasonWins'],
                        'losses': record['regularSeasonLosses']
                    }

        if complete_seasons:
            best_season = max(complete_seasons.items(), key=lambda x: (x[1]['winPercentage'], x[1]['wins']))
            worst_season = min(complete_seasons.items(), key=lambda x: (x[1]['winPercentage'], -x[1]['wins']))
            most_wins = max(complete_seasons.items(), key=lambda x: x[1]['wins'])
            most_losses = max(complete_seasons.items(), key=lambda x: x[1]['losses'])
            
            stats['bestSeasonWinPct'] = best_season[1]['winPercentage']
            stats['bestSeasonYear'] = best_season[0]
            stats['bestSeasonWins'] = best_season[1]['wins']
            stats['bestSeasonLosses'] = best_season[1]['losses']
            
            stats['worstSeasonWinPct'] = worst_season[1]['winPercentage']
            stats['worstSeasonYear'] = worst_season[0]
            stats['worstSeasonWins'] = worst_season[1]['wins']
            stats['worstSeasonLosses'] = worst_season[1]['losses']
            
            stats['mostWinsSeason'] = most_wins[1]['wins']
            stats['mostWinsSeasonYear'] = most_wins[0]
            stats['mostLossesSeason'] = most_losses[1]['losses']
            stats['mostLossesSeasonYear'] = most_losses[0]
        else:
            stats['bestSeasonWinPct'] = 0
            stats['bestSeasonLosses'] = 0
            stats['bestSeasonWins'] = 0
            stats['worstSeasonWinPct'] = 0
            stats['worstSeasonWins'] = 0
            stats['worstSeasonLosses'] = 0
            stats['mostWinsSeason'] = 0
            stats['mostLossesSeason'] = 0
        
        return stats
    
    def calculate_record(self, matches):
        """Calculate W-L-T record"""
        wins = sum(1 for m in matches if self.is_winner(m))
        losses = sum(1 for m in matches if self.is_loser(m))
        ties = sum(1 for m in matches if m['winner'] == 'TIE')
        
        total = len(matches)
        win_pct = wins / total if total > 0 else 0
        
        return {
            'wins': wins,
            'losses': losses,
            'ties': ties,
            'winPercentage': round(win_pct, 3)
        }
    
    def is_winner(self, match):
        """Check if team won"""
        if match['winner'] == 'TIE' or match['winner'] == 'UNDECIDED':
            return False
        
        if match['isHome']:
            return match['winner'] == 'HOME'
        else:
            return match['winner'] == 'AWAY'
    
    def is_loser(self, match):
        """Check if team lost"""
        if match['winner'] == 'TIE' or match['winner'] == 'UNDECIDED':
            return False
        
        if match['isHome']:
            return match['winner'] == 'AWAY'
        else:
            return match['winner'] == 'HOME'
    
    def calculate_streaks(self, matches):
        """Calculate streaks"""
        if not matches:
            return {'longestWin': 0, 'longestLoss': 0, 'current': {'type': None, 'length': 0}}
        
        longest_win = 0
        longest_loss = 0
        current_streak = 0
        current_type = None
        
        for match in matches:
            if match['winner'] == 'TIE':
                continue
            
            is_win = self.is_winner(match)
            
            if is_win:
                if current_type == 'WIN':
                    current_streak += 1
                else:
                    current_type = 'WIN'
                    current_streak = 1
                longest_win = max(longest_win, current_streak)
            else:
                if current_type == 'LOSS':
                    current_streak += 1
                else:
                    current_type = 'LOSS'
                    current_streak = 1
                longest_loss = max(longest_loss, current_streak)
        
        return {
            'longestWin': longest_win,
            'longestLoss': longest_loss,
            'current': {'type': current_type, 'length': current_streak}
        }
    
    def calculate_yearly_records(self, matches):
        """Calculate yearly records"""
        yearly = defaultdict(list)
        
        for match in matches:
            yearly[match['year']].append(match)
        
        records = {}
        for year, year_matches in yearly.items():
            playoff_games = [m for m in year_matches if m['isPlayoff']]
            regular_games = [m for m in year_matches if not m['isPlayoff']]
            
            record = self.calculate_record(year_matches)
            
            scores = [m['teamScore'] for m in year_matches]
            record['pointsFor'] = sum(scores)
            record['pointsAgainst'] = sum(m['opponentScore'] for m in year_matches)
            record['avgPointsFor'] = record['pointsFor'] / sum(len(m['weeks']) for m in year_matches)
            record['madePlayoffs'] = len(playoff_games) > 0
            
            if regular_games:
                reg_record = self.calculate_record(regular_games)
                record['regularSeasonWins'] = reg_record['wins']
                record['regularSeasonLosses'] = reg_record['losses']
            else:
                record['regularSeasonWins'] = 0
                record['regularSeasonLosses'] = 0
            
            # Whether this regular season is finished (a past season always is;
            # the current one only once all of ESPN's reg_season_count games
            # have been played) - lets teams.html draw an in-progress season
            # differently instead of showing a partial record as a collapse.
            season_league = self.leagues.get(year)
            reg_games = getattr(season_league.settings, 'reg_season_count', None) if season_league else None
            record['regularSeasonGames'] = reg_games
            record['seasonComplete'] = year < self.end_year or (
                reg_games is not None and len(regular_games) >= reg_games)
            
            records[year] = record
        
        return records
    
    def calculate_superlatives(self, all_matchups):
        """Calculate superlatives"""
        print(f"\n{'='*80}")
        print(" Calculating Superlatives")
        print(f"{'='*80}")
        
        regular_season = [m for m in all_matchups if not m['isPlayoff']]
        
        all_time_streaks = self.calculate_all_time_streaks(all_matchups)
        regular_season_streaks = self.calculate_all_time_streaks(regular_season)
        all_time_weekly = self.calculate_weekly_performance(all_matchups)
        regular_season_weekly = self.calculate_weekly_performance(regular_season)
        
        superlatives = {
            'allTime': {
                **self.calculate_single_superlatives(all_matchups, "All-Time"),
                **all_time_streaks,
                **all_time_weekly
            },
            'regularSeason': {
                **self.calculate_single_superlatives(regular_season, "Regular Season"),
                **regular_season_streaks,
                **regular_season_weekly,
                **self.calculate_seasonal_stats(regular_season)
            }
        }
        
        return superlatives
    
    def _weekly_score_entries(self, m):
        """One entry per team per SCORING WEEK of a matchup (two entries per
        week; a two-week matchup yields four). 'week' is the actual scoring
        week and the opponent score is that week's score, so every entry is a
        true single-week result."""
        entries = []
        for i, wk in enumerate(m['weeks']):
            for side, other in (('home', 'away'), ('away', 'home')):
                entries.append({
                    'score': m[side]['weeklyScores'][i],
                    'team': m[side]['teamName'],
                    'teamId': m[side]['teamId'],
                    'owner': m[side]['owner'],
                    'opponent': m[other]['teamName'],
                    'opponentScore': m[other]['weeklyScores'][i],
                    'year': m['year'],
                    'week': wk,
                    'matchupPeriodId': m['matchupPeriodId']
                })
        return entries

    def _margin_record(self, m):
        """Winner/loser/margin for a decided matchup (matchup totals)."""
        winner_data = m['home'] if m['winner'] == 'HOME' else m['away']
        loser_data = m['away'] if m['winner'] == 'HOME' else m['home']
        return {
            'margin': abs(m['home']['score'] - m['away']['score']),
            'winner': winner_data['teamName'],
            'winnerOwner': winner_data['owner'],
            'winnerTeamId': winner_data['teamId'],
            'winnerScore': winner_data['score'],
            'loser': loser_data['teamName'],
            'loserScore': loser_data['score'],
            'year': m['year'],
            'week': m['week'],
            'weeks': m['weeks'],
            'isTwoWeek': m['isTwoWeek'],
            'matchupPeriodId': m['matchupPeriodId']
        }

    def _combined_record(self, m):
        """Both teams' matchup totals added together (matchup totals)."""
        return {
            'total': m['home']['score'] + m['away']['score'],
            'team1': m['home']['teamName'],
            'owner1': m['home']['owner'],
            'score1': m['home']['score'],
            'team2': m['away']['teamName'],
            'owner2': m['away']['owner'],
            'score2': m['away']['score'],
            'teamId': m['home']['teamId'],
            'year': m['year'],
            'week': m['week'],
            'weeks': m['weeks'],
            'isTwoWeek': m['isTwoWeek'],
            'matchupPeriodId': m['matchupPeriodId']
        }

    def _two_week_companion(self, main, best_two_week, field, higher_is_better):
        """The two-week-matchup record to show beneath a single-week record,
        or None when it would not beat the single-week record (a two-week
        matchup that isn't a bigger/lower record isn't worth a line)."""
        if not best_two_week:
            return None
        if main is None:
            return best_two_week
        if higher_is_better and best_two_week[field] > main[field]:
            return best_two_week
        if not higher_is_better and best_two_week[field] < main[field]:
            return best_two_week
        return None

    def calculate_single_superlatives(self, matchups, label):
        """Calculate superlatives for a set of matchups.

        Highest/lowest score and the Top 10 lists use true single-week scores
        (a two-week matchup contributes its two real weekly scores). Closest
        Win uses all matchups. Biggest Blowout and Highest/Lowest Scoring Game
        compare matchup totals, so the main record uses single-week matchups
        only, with a '...TwoWeek' companion when a two-week matchup beats it.
        """
        if not matchups:
            return {}
        
        print(f"  Calculating {label} superlatives...")
        
        all_scores = []
        for m in matchups:
            all_scores.extend(self._weekly_score_entries(m))
        
        non_zero_scores = [s for s in all_scores if s['score'] > 0]
        sorted_high = sorted(all_scores, key=lambda x: x['score'], reverse=True)
        sorted_low = sorted(non_zero_scores, key=lambda x: x['score'])
        
        superlatives = {
            'highestScore': sorted_high[0] if sorted_high else None,
            'lowestScore': sorted_low[0] if sorted_low else None,
            'top10HighestScores': sorted_high[:10],
            'top10LowestScores': sorted_low[:10]
        }
        
        margins = [self._margin_record(m) for m in matchups if m['winner'] != 'TIE']
        single_margins = [r for r in margins if not r['isTwoWeek']]
        two_margins = [r for r in margins if r['isTwoWeek']]
        if margins:
            superlatives['closestWin'] = min(margins, key=lambda x: x['margin'])
        if single_margins:
            superlatives['biggestBlowout'] = max(single_margins, key=lambda x: x['margin'])
        superlatives['biggestBlowoutTwoWeek'] = self._two_week_companion(
            superlatives.get('biggestBlowout'),
            max(two_margins, key=lambda x: x['margin']) if two_margins else None,
            'margin', True)

        combined = [self._combined_record(m) for m in matchups]
        single_combined = [c for c in combined if not c['isTwoWeek']]
        two_combined = [c for c in combined if c['isTwoWeek']]
        non_zero_single = [c for c in single_combined if c['score1'] > 0 and c['score2'] > 0]
        non_zero_two = [c for c in two_combined if c['score1'] > 0 and c['score2'] > 0]

        superlatives['highestScoringGame'] = max(single_combined, key=lambda x: x['total']) if single_combined else None
        superlatives['highestScoringGameTwoWeek'] = self._two_week_companion(
            superlatives['highestScoringGame'],
            max(two_combined, key=lambda x: x['total']) if two_combined else None,
            'total', True)
        superlatives['lowestScoringGame'] = min(non_zero_single, key=lambda x: x['total']) if non_zero_single else None
        superlatives['lowestScoringGameTwoWeek'] = self._two_week_companion(
            superlatives['lowestScoringGame'],
            min(non_zero_two, key=lambda x: x['total']) if non_zero_two else None,
            'total', False)
        
        return superlatives

    def calculate_all_time_streaks(self, matchups):
        """Calculate longest win and loss streaks across all owners"""
        owner_games = defaultdict(list)
        for m in matchups:
            owner_games[m['home']['owner']].append({
                'year': m['year'],
                'week': m['week'],
                'period': m['matchupPeriodId'],
                'won': m['winner'] == 'HOME',
                'lost': m['winner'] == 'AWAY',
                'teamName': m['home']['teamName']
            })
            owner_games[m['away']['owner']].append({
                'year': m['year'],
                'week': m['week'],
                'period': m['matchupPeriodId'],
                'won': m['winner'] == 'AWAY',
                'lost': m['winner'] == 'HOME',
                'teamName': m['away']['teamName']
            })
        
        best_win, best_loss = self._compute_owner_streaks(owner_games)
        win_ties, win_length = self._streak_ties_at_max(best_win)
        loss_ties, loss_length = self._streak_ties_at_max(best_loss)

        empty_streak = {'owner': None, 'length': 0, 'teamName': None, 'startYear': None, 'startWeek': None, 'endYear': None, 'endWeek': None, 'isOngoing': False}

        longest_win_streak = {
            **(win_ties[0] if win_ties else empty_streak),
            'owners': win_ties,
            'tied': len(win_ties) > 1
        }
        longest_loss_streak = {
            **(loss_ties[0] if loss_ties else empty_streak),
            'owners': loss_ties,
            'tied': len(loss_ties) > 1
        }

        return {
            'longestWinStreak': longest_win_streak,
            'longestLossStreak': longest_loss_streak
        }

    def calculate_weekly_performance(self, matchups):
        """Weekly high/low scorer counts (every scoring week, using true
        single-week scores).

        Note: this league has no "consolation dollar" mechanic (no payout
        to the 2nd-highest scorer in a week if they lost), so that tracking
        is not done here.
        """
        weekly_scores = defaultdict(list)
        for m in matchups:
            for i, wk in enumerate(m['weeks']):
                key = (m['year'], wk)
                weekly_scores[key].append({
                    'owner': m['home']['owner'],
                    'score': m['home']['weeklyScores'][i]
                })
                weekly_scores[key].append({
                    'owner': m['away']['owner'],
                    'score': m['away']['weeklyScores'][i]
                })
        
        high_scorer_count = defaultdict(int)
        low_scorer_count = defaultdict(int)
        
        for scores in weekly_scores.values():
            if not scores:
                continue
            sorted_scores = sorted(scores, key=lambda x: x['score'], reverse=True)
            high_scorer_count[sorted_scores[0]['owner']] += 1
            low_scorer_count[sorted_scores[-1]['owner']] += 1
        
        high_owners, high_count = self._counter_ties_at_max(high_scorer_count)
        low_owners, low_count = self._counter_ties_at_max(low_scorer_count)

        return {
            'mostWeeklyHighScores': {
                'owner': high_owners[0] if high_owners else None,
                'owners': high_owners,
                'count': high_count,
                'tied': len(high_owners) > 1
            },
            'mostWeeklyLowScores': {
                'owner': low_owners[0] if low_owners else None,
                'owners': low_owners,
                'count': low_count,
                'tied': len(low_owners) > 1
            }
        }

    def calculate_seasonal_stats(self, matchups):
        """Calculate seasonal records like most points against (regular season only)"""
        season_stats = defaultdict(lambda: defaultdict(lambda: {'pointsAgainst': 0, 'teamName': None, 'owner': None}))
        
        # Only count regular season games
        for m in matchups:
            if m['isPlayoff']:
                continue
            
            home_owner = m['home'].get('owner')
            away_owner = m['away'].get('owner')
            
            # Skip if owner is missing
            if not home_owner or not away_owner:
                continue
                
            year = m['year']
            
            # Use owner as the key to avoid duplicate counting
            season_stats[home_owner][year]['pointsAgainst'] += m['away']['score']
            season_stats[home_owner][year]['teamName'] = m['home']['teamName']
            season_stats[home_owner][year]['owner'] = home_owner
            
            season_stats[away_owner][year]['pointsAgainst'] += m['home']['score']
            season_stats[away_owner][year]['teamName'] = m['away']['teamName']
            season_stats[away_owner][year]['owner'] = away_owner
        
        all_seasons = []
        for owner, years in season_stats.items():
            for year, stats in years.items():
                # Only add if we have valid data
                if stats['owner'] and stats['teamName']:
                    all_seasons.append({
                        'owner': owner,
                        'teamName': stats['teamName'],
                        'year': year,
                        'pointsAgainst': stats['pointsAgainst']
                    })
        
        sorted_high_pa = sorted(all_seasons, key=lambda x: x['pointsAgainst'], reverse=True)
        sorted_low_pa = sorted(all_seasons, key=lambda x: x['pointsAgainst'])
        
        # Debug print top 5
        print(f"  Top 5 highest PA seasons:")
        for i, s in enumerate(sorted_high_pa[:5]):
            print(f"    {i+1}. {s['owner']} ({s['teamName']}) - {s['year']}: {s['pointsAgainst']:.2f}")
        
        return {
            'mostPointsAgainstSeasons': sorted_high_pa[:5],
            'fewestPointsAgainstSeasons': sorted_low_pa[:5]
        }

    def calculate_season_stats(self, all_matchups, skip_bench=False, keepers_by_year=None):
        """Calculate stats for each individual season"""
        print(f"\n{'='*80}")
        print(" Calculating Season-by-Season Statistics")
        print(f"{'='*80}")
        
        # Calculate bench points (skip if flag is set)
        if skip_bench:
            print(f"  ⚠️  Skipping bench points calculation")
            bench_points = {}
        else:
            bench_points = self.calculate_bench_points_by_season()
        
        season_data = {}

        
        # Group matchups by year
        matchups_by_year = defaultdict(list)
        for m in all_matchups:
            matchups_by_year[m['year']].append(m)
        
        for year in sorted(matchups_by_year.keys()):
            year_matchups = matchups_by_year[year]
            regular_season = [m for m in year_matchups if not m['isPlayoff']]
            playoffs = [m for m in year_matchups if m['isPlayoff']]
            
            print(f"  Processing {year}...")
            
            # Basic superlatives for this season. Highest/lowest score use true
            # single-week scores; closest win uses all matchups; biggest
            # blowout uses single-week matchups with a two-week companion
            # (same rules as the all-time superlatives).
            all_scores = []
            for m in year_matchups:
                all_scores.extend(self._weekly_score_entries(m))
            
            sorted_scores = sorted(all_scores, key=lambda x: x['score'], reverse=True)
            non_zero = [s for s in all_scores if s['score'] > 0]
            sorted_low = sorted(non_zero, key=lambda x: x['score'])
            
            matchup_margins = [self._margin_record(m) for m in year_matchups if m['winner'] != 'TIE']
            single_margins = [r for r in matchup_margins if not r['isTwoWeek']]
            two_margins = [r for r in matchup_margins if r['isTwoWeek']]
            season_blowout = max(single_margins, key=lambda x: x['margin']) if single_margins else None
            season_blowout_two_week = self._two_week_companion(
                season_blowout,
                max(two_margins, key=lambda x: x['margin']) if two_margins else None,
                'margin', True)
            
            # Streaks for this season
            owner_games = defaultdict(list)
            for m in year_matchups:
                owner_games[m['home']['owner']].append({
                    'week': m['week'],
                    'period': m['matchupPeriodId'],
                    'won': m['winner'] == 'HOME',
                    'lost': m['winner'] == 'AWAY',
                    'teamName': m['home']['teamName']
                })
                owner_games[m['away']['owner']].append({
                    'week': m['week'],
                    'period': m['matchupPeriodId'],
                    'won': m['winner'] == 'AWAY',
                    'lost': m['winner'] == 'HOME',
                    'teamName': m['away']['teamName']
                })

            season_best_win, season_best_loss = self._compute_owner_streaks(owner_games)
            season_win_ties, season_win_length = self._streak_ties_at_max(season_best_win)
            season_loss_ties, season_loss_length = self._streak_ties_at_max(season_best_loss)

            season_win_owners = [d['owner'] for d in season_win_ties]
            season_loss_owners = [d['owner'] for d in season_loss_ties]

            # Per-owner matchup periods of each streak, for the scoreboard
            # links (the page links by matchup period; a two-week playoff
            # matchup is one period, so it counts as one game in the streak).
            season_win_streaks = [
                {'owner': d['owner'], 'weeks': d['periods']}
                for d in season_win_ties
            ]
            season_loss_streaks = [
                {'owner': d['owner'], 'weeks': d['periods']}
                for d in season_loss_ties
            ]

            longest_win = {
                'owner': season_win_owners[0] if season_win_owners else None,
                'length': season_win_length,
                'owners': season_win_owners,
                'tied': len(season_win_owners) > 1,
                'streaks': season_win_streaks
            }
            longest_loss = {
                'owner': season_loss_owners[0] if season_loss_owners else None,
                'length': season_loss_length,
                'owners': season_loss_owners,
                'tied': len(season_loss_owners) > 1,
                'streaks': season_loss_streaks
            }
            
            # Weekly high/low scorers
            weekly_scores = defaultdict(list)
            for m in regular_season:
                key = m['week']
                weekly_scores[key].append({
                    'owner': m['home']['owner'],
                    'team': m['home']['teamName'],
                    'teamId': m['home']['teamId'],
                    'period': m['matchupPeriodId'],
                    'score': m['home']['score'],
                    'won': m['winner'] == 'HOME'
                })
                weekly_scores[key].append({
                    'owner': m['away']['owner'],
                    'team': m['away']['teamName'],
                    'teamId': m['away']['teamId'],
                    'period': m['matchupPeriodId'],
                    'score': m['away']['score'],
                    'won': m['winner'] == 'AWAY'
                })

            high_scorer_count = defaultdict(int)
            low_scorer_count = defaultdict(int)
            high_games = []   # every weekly high/low game, for the page's per-week links
            low_games = []

            # Note: this league has no weekly high-score payout and no
            # consolation-dollar mechanic, so those payout calculations
            # (present in the Monday Morning Tears version of this script)
            # are not done here. Only the season-end playoff payout
            # (below) applies to this league.

            for week, scores in weekly_scores.items():
                if not scores:
                    continue
                
                sorted_week = sorted(scores, key=lambda x: x['score'], reverse=True)
                highest = sorted_week[0]
                lowest = sorted_week[-1]
                
                high_scorer_count[highest['owner']] += 1
                low_scorer_count[lowest['owner']] += 1
                high_games.append({'owner': highest['owner'], 'week': week, 'matchupPeriodId': highest['period'],
                                   'teamId': highest['teamId'], 'score': highest['score']})
                low_games.append({'owner': lowest['owner'], 'week': week, 'matchupPeriodId': lowest['period'],
                                  'teamId': lowest['teamId'], 'score': lowest['score']})

            # NOW calculate the most values AFTER the loop
            season_high_owners, season_high_count = self._counter_ties_at_max(high_scorer_count)
            season_low_owners, season_low_count = self._counter_ties_at_max(low_scorer_count)

            # Playoff payouts - this league had no buy-in before 2023.
            # $5 buy-in added in 2023: 1st = $35, 2nd = $10, 3rd = $5.
            # No documented change to these amounts since 2023; update this
            # table if/when payout amounts change in a future season.
            playoff_payouts = []
            season_league = self.leagues.get(year)

            if year < 2023:
                playoff_amounts = {'1st': 0, '2nd': 0, '3rd': 0}  # No buy-in
            else:  # 2023+
                # Confirmed $35/$10/$5 for all years 2023+ (a $45 figure
                # appears in some 2023 informal notes but was a one-off
                # discrepancy, not the actual payout amount).
                playoff_amounts = {'1st': 35, '2nd': 10, '3rd': 5}

            if season_league:
                for team in season_league.teams:
                    owner = self.team_owner.get((year, team.team_id))
                    if hasattr(team, 'final_standing') and team.final_standing:
                        if team.final_standing == 1:
                            playoff_payouts.append({
                                'owner': owner,
                                'amount': playoff_amounts['1st'],
                                'place': '1st'
                            })
                        elif team.final_standing == 2:
                            playoff_payouts.append({
                                'owner': owner,
                                'amount': playoff_amounts['2nd'],
                                'place': '2nd'
                            })
                        elif team.final_standing == 3:
                            playoff_payouts.append({
                                'owner': owner,
                                'amount': playoff_amounts['3rd'],
                                'place': '3rd'
                            })

            season_data[year] = {
                'year': year,
                'highestScore': sorted_scores[0] if sorted_scores else None,
                'lowestScore': sorted_low[0] if sorted_low else None,
                'closestWin': min(matchup_margins, key=lambda x: x['margin']) if matchup_margins else None,
                'biggestBlowout': season_blowout,
                'biggestBlowoutTwoWeek': season_blowout_two_week,
                'longestWinStreak': longest_win,
                'longestLossStreak': longest_loss,
                'mostWeeklyHighScores': {
                    'owner': season_high_owners[0] if season_high_owners else None,
                    'owners': season_high_owners,
                    'count': season_high_count,
                    'tied': len(season_high_owners) > 1,
                    'games': sorted((g for g in high_games if g['owner'] in season_high_owners),
                                    key=lambda g: g['week'])
                },
                'mostWeeklyLowScores': {
                    'owner': season_low_owners[0] if season_low_owners else None,
                    'owners': season_low_owners,
                    'count': season_low_count,
                    'tied': len(season_low_owners) > 1,
                    'games': sorted((g for g in low_games if g['owner'] in season_low_owners),
                                    key=lambda g: g['week'])
                },
                'bestBenchWarmers': bench_points.get(year),
                'playoffPayouts': playoff_payouts,
                'keeperAnalysis': {
                    'keepers': (keepers_by_year or {}).get(year, []),
                },
            }
        
        return season_data

    def calculate_player_loyalty_stats(self, owner_stats):
        """Calculate which player each owner rostered the most seasons"""
        print(f"\n  Calculating player loyalty stats...")
        
        loyalty_stats = {}
        
        for season_data in self.all_seasons:
            year = season_data['year']
            league = season_data['league']
            
            for team in league.teams:
                owner = self.team_owner.get((year, team.team_id))
                if not owner or owner not in owner_stats:
                    continue
                
                if owner not in loyalty_stats:
                    loyalty_stats[owner] = defaultdict(list)
                
                # Track all players on roster
                for player in team.roster:
                    player_name = player.name
                    loyalty_stats[owner][player_name].append(year)
        
        # Find most loyal player for each owner
        for owner in loyalty_stats:
            player_seasons = {player: len(years) for player, years in loyalty_stats[owner].items()}
            if player_seasons:
                most_loyal_player = max(player_seasons.items(), key=lambda x: x[1])
                owner_stats[owner]['mostLoyalPlayer'] = most_loyal_player[0]
                owner_stats[owner]['mostLoyalPlayerSeasons'] = most_loyal_player[1]
                owner_stats[owner]['mostLoyalPlayerYears'] = sorted(loyalty_stats[owner][most_loyal_player[0]])
            else:
                owner_stats[owner]['mostLoyalPlayer'] = None
                owner_stats[owner]['mostLoyalPlayerSeasons'] = 0
        
        return owner_stats
        
    def calculate_bench_points_by_season(self):
        """Best bench-points team per season (2019+; earlier seasons have no
        reliable bench data). Totals are gathered during process_all_matchups
        from the box scores it already fetches, so this makes no API calls."""
        print(f"\n  Calculating bench points by season...")
        
        bench_points_by_season = {}
        
        for season_data in self.all_seasons:
            year = season_data['year']
            league = season_data['league']
            
            # Skip years before 2019 (bench data not reliable)
            if year < 2019:
                continue
            
            print(f"    Processing {year} bench scores...")
            
            year_totals = self._bench_points.get(year, {})
            year_bench_points = {}
            
            for team in league.teams:
                owner = self.team_owner.get((year, team.team_id))
                if not owner:
                    continue
                
                total_bench_points = year_totals.get(team.team_id, 0)
                
                if total_bench_points > 0:
                    year_bench_points[owner] = {
                        'owner': owner,
                        'teamName': self._clean_team_name(team.team_name),
                        'benchPoints': total_bench_points
                    }
            
            if year_bench_points:
                best_bench = max(year_bench_points.values(), key=lambda x: x['benchPoints'])
                bench_points_by_season[year] = best_bench
                print(f"      ✓ Best bench: {best_bench['owner']} - {best_bench['benchPoints']:.2f} pts")
            else:
                bench_points_by_season[year] = None
                print(f"      ✗ No bench data available")
                self._record_warning(f"{year}: no bench data available")
        
        return bench_points_by_season

    def _summarize_two_week(self, rows):
        """Counts and rates for a list of two-week matchup rows."""
        decided = [r for r in rows if r['week1Leader'] is not None]
        comebacks = [r for r in decided if r['outcome'] == 'COMEBACK']
        held = [r for r in decided if r['outcome'] == 'HELD']
        tied_overall = [r for r in decided if r['outcome'] == 'TIED']
        largest = max(comebacks, key=lambda r: r['week1Lead']) if comebacks else None
        return {
            'matchups': len(rows),
            'week1Ties': len(rows) - len(decided),
            'decidedAfterWeek1': len(decided),
            'comebacks': len(comebacks),
            'held': len(held),
            'tiedOverall': len(tied_overall),
            'comebackRate': len(comebacks) / len(decided) if decided else None,
            'avgWeek1Lead': sum(r['week1Lead'] for r in decided) / len(decided) if decided else None,
            'avgLeadOvercome': sum(r['week1Lead'] for r in comebacks) / len(comebacks) if comebacks else None,
            'avgLeadHeld': sum(r['week1Lead'] for r in held) / len(held) if held else None,
            'largestComeback': ({
                'year': largest['year'], 'weeks': largest['weeks'],
                'lead': largest['week1Lead'], 'bracket': largest['bracket'],
                'leader': largest[largest['week1Leader']]['owner'],
                'winner': largest[largest['winner']]['owner'],
            } if largest else None),
        }

    def analyze_two_week_playoffs(self, all_matchups):
        """Was the two-week playoff format ever decisive? For every two-week
        matchup (all brackets, flagged by bracket tier), compare who led after
        week 1 with who won overall. A COMEBACK is a team that trailed after
        week 1 and won on the two-week total. Built from the true per-week
        scores, so it covers every season ESPN recorded as two-week."""
        print(f"\n{'='*80}")
        print(" Analyzing Two-Week Playoff Matchups")
        print(f"{'='*80}")

        two_week = [m for m in all_matchups if m['isTwoWeek']]
        periods_by_year = defaultdict(set)
        for m in two_week:
            periods_by_year[m['year']].add(m['matchupPeriodId'])

        rows = []
        for m in sorted(two_week, key=lambda x: (x['year'], x['matchupPeriodId'], x['id'])):
            if len(m['weeks']) != 2:
                print(f"  ⚠️  Skipping {m['year']} matchup spanning {len(m['weeks'])} weeks")
                self._record_warning(f"{m['year']}: a two-week analysis matchup spans {len(m['weeks'])} weeks and was skipped")
                continue
            year = m['year']
            round_no = sorted(periods_by_year[year]).index(m['matchupPeriodId']) + 1
            last_round = len(periods_by_year[year])
            h, a = m['home'], m['away']
            h1, h2 = h['weeklyScores']
            a1, a2 = a['weeklyScores']

            if h1 == a1:
                leader = None
            else:
                leader = 'home' if h1 > a1 else 'away'
            winner = {'HOME': 'home', 'AWAY': 'away'}.get(m['winner'])
            if leader is None:
                outcome = 'WEEK1_TIE'
            elif winner is None:
                outcome = 'TIED'
            elif winner == leader:
                outcome = 'HELD'
            else:
                outcome = 'COMEBACK'

            trailer = None if leader is None else ('away' if leader == 'home' else 'home')
            week2 = {'home': h2, 'away': a2}
            tier = m['playoffType']
            side = lambda t, w1, w2: {
                'owner': t['owner'], 'teamName': t['teamName'], 'teamId': t['teamId'],
                'week1': w1, 'week2': w2, 'total': t['score']
            }
            rows.append({
                'year': year,
                'matchupPeriodId': m['matchupPeriodId'],
                'weeks': m['weeks'],
                'round': round_no,
                'bracket': tier,
                'isWinnersBracket': tier == 'WINNERS_BRACKET',
                'isChampionship': tier == 'WINNERS_BRACKET' and round_no == last_round,
                'home': side(h, h1, h2),
                'away': side(a, a1, a2),
                'week1Leader': leader,
                'winner': winner,
                'week1Lead': round(abs(h1 - a1), 2),
                'week2Swing': None if leader is None else round(week2[trailer] - week2[leader], 2),
                'finalMargin': round(abs(h['score'] - a['score']), 2),
                'outcome': outcome
            })

        winners = [r for r in rows if r['isWinnersBracket']]
        others = [r for r in rows if not r['isWinnersBracket']]
        by_year = []
        for year in sorted(periods_by_year):
            yr = [r for r in rows if r['year'] == year]
            summary = self._summarize_two_week(yr)
            by_year.append({'year': year, **{k: summary[k] for k in
                            ('matchups', 'week1Ties', 'comebacks', 'held', 'tiedOverall')}})

        result = {
            'summary': {
                'all': self._summarize_two_week(rows),
                'winnersBracket': self._summarize_two_week(winners),
                'otherBrackets': self._summarize_two_week(others)
            },
            'byYear': by_year,
            'matchups': rows,
            'meta': {
                'twoWeekYears': sorted(periods_by_year),
                'oneWeekPlayoffYears': sorted(
                    {m['year'] for m in all_matchups if m['isPlayoff']} - set(periods_by_year))
            }
        }
        total = result['summary']['all']
        print(f"  ✓ {total['matchups']} two-week matchups ({len(winners)} winners' bracket) in "
              f"{len(periods_by_year)} seasons; {total['comebacks']} comebacks")
        return result

    def _matchup_is_final(self, box_score):
        """Whether a box_scores() matchup has actually finished, checked
        directly instead of inferred from league.current_week or a 0-0
        placeholder heuristic.

        ESPN's box_scores() returns live, in-progress scores throughout a
        week - by mid-week these are real, non-zero, plausible-looking
        totals (e.g. after Thursday night's game), not just 0-0
        placeholders. A 0-0 check only catches the case where nobody in
        the matchup has played at all; it does not catch a matchup that's
        partially through the week.

        espn_api's BoxPlayer (confirmed in the installed 0.46.0, and
        present at least as far back as 0.10.0) exposes game_played per
        player: 100 once that player's real NFL game kicked off plus 3
        hours, 0 otherwise. Bye-week players keep the default 100 and
        never block completion. A matchup only counts as final once every
        starter (excluding bench/IR, whose games don't affect the
        displayed score) in both lineups has actually played.
        """
        for player in box_score.home_lineup + box_score.away_lineup:
            if player.slot_position in ('BE', 'IR'):
                continue
            if player.game_played != 100:
                return False
        return True

    def _completed_weeks(self, league, requested_weeks):
        """Cap a per-week loop at the number of weeks that have actually
        started this season, per league.current_week.

        ESPN's per-week endpoints do not reliably return empty data for
        weeks beyond the season's current live week. Confirmed directly
        in the espn-api library source (box_scores()): if the requested
        week is greater than league.current_week, the method silently
        keeps querying the CURRENT scoring/matchup period instead of the
        requested one - it does not raise, return empty, or signal
        anything is wrong. The raw mTransactions2 endpoint (used by
        transactions() and by our direct-API keeper sweeps) shows the
        same symptom: querying a future scoringPeriodId echoes back
        current-period data rather than an empty list.

        Net effect if this isn't guarded: looping range(1, total_weeks+1)
        for the current in-progress season doesn't skip unplayed weeks -
        it duplicates the current week's real games/transactions once
        per remaining week number. Confirmed empirically in Week 1 of
        the 2026 season: fake 17-0 season records and a single real
        drop inflated to 17x in the most-dropped-players stat.

        For a completed past season, league.current_week reflects that
        season's final week, so this cap is a no-op there - it only
        changes behavior for the current, still-in-progress season.
        """
        current_week = getattr(league, 'current_week', None)
        if not current_week or current_week <= 0:
            return requested_weeks
        return min(requested_weeks, current_week)

    def _get_with_retry(self, url, cookies, headers, label):
        """GET a JSON endpoint, retrying transient failures (network errors,
        5xx, unparseable bodies) up to MAX_FETCH_ATTEMPTS times. 4xx responses
        are not retried (and not printed - leagueHistory legitimately has
        nothing for an in-progress season). Returns parsed JSON or None."""
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            retryable = True
            try:
                resp = requests.get(url, cookies=cookies, headers=headers, timeout=20)
                if resp.status_code == 200:
                    return resp.json()
                retryable = resp.status_code >= 500
                if retryable:
                    print(f"    ⚠️  {label}: HTTP {resp.status_code} (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
            except Exception as e:
                print(f"    ⚠️  {label}: {e} (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
            if not retryable:
                return None
            if attempt < MAX_FETCH_ATTEMPTS:
                time.sleep(2 * attempt)
        print(f"    ✗ {label}: giving up after {MAX_FETCH_ATTEMPTS} attempts")
        return None

    def _direct_api(self, year, view, extra_params=""):
        """
        Make a direct request to ESPN's league data.

        Tries the leagueHistory endpoint first - this is what has reliably
        worked for every completed season this collector fetches, since
        ESPN archives a season there once it's over. It does NOT serve a
        season that's still in progress: confirmed live when this league's
        2026 keeper fetch failed here with "Could not fetch mDraftDetail
        for 2026" while every prior year succeeded via this same endpoint
        (same failure mode confirmed for the MMT sibling league).

        Falls back to the live seasons/{year}/segments/0/leagues/{id}
        endpoint when leagueHistory comes back empty - per ESPN API
        references (e.g. github.com/cwendt94/espn-api community docs),
        this is the endpoint that actually serves the current in-progress
        season. In practice this fallback should only ever trigger for
        self.end_year while that season is still ongoing.

        Transient failures are retried (see _get_with_retry). Returns the
        unwrapped response dict, or None on failure. Successful responses
        are memoized by (year, view, extra_params) in self._api_cache so
        repeated sweeps (e.g. the per-keeper fallback) cost no extra calls;
        failures are never cached.
        """
        cache_key = (year, view, extra_params)
        if cache_key in self._api_cache:
            return self._api_cache[cache_key]

        cookies = {"SWID": self.swid, "espn_s2": self.espn_s2}
        headers = {"Accept": "application/json"}

        # 1. leagueHistory - the response is a list containing one snapshot
        history_url = (
            f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
            f"/leagueHistory/{self.league_id}?seasonId={year}&view={view}{extra_params}"
        )
        data = self._get_with_retry(history_url, cookies, headers, f"leagueHistory {year} {view}")
        if isinstance(data, list) and data:
            self._api_cache[cache_key] = data[0]
            return data[0]

        # 2. Live endpoint fallback - the response is a single dict, not a list
        live_url = (
            f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
            f"/seasons/{year}/segments/0/leagues/{self.league_id}?view={view}{extra_params}"
        )
        data = self._get_with_retry(live_url, cookies, headers, f"live endpoint {year} {view}")
        if data:
            self._api_cache[cache_key] = data
            return data
        return None

    def _normalize_name(self, name):
        """Lowercase + strip punctuation for fuzzy player name matching."""
        import re
        return re.sub(r"[^a-z0-9 ]", "", name.lower().strip())

    def _title_case_owner(self, name):
        """Ensure owner names are consistently title-cased."""
        if not name:
            return name
        return " ".join(w.capitalize() for w in name.strip().split())

    def _clean_team_name(self, name):
        """Collapse multiple spaces and strip leading/trailing whitespace
        from team names. ESPN occasionally stores team names with double
        spaces as an artifact of in-app editing (e.g. 'The  Other Team')
        even though the name displays correctly on the ESPN website."""
        import re
        if not name:
            return name
        return re.sub(r' +', ' ', name).strip()

    def _get_player_points_weekly(self, year, reg_season_weeks):
        """
        For years where mRoster appliedStatTotal is zeroed (2018),
        reconstruct per-player season totals by summing weekly
        appliedStatTotal across all regular season scoring periods.

        Returns:
            player_points: { playerId: { name, points, teamId,
                                         droppedMidSeason, lastActiveWeek } }
        """
        # player_id -> { name, teamId, weekly: {week: points} }
        weekly_tracker = {}

        for week in range(1, reg_season_weeks + 1):
            week_data = self._direct_api(
                year, "mRoster",
                extra_params=f"&scoringPeriodId={week}"
            )
            if not week_data:
                self._sweep_failed.setdefault(year, set()).add(week)
                continue
            for team in week_data.get("teams", []):
                team_id = team.get("id")
                for entry in team.get("roster", {}).get("entries", []):
                    pool   = entry.get("playerPoolEntry", {})
                    pid    = pool.get("id") or entry.get("playerId")
                    pts    = pool.get("appliedStatTotal", 0.0) or 0.0
                    name   = pool.get("player", {}).get("fullName", "")
                    if pid not in weekly_tracker:
                        weekly_tracker[pid] = {
                            "name":   name,
                            "teamId": team_id,
                            "weekly": {}
                        }
                    # Only update name/teamId from the first week we see them
                    if not weekly_tracker[pid]["name"] and name:
                        weekly_tracker[pid]["name"] = name
                    weekly_tracker[pid]["weekly"][week] = pts

        # Collapse to season totals + drop detection
        player_points = {}
        for pid, info in weekly_tracker.items():
            weekly = info["weekly"]
            season_total = sum(weekly.values())
            weeks_with_points = [w for w, p in weekly.items() if p > 0]
            last_active = max(weeks_with_points) if weeks_with_points else 0
            dropped = (
                last_active > 0 and
                last_active < reg_season_weeks and
                all(weekly.get(w, 0) == 0
                    for w in range(last_active + 1, reg_season_weeks + 1))
            )
            player_points[pid] = {
                "name":            info["name"],
                "points":          round(season_total, 2),
                "teamId":          info["teamId"],
                "droppedMidSeason": dropped,
                "lastActiveWeek":  last_active if dropped else None,
            }
        return player_points

    def _get_player_points_weekly_via_boxscore(self, year, reg_season_weeks):
        """
        Fallback for years where mRoster's appliedStatTotal is broken at
        the player level (confirmed for 2017 in this league — mRoster
        returns 0.0 for every player every week, but mBoxscore's
        rosterForMatchupPeriod carries real per-player point values).

        IMPORTANT CAVEAT: mBoxscore's rosterForMatchupPeriod only includes
        players in active (non-bench) lineup slots for that matchup, not
        the full roster. This means:
          - A player's score is only captured for weeks they started.
          - We CANNOT distinguish "benched this week" from "not yet
            rostered" or "dropped" the way the mRoster-based sweep can,
            since bench players simply don't appear here at all.
          - droppedMidSeason / lastActiveWeek from this method should be
            treated as "last week we saw them START", not a true drop
            date. A keeper benched for the second half of the season will
            look identical to one who was dropped — this is a real
            information loss versus 2019+, not just a quirk of the method.
            If a specific keeper's case matters (e.g. STEAL/BUST verdict
            looks suspicious), verify manually rather than trusting this
            distinction for 2017 data.

        Returns same shape as _get_player_points_weekly.
        """
        weekly_tracker = {}

        for week in range(1, reg_season_weeks + 1):
            week_data = self._direct_api(
                year, "mBoxscore",
                extra_params=f"&scoringPeriodId={week}"
            )
            if not week_data:
                self._sweep_failed.setdefault(year, set()).add(week)
                continue
            for matchup in week_data.get("schedule", []):
                for side in ("home", "away"):
                    team_side = matchup.get(side, {})
                    team_id = team_side.get("teamId")
                    roster = team_side.get("rosterForMatchupPeriod", {})
                    for entry in roster.get("entries", []):
                        pool = entry.get("playerPoolEntry", {})
                        pid  = pool.get("id") or entry.get("playerId")
                        pts  = pool.get("appliedStatTotal", 0.0) or 0.0
                        name = pool.get("player", {}).get("fullName", "")
                        if pid not in weekly_tracker:
                            weekly_tracker[pid] = {
                                "name":   name,
                                "teamId": team_id,
                                "weekly": {}
                            }
                        if not weekly_tracker[pid]["name"] and name:
                            weekly_tracker[pid]["name"] = name
                        weekly_tracker[pid]["weekly"][week] = pts

        player_points = {}
        for pid, info in weekly_tracker.items():
            weekly = info["weekly"]
            season_total = sum(weekly.values())
            weeks_seen = sorted(weekly.keys())
            last_active = max(weeks_seen) if weeks_seen else 0
            # See caveat above: "dropped" here really means "stopped
            # appearing in the active lineup", which conflates benching
            # with dropping. Flagged as droppedMidSeason so the season
            # stats page can still show a note, but it is a weaker signal
            # than the mRoster-based version used for 2019+.
            dropped = (
                last_active > 0 and last_active < reg_season_weeks
            )
            player_points[pid] = {
                "name":            info["name"],
                "points":          round(season_total, 2),
                "teamId":          info["teamId"],
                "droppedMidSeason": dropped,
                "lastActiveWeek":  last_active if dropped else None,
            }
        return player_points

    def _get_player_points_endofseason(self, year):
        """
        For years where mRoster appliedStatTotal is reliable (2019+),
        fetch the end-of-season snapshot directly.

        Returns same shape as _get_player_points_weekly.
        """
        roster_data = self._direct_api(year, "mRoster")
        if not roster_data:
            return {}

        player_points = {}
        for team in roster_data.get("teams", []):
            team_id = team.get("id")
            for entry in team.get("roster", {}).get("entries", []):
                pool   = entry.get("playerPoolEntry", {})
                pid    = pool.get("id") or entry.get("playerId")
                pts    = pool.get("appliedStatTotal", 0.0) or 0.0
                name   = pool.get("player", {}).get("fullName", "")
                player_points[pid] = {
                    "name":            name,
                    "points":          pts,
                    "teamId":          team_id,
                    "droppedMidSeason": False,
                    "lastActiveWeek":  None,
                }
        return player_points

    def _get_player_points_with_fallback(self, year, player_points,
                                         pid, norm_name, reg_season_weeks):
        """
        For a specific keeper showing zero or missing points, do a targeted
        weekly sweep to get their real season contribution even if dropped.

        Tries mRoster first; if that finds nothing (e.g. a year where
        mRoster's appliedStatTotal is broken at the player level, as
        confirmed for 2017 in this league), falls back to mBoxscore's
        rosterForMatchupPeriod. See _get_player_points_weekly_via_boxscore
        for the bench/drop-detection caveat that applies to the mBoxscore
        path — a player who stops appearing there may have been benched,
        not dropped.

        Returns updated { points, droppedMidSeason, lastActiveWeek } or None.
        """
        weekly_scores = {}
        found_name    = ""

        for week in range(1, reg_season_weeks + 1):
            week_data = self._direct_api(
                year, "mRoster",
                extra_params=f"&scoringPeriodId={week}"
            )
            if not week_data:
                self._sweep_failed.setdefault(year, set()).add(week)
                continue
            for team in week_data.get("teams", []):
                for entry in team.get("roster", {}).get("entries", []):
                    pool  = entry.get("playerPoolEntry", {})
                    e_pid = pool.get("id") or entry.get("playerId")
                    e_name = pool.get("player", {}).get("fullName", "")
                    # Match by ID if we have it, otherwise by name
                    match = (pid and e_pid == pid) or (
                        not pid and
                        self._normalize_name(e_name) == norm_name
                    )
                    if match:
                        pts = pool.get("appliedStatTotal", 0.0) or 0.0
                        weekly_scores[week] = pts
                        if not found_name and e_name:
                            found_name = e_name
                        if not pid:
                            pid = e_pid
                        break

        # If mRoster found the player but every week is exactly zero, this
        # may be the same player-level mRoster bug seen in 2017 rather than
        # a genuinely zero-scoring player — try mBoxscore before giving up.
        all_zero = bool(weekly_scores) and all(v == 0 for v in weekly_scores.values())
        if not weekly_scores or all_zero:
            box_weekly = {}
            for week in range(1, reg_season_weeks + 1):
                week_data = self._direct_api(
                    year, "mBoxscore",
                    extra_params=f"&scoringPeriodId={week}"
                )
                if not week_data:
                    self._sweep_failed.setdefault(year, set()).add(week)
                    continue
                for matchup in week_data.get("schedule", []):
                    for side in ("home", "away"):
                        roster = matchup.get(side, {}).get("rosterForMatchupPeriod", {})
                        for entry in roster.get("entries", []):
                            pool   = entry.get("playerPoolEntry", {})
                            e_pid  = pool.get("id") or entry.get("playerId")
                            e_name = pool.get("player", {}).get("fullName", "")
                            match = (pid and e_pid == pid) or (
                                not pid and
                                self._normalize_name(e_name) == norm_name
                            )
                            if match:
                                box_weekly[week] = pool.get("appliedStatTotal", 0.0) or 0.0
                                if not found_name and e_name:
                                    found_name = e_name
                                if not pid:
                                    pid = e_pid
            # Only prefer the mBoxscore result if it actually found
            # non-zero data the mRoster sweep didn't.
            if box_weekly and any(v != 0 for v in box_weekly.values()):
                weekly_scores = box_weekly

        if not weekly_scores:
            return None, found_name

        season_total     = sum(weekly_scores.values())
        weeks_with_points = [w for w, p in weekly_scores.items() if p > 0]
        last_active      = max(weeks_with_points) if weeks_with_points else 0
        dropped          = (
            last_active > 0 and
            last_active < reg_season_weeks and
            all(weekly_scores.get(w, 0) == 0
                for w in range(last_active + 1, reg_season_weeks + 1))
        )

        return {
            "points":           round(season_total, 2),
            "droppedMidSeason": dropped,
            "lastActiveWeek":   last_active if dropped else None,
        }, found_name

    def fetch_keeper_data_by_year(self):
        """
        Build unified keeper dataset for this league.

        This league has used ESPN's native keeper tracking since keepers
        were introduced in 2017 (2 keepers/team throughout; custom
        per-player keeper rounds added in 2022 once ESPN supported them).
        Unlike Monday Morning Tears, there is no pre-API hardcoded era —
        all years are sourced directly from the mDraftDetail keeper flag.

        For all years: player identity + round kept from mDraftDetail
        keeper flag; points normally come from a weekly mRoster sweep
        (end-of-season snapshot's appliedStatTotal is unreliable — see
        _get_player_points_weekly).

        KNOWN EXCEPTION — 2017: mRoster's appliedStatTotal returns 0.0 for
        every player, every week, even though mBoxscore's per-team totals
        for the same weeks are correct (confirmed via direct API testing
        against this league, June 2026). The weekly sweep automatically
        detects an all-zero result and falls back to
        _get_player_points_weekly_via_boxscore, which reconstructs points
        from mBoxscore's rosterForMatchupPeriod instead. That fallback has
        a real information loss versus mRoster: it only sees players in
        active lineup slots, so it can't distinguish "benched" from
        "dropped" the way 2019+ data can. Treat 2017 droppedMidSeason /
        lastActiveWeek values as approximate.

        Zero-point keepers (in years where the bulk sweep otherwise
        succeeds) get a second, targeted weekly fallback to capture
        mid-season drops — see _get_player_points_with_fallback, which
        also tries mBoxscore if mRoster finds nothing.

        Round benchmarks: average points scored by all non-keeper drafted
        players in the same round, giving a fair "expected value" baseline.

        Returns:
            keepers_by_year: { year: [ keeperEntry, ... ] }
        """

        print(f"\n{'='*80}")
        print(" Fetching Keeper Data & Round Benchmarks")
        print(f"{'='*80}")

        VERDICT_THRESHOLD = 0.20   # ±20% vs round average = STEAL / BUST

        # Keepers introduced in this league in 2017; ESPN native tracking
        # covers this entire span, so no hardcoded pre-API years are needed.
        FIRST_KEEPER_YEAR  = 2017

        keepers_by_year = {}

        for year in range(FIRST_KEEPER_YEAR, self.end_year + 1):
            print(f"\n  Processing {year}...")
            # Drop the previous season's memoized API responses (the weekly
            # sweeps are large); repeats within THIS season still hit the cache.
            self._api_cache.clear()

            # ── Get teamId -> owner mapping from already-fetched league ───
            season_obj = next(
                (s for s in self.all_seasons if s['year'] == year), None
            )
            team_id_to_owner = {}
            reg_season_weeks  = 13   # safe default
            season_in_progress = False
            if season_obj:
                league = season_obj['league']
                for team in league.teams:
                    owner = self.team_owner.get((year, team.team_id))
                    if owner:
                        team_id_to_owner[team.team_id] = (
                            self._title_case_owner(owner)
                        )
                reg_season_count = getattr(
                    league.settings, 'reg_season_count', 13
                )
                reg_season_weeks = self._completed_weeks(league, reg_season_count)
                # If completed_weeks came in under the full regular season,
                # this season hasn't finished yet - points/verdict aren't
                # meaningful on a partial sample, so we'll show identity
                # only (owner/player/round) and mark the stat columns TBD
                # rather than compute a misleading early-season verdict.
                season_in_progress = reg_season_weeks < reg_season_count

            # ── 1. Fetch draft board ──────────────────────────────────────
            draft_data = self._direct_api(year, "mDraftDetail")
            if not draft_data:
                print(f"    ✗ Could not fetch mDraftDetail for {year}, skipping")
                self._record_warning(f"{year}: could not fetch the draft board - keeper analysis skipped for this season")
                continue

            picks = draft_data.get("draftDetail", {}).get("picks", [])
            if not picks:
                print(f"    ✗ No draft picks returned for {year}, skipping")
                self._record_warning(f"{year}: no draft picks returned - keeper analysis skipped for this season")
                continue

            # playerId -> { round, pick, teamId, keeper, keeperValue }
            draft_by_pid  = {}
            # normalized name -> playerId  (fallback for hardcoded keepers)
            pid_by_name   = {}
            for pick in picks:
                pid  = pick.get("playerId")
                rnd  = pick.get("roundId")
                entry = {
                    "round":       rnd,
                    "pick":        pick.get("roundPickNumber"),
                    "teamId":      pick.get("teamId"),
                    "keeper":      pick.get("keeper", False),
                    # keeperValue = the round slot consumed (may differ from roundId)
                    "keeperValue": pick.get("keeperValue") or rnd,
                }
                if pid:
                    draft_by_pid[pid] = entry
                    # ESPN doesn't return playerName in leagueHistory draft picks,
                    # so we populate pid_by_name from player_points after roster fetch
            print(f"    ✓ Draft board: {len(picks)} picks")

            # ── 2. Fetch player points via weekly sweep ───────────────────
            # appliedStatTotal on end-of-season mRoster returns only the LAST
            # scoring period's score, not a season total — unusable for our
            # purposes. Weekly sweep is required for all years.
            print(f"    Sweeping {reg_season_weeks} weeks for player points...")
            player_points = self._get_player_points_weekly(
                year, reg_season_weeks
            )

            non_zero = sum(1 for p in player_points.values() if p['points'] > 0)
            print(f"    ✓ Player points: {len(player_points)} players, "
                  f"{non_zero} with non-zero totals")

            # mRoster's appliedStatTotal is broken at the player level for
            # some historical seasons (confirmed for 2017 in this league,
            # even though mBoxscore's per-team totals for the same weeks
            # are correct). If the mRoster sweep comes back essentially
            # empty, fall back to reconstructing points from mBoxscore.
            # See _get_player_points_weekly_via_boxscore for the caveat
            # this introduces around drop/bench detection.
            if player_points and non_zero == 0:
                print(f"    ⚠️  mRoster returned all-zero player points for "
                      f"{year} — falling back to mBoxscore sweep...")
                player_points = self._get_player_points_weekly_via_boxscore(
                    year, reg_season_weeks
                )
                non_zero = sum(1 for p in player_points.values() if p['points'] > 0)
                print(f"    ✓ Player points (via mBoxscore): {len(player_points)} players, "
                      f"{non_zero} with non-zero totals")

            # Build name->pid lookup from the player_points we just fetched
            for p_pid, p_info in player_points.items():
                n = self._normalize_name(p_info["name"])
                if n:
                    pid_by_name[n] = p_pid

            # ── 3. Build round benchmarks ─────────────────────────────────
            # Use only non-keeper drafted players for a fair baseline
            round_totals = defaultdict(list)
            for p_pid, p_info in player_points.items():
                draft_info = draft_by_pid.get(p_pid)
                if not draft_info:
                    continue
                if draft_info["keeper"]:
                    continue          # exclude keepers from their own benchmark
                rnd = draft_info["round"]
                pts = p_info["points"]
                if rnd and pts > 0:
                    round_totals[rnd].append(pts)

            benchmarks = {
                rnd: round(sum(pts) / len(pts), 2)
                for rnd, pts in round_totals.items()
                if pts
            }
            print(f"    ✓ Benchmarks: {len(benchmarks)} rounds, "
                  f"{sum(len(v) for v in round_totals.values())} players")

            # ── 4. Identify raw keepers ───────────────────────────────────
            # Identity from draft board keeper flag (ESPN-native for all
            # years this league has used keepers — no hardcoded era needed)
            raw_keepers = []

            for pick in picks:
                if not pick.get("keeper", False):
                    continue
                pid     = pick.get("playerId")
                tid     = pick.get("teamId")
                round_k = pick.get("keeperValue") or pick.get("roundId")
                owner   = self._title_case_owner(
                    team_id_to_owner.get(tid, f"Team {tid}")
                )
                # Resolve name: player_points roster first, then draft pick
                p_name = ""
                if pid and pid in player_points:
                    p_name = player_points[pid]["name"]
                if not p_name:
                    # Player was dropped before season end and ESPN has
                    # scrubbed them — check KEEPER_POINT_OVERRIDES for a
                    # matching (year, owner) entry to recover the name.
                    for (ov_year, ov_owner, ov_player) in KEEPER_POINT_OVERRIDES:
                        if (ov_year == year
                                and self._title_case_owner(ov_owner) == owner):
                            p_name = ov_player
                            break
                if not p_name:
                    p_name = "Unknown"
                raw_keepers.append({
                    "owner":     owner,
                    "player":    p_name,
                    "roundKept": round_k,
                    "playerId":  pid,
                })

            # ── 5. Enrich keepers with points + verdict ───────────────────
            enriched = []
            for k in raw_keepers:
                pid       = k["playerId"]
                norm_name = self._normalize_name(k["player"])

                if season_in_progress:
                    # Season isn't over - a partial-sample verdict would be
                    # misleading (e.g. 2 weeks of data), so show who was
                    # kept and in what round, with everything else TBD.
                    # Still resolve the player's name if we can (cheap,
                    # already-fetched data) so the identity display isn't
                    # missing anything just because points are pending.
                    p_info = None
                    if pid and pid in player_points:
                        p_info = player_points[pid]
                    elif not pid:
                        resolved = pid_by_name.get(norm_name)
                        if resolved:
                            p_info = player_points.get(resolved)
                    if p_info and p_info["name"] and (
                        not k["player"] or k["player"] == "Unknown"
                    ):
                        k["player"] = p_info["name"]

                    enriched.append({
                        "owner":            k["owner"],
                        "player":           k["player"],
                        "roundKept":        k["roundKept"],
                        "pointsScored":     round(p_info["points"], 2) if p_info else None,
                        "pointsThroughWeek": reg_season_weeks,
                        "roundAvgPoints":   None,
                        "pointsVsAvg":      None,
                        "pointsVsAvgPct":   None,
                        "verdict":          "TBD",
                        "droppedMidSeason": False,
                        "lastActiveWeek":   None,
                        "note":             KEEPER_NOTES.get((year, k["owner"], k["player"])),
                    })
                    pts_note = f"{p_info['points']:.1f} pts thru Wk {reg_season_weeks}" if p_info else "no points yet"
                    print(f"    ⏳ {k['owner']:<20} {k['player']:<25} "
                          f"Rd {k['roundKept']}  TBD ({pts_note}, season in progress)")
                    continue

                # Resolve points from player_points
                p_info = None
                if pid and pid in player_points:
                    p_info = player_points[pid]
                elif not pid:
                    # name-only match (fallback if draft board didn't resolve a playerId)
                    resolved = pid_by_name.get(norm_name)
                    if resolved:
                        pid    = resolved
                        p_info = player_points.get(pid)

                points          = p_info["points"]          if p_info else None
                dropped         = p_info["droppedMidSeason"] if p_info else False
                last_active_wk  = p_info["lastActiveWeek"]  if p_info else None

                # Update player name from roster data if we had "Unknown"
                if p_info and p_info["name"] and (
                    not k["player"] or k["player"] == "Unknown"
                ):
                    k["player"] = p_info["name"]

                # Check manual override first (players ESPN has scrubbed)
                override_key = (year, k["owner"], k["player"])
                if override_key in KEEPER_POINT_OVERRIDES:
                    override_val = KEEPER_POINT_OVERRIDES[override_key]
                    points         = override_val[0]
                    dropped        = override_val[1]
                    last_active_wk = override_val[2] if dropped else None
                    print(f"    ⚡ Override applied: {k['player']} ({year}) "
                          f"= {points} pts"
                          f"{f' [dropped wk {last_active_wk}]' if dropped else ''}")
                # Zero-points fallback: targeted weekly sweep for dropped players.
                # Since we already did a full sweep, zeros here are genuine
                # absences — player wasn't on any roster any week (e.g. cut
                # pre-season or ESPN scrubbed them). Try one more targeted pass
                # in case they appear under a slightly different team context.
                elif (points is None or points == 0.0):
                    print(f"    ↩ Zero/missing — weekly fallback for "
                          f"{k['player']} ({year})...")
                    fallback, recovered_name = self._get_player_points_with_fallback(
                        year, player_points, pid, norm_name, reg_season_weeks
                    )
                    if fallback:
                        points         = fallback["points"]
                        dropped        = fallback["droppedMidSeason"]
                        last_active_wk = fallback["lastActiveWeek"]
                    if recovered_name and (
                        not k["player"] or k["player"] == "Unknown"
                    ):
                        k["player"] = recovered_name

                # Verdict
                round_kept    = k["roundKept"]
                round_avg     = benchmarks.get(round_kept)
                points_vs_avg = None
                pct_vs_avg    = None

                if points is not None and round_avg and round_avg > 0:
                    points_vs_avg = round(points - round_avg, 2)
                    pct_vs_avg    = round((points - round_avg) / round_avg, 4)
                    if pct_vs_avg >= VERDICT_THRESHOLD:
                        verdict = "STEAL"
                    elif pct_vs_avg <= -VERDICT_THRESHOLD:
                        verdict = "BUST"
                    else:
                        verdict = "AVERAGE"
                elif points is None:
                    verdict = "NO_DATA"
                else:
                    # points == 0.0 and no round_avg (shouldn't happen often)
                    verdict = "BUST" if round_avg else "NO_DATA"

                enriched.append({
                    "owner":            k["owner"],
                    "player":           k["player"],
                    "roundKept":        round_kept,
                    "pointsScored":     round(points, 2) if points is not None else None,
                    "roundAvgPoints":   round_avg,
                    "pointsVsAvg":      points_vs_avg,
                    "pointsVsAvgPct":   pct_vs_avg,
                    "verdict":          verdict,
                    "droppedMidSeason": dropped,
                    "lastActiveWeek":   last_active_wk,
                    "note":             KEEPER_NOTES.get((year, k["owner"], k["player"])),
                })

                verdict_icon = {
                    "STEAL": "🟢", "BUST": "🔴",
                    "AVERAGE": "🟡", "NO_DATA": "❓"
                }.get(verdict, "❓")
                pts_str  = f"{points:.1f}" if points is not None else "N/A"
                avg_str  = f"{round_avg:.1f}" if round_avg else "N/A"
                drop_str = f" [dropped wk {last_active_wk}]" if dropped else ""
                print(f"    {verdict_icon} {k['owner']:<20} {k['player']:<25} "
                      f"Rd {round_kept}  {pts_str} pts (avg {avg_str})"
                      f"  {verdict}{drop_str}")

            failed_weeks = sorted(self._sweep_failed.get(year, ()))
            if failed_weeks:
                msg = (f"{year}: weekly player-point request failed after retries for week(s) "
                       f"{failed_weeks} - keeper points for this season may be UNDERCOUNTED")
                print(f"    ⚠️  {msg}")
                self._record_warning(msg)
            keepers_by_year[year] = enriched
            print(f"    → {len(enriched)} keepers for {year}")

        self._api_cache.clear()
        return keepers_by_year


    def _resolve_position_limit_name(self, pos_id, year, cache, slot_names):
        """Resolve a rosterSettings.positionLimits ID to a readable name.

        positionLimits uses ESPN's player defaultPositionId scheme, which is
        NOT the same numbering as lineup slotCategoryId (confirmed live: id
        '1' is QB here, but slot id 1 is 'TQB'). Only a handful of
        defaultPositionId values are publicly documented anywhere (QB=1,
        RB=2, WR=3, TE=4, K=5, D/ST=16 - see cwendt94/espn-api issue #460,
        where even the library maintainers call this "decoded by hand" and
        ask IDP-league users to open an issue for anything else). Rather
        than guess at undocumented IDs (confirmed to otherwise show up as
        e.g. "position ID 7" or "position ID 15" for IDP-adjacent slots
        neither MMT nor TOT actually uses), this resolves them from real
        roster data: each player's eligibleSlots list uses the fully
        documented slot-ID scheme, so the first non-generic (single
        position) slot a player is eligible for tells us what their
        defaultPositionId actually means.

        Fetches at most one mRoster snapshot per season, and only when an
        unmapped ID is actually encountered in a diff - most runs never
        trigger this at all, since the six hardcoded names above already
        cover ordinary QB/RB/WR/TE/K/D/ST limit changes. Results are cached
        in `cache` (passed in by the caller) for the rest of this
        settings-history run, since defaultPositionId is a stable NFL-wide
        ID, not something that varies year to year.
        """
        if pos_id in cache:
            return cache[pos_id]

        # Slots that are multi-position or non-positional and so make poor
        # labels even though they're "non-generic" by ID: bench, IR, FLEX,
        # OP, RB/WR, WR/TE, RB/WR/TE.
        GENERIC_SLOTS = {'20', '21', '23', '7', '3', '5'}

        roster_data = self._direct_api(year, "mRoster")
        if roster_data:
            for team in roster_data.get("teams", []):
                for entry in team.get("roster", {}).get("entries", []):
                    player = entry.get("playerPoolEntry", {}).get("player", {})
                    p_id = player.get("defaultPositionId")
                    if p_id is None:
                        continue
                    p_id = str(p_id)
                    if p_id in cache:
                        continue
                    eligible = [str(s) for s in player.get("eligibleSlots", [])]
                    primary = next(
                        (s for s in eligible
                         if s in slot_names and s not in GENERIC_SLOTS),
                        None
                    )
                    if primary:
                        cache[p_id] = slot_names[primary].replace(' slots', '')

        return cache.get(pos_id, f"position ID {pos_id}")

    def _get_json_with_retry(self, url, params, cookies, label=""):
        """GET a JSON endpoint, retrying transient failures (network errors,
        5xx) up to MAX_FETCH_ATTEMPTS times. 4xx responses are not retried.
        Every failure is printed. Returns parsed JSON, or None if all
        attempts fail."""
        for attempt in range(1, MAX_FETCH_ATTEMPTS + 1):
            retryable = True
            try:
                r = requests.get(url, params=params, cookies=cookies, timeout=10)
                if r.status_code == 200:
                    return r.json()
                print(f"  ⚠️  {label}: HTTP {r.status_code} (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
                retryable = r.status_code >= 500
            except Exception as e:
                print(f"  ⚠️  {label}: {e} (attempt {attempt}/{MAX_FETCH_ATTEMPTS})")
            if not retryable:
                break
            if attempt < MAX_FETCH_ATTEMPTS:
                time.sleep(3 * attempt)
        return None

    def calculate_settings_history(self):
        """Fetch and diff league settings year-over-year via direct API calls.

        Uses mSettings view for each season. For 2019+ uses the live seasons
        endpoint; for pre-2019 uses leagueHistory. Diffs every meaningful
        setting field including scoring rule changes derived from ESPN's
        scoringItems array.

        Stat IDs and lineup slot IDs sourced from espn_api/football/constant.py
        (cwendt94/espn-api) — includes punter stats (138-154) and punter
        roster slot (18) which are required for this league (punters active
        in 2024, removed in 2025).

        Returns a list of { year, changes: [...] } dicts, one per season.
        """
        print(f"\n{'='*80}")
        print(" Fetching Settings History")
        print(f"{'='*80}")

        # Lineup slot IDs → display names
        # Source: POSITION_MAP in espn_api/football/constant.py
        LINEUP_SLOT_NAMES = {
            '0':  'QB slots',
            '2':  'RB slots',
            '4':  'WR slots',
            '6':  'TE slots',
            '16': 'D/ST slots',
            '17': 'K slots',
            '18': 'P (punter) slots',   # added — this league used punters in 2024
            '23': 'FLEX slots',
            '20': 'Bench slots',
            '21': 'IR slots',
        }
        # Remaining lineup slot IDs from POSITION_MAP (espn_api constant.py),
        # used only by the generic slot diff below so no slot change is
        # silently ignored just because it isn't one of the named ones above.
        LINEUP_SLOT_NAMES_EXTRA = {
            '1': 'TQB slots', '3': 'RB/WR slots', '5': 'WR/TE slots',
            '7': 'OP (superflex) slots', '8': 'DT slots', '9': 'DE slots',
            '10': 'LB slots', '11': 'DL slots', '12': 'CB slots',
            '13': 'S slots', '14': 'DB slots', '15': 'DP slots',
            '19': 'HC slots', '22': 'RB/WR/TE slots (unused id)',
        }
        # Slot IDs that already have their own dedicated diff wording below
        SLOTS_WITH_CUSTOM_DIFF = {'20', '21', '23', '18'}
        # positionLimits keys use player default-position IDs (espn_api
        # constant.py POSITION_MAP for players), which differ from slot IDs.
        POSITION_LIMIT_NAMES = {
            '1': 'QB', '2': 'RB', '3': 'WR', '4': 'TE', '5': 'K', '16': 'D/ST',
            # '7' and '15' aren't part of the handful of publicly-documented
            # defaultPositionId values above - confirmed instead against
            # ESPN's own roster-settings UI (Punter and Defensive Player
            # rows), and against the espn_api library's slot-ID POSITION_MAP,
            # which this positionLimits scheme falls back to matching for any
            # position without a custom remapped ID.
            '7': 'Punter', '15': 'Defensive Player',
        }

        # Scoring stat IDs → display names
        # Source: SETTINGS_SCORING_FORMAT_MAP in espn_api/football/constant.py
        STAT_ID_NAMES = {
            0:   'Each Pass Attempted',
            1:   'Each Pass Completed',
            2:   'Each Incomplete Pass',
            3:   'Passing Yards',
            4:   'TD Pass',
            5:   'Every 5 passing yards',
            6:   'Every 10 passing yards',
            8:   'Every 25 passing yards',
            10:  'Every 100 passing yards',
            15:  '40+ yard TD pass bonus',
            16:  '50+ yard TD pass bonus',
            17:  '300-399 yard passing game',
            18:  '400+ yard passing game',
            19:  '2pt Passing Conversion',
            20:  'Interceptions Thrown',
            23:  'Rushing Attempts',
            24:  'Rushing Yards',
            25:  'TD Rush',
            26:  '2pt Rushing Conversion',
            35:  '40+ yard TD rush bonus',
            36:  '50+ yard TD rush bonus',
            37:  '100-199 yard rushing game',
            38:  '200+ yard rushing game',
            41:  'Receptions',
            42:  'Receiving Yards',
            43:  'TD Reception',
            44:  '2pt Receiving Conversion',
            45:  '40+ yard TD rec bonus',
            46:  '50+ yard TD rec bonus',
            53:  'Each Reception (PPR)',
            56:  '100-199 yard receiving game',
            57:  '200+ yard receiving game',
            58:  'Receiving Target',
            62:  'Total 2pt Conversions',
            63:  'Fumble Recovered for TD',
            64:  'Times Sacked',
            68:  'Total Fumbles',
            72:  'Total Fumbles Lost',
            73:  'Total Turnovers',
            74:  'FG Made (50+ yards)',
            76:  'FG Missed (50+ yards)',
            77:  'FG Made (40-49 yards)',
            79:  'FG Missed (40-49 yards)',
            80:  'FG Made (0-39 yards)',
            82:  'FG Missed (0-39 yards)',
            83:  'Total FG Made',
            85:  'Total FG Missed',
            86:  'Each PAT Made',
            88:  'Each PAT Missed',
            89:  '0 points allowed',
            90:  '1-6 points allowed',
            91:  '7-13 points allowed',
            92:  '14-17 points allowed',
            93:  'Blocked Punt/FG returned for TD',
            94:  'Fumble or INT Return for TD',
            95:  'Each Interception',
            96:  'Each Fumble Recovered',
            97:  'Blocked Punt, PAT or FG',
            98:  'Each Safety',
            99:  'Each Sack',
            100: '1/2 Sack',
            101: 'Kickoff Return TD',
            102: 'Punt Return TD',
            103: 'Interception Return TD',
            104: 'Fumble Return TD',
            105: 'Total Return TD',
            106: 'Each Fumble Forced',
            107: 'Assisted Tackles',
            108: 'Solo Tackles',
            109: 'Total Tackles',
            120: 'Points Allowed',
            121: '18-21 points allowed',
            122: '22-27 points allowed',
            123: '28-34 points allowed',
            124: '35-45 points allowed',
            125: '46+ points allowed',
            127: 'Yards Allowed',
            128: '<100 total yards allowed',
            129: '100-199 yards allowed',
            130: '200-299 yards allowed',
            131: '300-349 yards allowed',
            132: '350-399 yards allowed',
            133: '400-449 yards allowed',
            134: '450-499 yards allowed',
            135: '500-549 yards allowed',
            136: '550+ yards allowed',
            # Punter stats (stat IDs 138-154, from espn_api constant.py)
            138: 'Net Punts',
            139: 'Punt Yards',
            140: 'Punts Inside the 10',
            141: 'Punts Inside the 20',
            142: 'Blocked Punts',
            145: 'Punt Touchbacks',
            146: 'Punt Fair Catches',
            147: 'Punt Average',
            148: 'Punt Average 44.0+',
            149: 'Punt Average 42.0-43.9',
            150: 'Punt Average 40.0-41.9',
            151: 'Punt Average 38.0-39.9',
            152: 'Punt Average 36.0-37.9',
            153: 'Punt Average 34.0-35.9',
            154: 'Punt Average 33.9 or less',
            198: 'FG Made (50-59 yards)',
            200: 'FG Missed (50-59 yards)',
            201: 'FG Made (60+ yards)',
            203: 'FG Missed (60+ yards)',
            205: 'Defensive 2pt Return',
            206: '2pt Return',
            209: '1pt Safety',
            211: 'Passing First Down',
            212: 'Rushing First Down',
            213: 'Receiving First Down',
        }

        def fmt_date(epoch_ms):
            if not epoch_ms or epoch_ms <= 0:
                return None
            try:
                return datetime.fromtimestamp(epoch_ms / 1000).strftime('%b %-d, %Y')
            except Exception:
                try:
                    return datetime.fromtimestamp(epoch_ms / 1000).strftime('%b %d, %Y')
                except Exception:
                    return None

        cookies = {"swid": self.swid, "espn_s2": self.espn_s2}
        all_settings = {}

        for season_data in self.all_seasons:
            year = season_data['year']

            if year >= 2019:
                url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
                       f"/seasons/{year}/segments/0/leagues/{self.league_id}")
                params = {"view": "mSettings"}
            else:
                url = (f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
                       f"/leagueHistory/{self.league_id}")
                params = {"view": "mSettings", "seasonId": year}

            try:
                d = self._get_json_with_retry(url, params, cookies, label=str(year))
                if d is None:
                    print(f"  ✗ {year}: settings unavailable after {MAX_FETCH_ATTEMPTS} attempts, skipping")
                    self._record_warning(f"{year}: settings history unavailable after {MAX_FETCH_ATTEMPTS} attempts")
                    continue
                if isinstance(d, list):
                    d = d[0]

                s        = d.get('settings', {})
                acq      = s.get('acquisitionSettings', {})
                draft    = s.get('draftSettings', {})
                roster   = s.get('rosterSettings', {})
                schedule = s.get('scheduleSettings', {})
                trade    = s.get('tradeSettings', {})
                scoring  = s.get('scoringSettings', {})

                # Build scoring map: statId → points for standard scoring type
                scoring_map = {}
                for item in scoring.get('scoringItems', []):
                    stat_id   = item['statId']
                    overrides = item.get('pointsOverrides', {})
                    pts       = overrides.get('16', item.get('points', 0))
                    if pts != 0:
                        scoring_map[stat_id] = pts

                # Lineup slot counts — string keys in ESPN API response
                lineup = roster.get('lineupSlotCounts', {})
                # Full slot-count and per-position max-roster maps, keyed by
                # string ID, so the diff can catch any slot/limit change
                # (e.g. QB slots, max rostered QBs) generically.
                lineup_all = {str(k): int(v) for k, v in lineup.items()}
                position_limits = {
                    str(k): int(v)
                    for k, v in (roster.get('positionLimits') or {}).items()
                }

                # Divisions
                divisions   = schedule.get('divisions', [])
                div_summary = (f"{len(divisions)} division(s): "
                               f"{', '.join(d['name'] for d in divisions)}")

                trade_deadline = fmt_date(trade.get('deadlineDate'))

                all_settings[year] = {
                    'scoring':                    scoring_map,
                    'teamCount':                  s.get('size', 0),
                    'regSeasonCount':             schedule.get('matchupPeriodCount', 0),
                    'playoffTeamCount':           schedule.get('playoffTeamCount', 0),
                    'playoffMatchupPeriodLength': schedule.get('playoffMatchupPeriodLength', 1),
                    'keeperCount':                draft.get('keeperCount', 0),
                    'draftPickTrading':           draft.get('isTradingEnabled', False),
                    'draftTimePerPick':           draft.get('timePerSelection', 60),
                    'draftType':                  draft.get('type', 'SNAKE'),
                    'undroppableList':            roster.get('isUsingUndroppableList', True),
                    'irSlots':                    lineup.get('21', lineup.get(21, 0)),
                    'benchSlots':                 lineup.get('20', lineup.get(20, 0)),
                    'flexSlots':                  lineup.get('23', lineup.get(23, 0)),
                    'punterSlots':                lineup.get('18', lineup.get(18, 0)),
                    'lineupSlots':                lineup_all,
                    'positionLimits':             position_limits,
                    'divisionCount':              len(divisions),
                    'divisionSummary':            div_summary,
                    'tradeDeadline':              trade_deadline,
                    'vetoVotes':                  trade.get('vetoVotesRequired', 4),
                    'acquisitionLimit':           acq.get('acquisitionLimit', -1),
                    'matchupAcquisitionLimit':    acq.get('matchupAcquisitionLimit', 0),
                }
                print(f"  ✓ {year}: fetched settings")

            except Exception as e:
                print(f"  ⚠️  {year}: {e}")
                self._record_warning(f"{year}: settings history failed: {e}")
                continue

        # Diff year-over-year to build changelog
        history = []
        prev    = None
        position_id_name_cache = {}

        for year in sorted(all_settings.keys()):
            curr    = all_settings[year]
            changes = []

            if prev is None:
                changes.append(
                    f"League founded — {curr['teamCount']} teams, "
                    f"{curr['regSeasonCount']}-week regular season"
                )
                changes.append(f"Divisions: {curr['divisionSummary']}")
                changes.append(
                    f"Draft: {curr['draftType']}, {curr['draftTimePerPick']}s per pick, "
                    f"pick trading {'enabled' if curr['draftPickTrading'] else 'disabled'}"
                )
                changes.append(
                    f"Roster: {curr['benchSlots']} bench slots, "
                    f"{curr['irSlots']} IR slot(s), "
                    f"{curr['flexSlots']} FLEX slot(s)"
                    + (f", {curr['punterSlots']} punter slot(s)" if curr['punterSlots'] else "")
                )
                changes.append(
                    f"Undroppable player list: "
                    f"{'enabled' if curr['undroppableList'] else 'disabled'}"
                )
                changes.append(
                    f"Trade deadline: {curr['tradeDeadline'] or 'none'}"
                )
                changes.append(f"Scoring: {len(curr['scoring'])} active rules")
            else:
                prev_year = history[-1]['year']
                if year != prev_year + 1:
                    gap = (f"{prev_year + 1}" if year - prev_year == 2
                           else f"{prev_year + 1}-{year - 1}")
                    self._record_warning(f"Settings for {gap} could not be fetched; the settings-history "
                                         f"changes listed for {year} span multiple seasons")
                    changes.append(
                        f"Note: settings for {gap} could not be fetched, "
                        f"so the changes below span multiple seasons"
                    )
                # Structure
                if curr['teamCount'] != prev['teamCount']:
                    changes.append(
                        f"Teams: {prev['teamCount']} → {curr['teamCount']}"
                    )
                if curr['regSeasonCount'] != prev['regSeasonCount']:
                    changes.append(
                        f"Regular season: {prev['regSeasonCount']} → "
                        f"{curr['regSeasonCount']} weeks"
                    )
                if curr['playoffTeamCount'] != prev['playoffTeamCount']:
                    changes.append(
                        f"Playoff teams: {prev['playoffTeamCount']} → "
                        f"{curr['playoffTeamCount']}"
                    )
                if curr['playoffMatchupPeriodLength'] != prev['playoffMatchupPeriodLength']:
                    fmt = lambda n: f"{n}-week matchups"
                    changes.append(
                        f"Playoff format: {fmt(prev['playoffMatchupPeriodLength'])} → "
                        f"{fmt(curr['playoffMatchupPeriodLength'])}"
                    )
                if curr['keeperCount'] != prev['keeperCount']:
                    changes.append(
                        f"Keepers per team: {prev['keeperCount']} → {curr['keeperCount']}"
                    )

                # Roster slots
                if curr['irSlots'] != prev['irSlots']:
                    changes.append(
                        f"IR slots: {prev['irSlots']} → {curr['irSlots']}"
                    )
                if curr['benchSlots'] != prev['benchSlots']:
                    changes.append(
                        f"Bench slots: {prev['benchSlots']} → {curr['benchSlots']}"
                    )
                if curr['flexSlots'] != prev['flexSlots']:
                    changes.append(
                        f"FLEX slots: {prev['flexSlots']} → {curr['flexSlots']}"
                    )
                if curr['punterSlots'] != prev['punterSlots']:
                    if curr['punterSlots'] > 0:
                        changes.append(
                            f"Punter slot added ({prev['punterSlots']} → "
                            f"{curr['punterSlots']})"
                        )
                    else:
                        changes.append(
                            f"Punter slot removed ({prev['punterSlots']} → 0)"
                        )

                # Every other lineup slot (QB, RB, WR, TE, D/ST, K, OP, ...)
                slot_names = {**LINEUP_SLOT_NAMES, **LINEUP_SLOT_NAMES_EXTRA}
                for slot_id in sorted(
                    set(curr['lineupSlots']) | set(prev['lineupSlots']),
                    key=int
                ):
                    if slot_id in SLOTS_WITH_CUSTOM_DIFF:
                        continue
                    c = curr['lineupSlots'].get(slot_id, 0)
                    p = prev['lineupSlots'].get(slot_id, 0)
                    if c != p:
                        label = slot_names.get(slot_id, f"Lineup slot {slot_id}")
                        changes.append(f"{label}: {p} → {c}")

                # Max rostered players per position (rosterSettings.positionLimits).
                # Keys here are ESPN *player* default-position IDs, NOT lineup
                # slot IDs (confirmed live: key '1' is QB, which the slot map
                # mislabelled "TQB"). The six most common positions are named
                # directly; anything else is resolved from real roster data
                # on demand (see _resolve_position_limit_name) rather than
                # left as a raw, meaningless ID.
                # ESPN switched its "position not configured" sentinel from 0
                # to -1 ("N/A" in the roster-settings UI) partway through
                # 2023. A change where BOTH the old and new value are one of
                # these two sentinels is just that display-convention flip,
                # not a real limit anyone set - confirmed against MMT 2023's
                # spurious "Max rostered Defensive Player: 0 → -1" line,
                # which can't be a real change since neither league has ever
                # used Defensive Players. Suppressed only when both sides are
                # sentinel values, so a real change into or out of an actual
                # configured limit (e.g. -1 → 2) is still reported normally.
                POSITION_LIMIT_SENTINELS = {0, -1}
                for pos_id in sorted(
                    set(curr['positionLimits']) | set(prev['positionLimits']),
                    key=int
                ):
                    c = curr['positionLimits'].get(pos_id)
                    p = prev['positionLimits'].get(pos_id)
                    if c is None or p is None or c == p:
                        continue
                    if c in POSITION_LIMIT_SENTINELS and p in POSITION_LIMIT_SENTINELS:
                        continue
                    name = POSITION_LIMIT_NAMES.get(pos_id) or \
                        self._resolve_position_limit_name(
                            pos_id, year, position_id_name_cache, slot_names
                        )
                    changes.append(f"Max rostered {name}: {p} → {c}")

                # Divisions
                if curr['divisionCount'] != prev['divisionCount']:
                    changes.append(
                        f"Divisions: {prev['divisionSummary']} → {curr['divisionSummary']}"
                    )

                # Draft
                if curr['draftPickTrading'] != prev['draftPickTrading']:
                    changes.append(
                        f"Draft pick trading: "
                        f"{'enabled' if curr['draftPickTrading'] else 'disabled'}"
                    )
                if curr['draftTimePerPick'] != prev['draftTimePerPick']:
                    changes.append(
                        f"Draft pick timer: {prev['draftTimePerPick']}s → "
                        f"{curr['draftTimePerPick']}s"
                    )
                if curr['draftType'] != prev['draftType']:
                    changes.append(
                        f"Draft type: {prev['draftType']} → {curr['draftType']}"
                    )

                # Waivers / admin
                if curr['undroppableList'] != prev['undroppableList']:
                    changes.append(
                        f"Undroppable player list: "
                        f"{'enabled' if curr['undroppableList'] else 'disabled'}"
                    )
                if curr['tradeDeadline'] != prev['tradeDeadline']:
                    changes.append(
                        f"Trade deadline: {prev['tradeDeadline'] or 'none'} → "
                        f"{curr['tradeDeadline'] or 'none'}"
                    )

                # Scoring changes — grouped by added / removed / changed
                scoring_added   = []
                scoring_removed = []
                scoring_changed = []

                all_stat_ids = (
                    set(curr['scoring'].keys()) | set(prev['scoring'].keys())
                )
                for stat_id in sorted(all_stat_ids):
                    curr_pts = curr['scoring'].get(stat_id, 0)
                    prev_pts = prev['scoring'].get(stat_id, 0)
                    if curr_pts != prev_pts:
                        name = STAT_ID_NAMES.get(stat_id, f"StatID {stat_id}")
                        if prev_pts == 0:
                            scoring_added.append(f"{name} ({curr_pts:+.2g} pts)")
                        elif curr_pts == 0:
                            scoring_removed.append(f"{name} (was {prev_pts:+.2g} pts)")
                        else:
                            scoring_changed.append(
                                f"{name}: {prev_pts:+.2g} → {curr_pts:+.2g} pts"
                            )

                if scoring_added:
                    label = f"Scoring added ({len(scoring_added)} rules) — " \
                            if len(scoring_added) > 1 else "Scoring added — "
                    changes.append(label + ', '.join(scoring_added))

                if scoring_removed:
                    label = f"Scoring removed ({len(scoring_removed)} rules) — " \
                            if len(scoring_removed) > 1 else "Scoring removed — "
                    changes.append(label + ', '.join(scoring_removed))

                if scoring_changed:
                    label = f"Scoring changed ({len(scoring_changed)} rules) — " \
                            if len(scoring_changed) > 1 else "Scoring changed — "
                    changes.append(label + ', '.join(scoring_changed))

            history.append({'year': year, 'changes': changes})
            prev = curr

        print(f"  ✓ Settings history built for {len(history)} seasons")
        return history

    def _counter_ties_at_max(self, counter):
        """Given an owner -> count dict, return (owners, max_count) where
        owners is every owner tied for the max count, sorted alphabetically
        for stable output. Returns ([], 0) if counter is empty. Prevents
        superlatives like 'most weekly high scores' from silently dropping
        every owner but one when there's a tie at the top."""
        if not counter:
            return [], 0
        max_count = max(counter.values())
        owners = sorted((o for o, c in counter.items() if c == max_count), key=lambda o: o or '')
        return owners, max_count

    def _compute_owner_streaks(self, owner_games):
        """For each owner, walk their games in chronological order and find
        their OWN best win streak and best loss streak (length plus detail:
        team, start/end year+week, the matchup periods of the games in the
        streak, whether it's still active as of their most recent game).
        Games only need 'won'/'lost' booleans and 'week'; 'year', 'period'
        and 'teamName' are optional. Returns (best_win_by_owner,
        best_loss_by_owner) dicts."""
        best_win = {}
        best_loss = {}

        for owner, games in owner_games.items():
            games = sorted(games, key=lambda g: (g.get('year', 0), g['week']))

            current_win = 0
            current_win_start = None
            current_win_periods = []
            current_loss = 0
            current_loss_start = None
            current_loss_periods = []

            for i, game in enumerate(games):
                is_last = (i == len(games) - 1)
                period = game.get('period', game['week'])

                if game['won']:
                    if current_win == 0:
                        current_win_start = game
                        current_win_periods = []
                    current_win += 1
                    current_win_periods.append(period)
                    current_loss = 0
                    current_loss_periods = []

                    if owner not in best_win or current_win > best_win[owner]['length']:
                        best_win[owner] = {
                            'owner': owner,
                            'length': current_win,
                            'teamName': game.get('teamName'),
                            'startYear': current_win_start.get('year'),
                            'startWeek': current_win_start['week'],
                            'endYear': game.get('year'),
                            'endWeek': game['week'],
                            'periods': list(current_win_periods),
                            'isOngoing': is_last
                        }
                elif game['lost']:
                    if current_loss == 0:
                        current_loss_start = game
                        current_loss_periods = []
                    current_loss += 1
                    current_loss_periods.append(period)
                    current_win = 0
                    current_win_periods = []

                    if owner not in best_loss or current_loss > best_loss[owner]['length']:
                        best_loss[owner] = {
                            'owner': owner,
                            'length': current_loss,
                            'teamName': game.get('teamName'),
                            'startYear': current_loss_start.get('year'),
                            'startWeek': current_loss_start['week'],
                            'endYear': game.get('year'),
                            'endWeek': game['week'],
                            'periods': list(current_loss_periods),
                            'isOngoing': is_last
                        }
                else:
                    current_win = 0
                    current_win_periods = []
                    current_loss = 0
                    current_loss_periods = []

        return best_win, best_loss

    def _streak_ties_at_max(self, best_by_owner):
        """Given an owner -> streak-detail dict (each with a 'length' key),
        return (tied_details, max_length): every owner's detail dict tied
        for the max length, sorted by owner name. Returns ([], 0) if empty."""
        if not best_by_owner:
            return [], 0
        max_length = max(d['length'] for d in best_by_owner.values())
        tied = sorted(
            (d for d in best_by_owner.values() if d['length'] == max_length),
            key=lambda d: d['owner'] or ''
        )
        return tied, max_length

    def _compute_period_transaction_stats(self, executed, year, team_attribution_start_year):
        """Compute most-dropped/sloppy-seconds stats for a given list of EXECUTED
        transactions. Caller decides which weeks to include (e.g. all weeks for
        the full-season view, or scoring_period <= reg_season_count for a
        regular-season-only view) - this method itself is period-agnostic.
        """
        league_drop_counts = Counter()
        for t in executed:
            for item in t.items:
                if item.type == 'DROP':
                    league_drop_counts[item.player] += 1
        league_wide_top3 = [
            {'player': player, 'count': count}
            for player, count in league_drop_counts.most_common(3)
        ]

        if year < team_attribution_start_year:
            return {
                'league_wide_top3': league_wide_top3,
                'per_team_top3': None,
                'sloppy': None,
                'team_tally': {},
                'same_week_flagged': 0
            }

        team_drop_counts = defaultdict(Counter)
        for t in executed:
            if t.team is None:
                continue
            owner = self.team_owner.get((year, t.team.team_id))
            if not owner:
                continue
            for item in t.items:
                if item.type == 'DROP':
                    team_drop_counts[owner][item.player] += 1

        per_team_top3 = {
            owner: [{'player': player, 'count': count} for player, count in counts.most_common(3)]
            for owner, counts in team_drop_counts.items()
        }

        # Sort chronologically by week so "who added it next" is meaningful.
        # Transactions within the same week have no finer ordering available
        # from this data (scoring_period only resolves to the week).
        executed_sorted = sorted(executed, key=lambda t: t.scoring_period)

        player_add_sequence = defaultdict(list)  # player -> [(week, owner), ...]
        for t in executed_sorted:
            if t.team is None:
                continue
            owner = self.team_owner.get((year, t.team.team_id))
            if not owner:
                continue
            for item in t.items:
                if item.type == 'ADD':
                    player_add_sequence[item.player].append((t.scoring_period, owner))

        multi_team_players = []
        team_tally = Counter()
        same_week_flagged = 0

        for player, seq in player_add_sequence.items():
            distinct_owners = list(dict.fromkeys(owner for _, owner in seq))
            if len(distinct_owners) < 2:
                continue

            multi_team_players.append({
                'player': player,
                'teamCount': len(distinct_owners),
                'teams': distinct_owners
            })

            seen_owners = set()
            for week, owner in seq:
                if seen_owners and owner not in seen_owners:
                    prior_same_week = any(
                        wk == week and ow != owner
                        for wk, ow in seq if (wk, ow) != (week, owner)
                    )
                    if prior_same_week:
                        same_week_flagged += 1
                    else:
                        team_tally[owner] += 1
                seen_owners.add(owner)

        multi_team_players.sort(key=lambda x: x['teamCount'], reverse=True)

        return {
            'league_wide_top3': league_wide_top3,
            'per_team_top3': per_team_top3,
            'sloppy': {
                'players': multi_team_players,
                'teamTally': dict(team_tally),
                'sameWeekAddsFlagged': same_week_flagged
            },
            'team_tally': dict(team_tally),
            'same_week_flagged': same_week_flagged
        }

    def calculate_transaction_analysis(self):
        """Calculate most-dropped-player and 'sloppy seconds' stats from waiver/free-agent transactions.

        ESPN API constraints confirmed empirically for this league directly
        (reference: espn-api source at https://github.com/cwendt94/espn-api -
        transactions()/Transaction/TransactionItem are not documented in
        detail, so behavior below was verified directly against this league
        rather than assumed):

        - league.transactions() returns NO data at all for 2014-2017 for this
          league (confirmed empirically - not a documented library limit).
        - Only status == 'EXECUTED' transactions represent moves that actually
          took effect; PENDING/FAILED_*/CANCELED are waiver claims that never
          went through and must be excluded.
        - In 2018 ONLY, every WAIVER-type transaction has team=None (confirmed
          100% broken that season - 132/132 - and 0% broken every other year
          2019-2025, same exact pattern as MMT). FREEAGENT-type transactions
          resolve team correctly even in 2018.
        - TransactionItem only exposes .type ('ADD'/'DROP') and .player
          (player name string) - no player ID, so matching is by name like
          the rest of this collector.
        - Transaction.scoring_period only resolves to the WEEK, not a precise
          timestamp, so two teams adding the same player in the same week
          cannot be reliably ordered - these are flagged as 'same-week adds'
          rather than arbitrarily attributed to one team.

        Produces TWO parallel views from the same fetched data (no extra API
        calls needed): the full-season view (all weeks, including playoffs -
        used by season-stats.html/teams.html/index.html), and a
        'regularSeasonOnly' view filtered to scoring_period <= reg_season_count,
        for pages like regular-season.html that need genuinely
        regular-season-scoped stats rather than the whole season lumped together.
        """
        print(f"\n{'='*80}")
        print(" Calculating Transaction Analysis (Most Dropped / Sloppy Seconds)")
        print(f"{'='*80}")

        TRANSACTION_DATA_START_YEAR = 2018  # transactions() returns nothing before this
        TEAM_ATTRIBUTION_START_YEAR = 2019  # 2018 WAIVER moves have team=None

        league_wide_most_dropped = {}
        per_team_most_dropped = {}
        sloppy_seconds_by_year = {}
        all_time_team_tally = defaultdict(int)
        all_time_same_week_flagged = 0

        reg_league_wide_most_dropped = {}
        reg_per_team_most_dropped = {}
        reg_sloppy_seconds_by_year = {}
        reg_all_time_team_tally = defaultdict(int)
        reg_all_time_same_week_flagged = 0

        for season_data in self.all_seasons:
            year = season_data['year']
            league = season_data['league']

            if year < TRANSACTION_DATA_START_YEAR:
                continue

            print(f"  Processing {year} transactions...")

            reg_weeks = getattr(league.settings, 'reg_season_count', 14)
            total_weeks_to_try = self._completed_weeks(league, self._final_week(league))

            all_transactions = []
            for week in range(1, total_weeks_to_try + 1):
                try:
                    week_transactions = league.transactions(
                        scoring_period=week,
                        types={"FREEAGENT", "WAIVER", "WAIVER_ERROR"}
                    )
                    all_transactions.extend(week_transactions)
                except ESPNAccessDenied:
                    raise
                except Exception as e:
                    print(f"    ⚠️  {year} week {week}: transactions skipped due to error: {e}")
                    self._record_warning(f"{year} week {week}: transactions skipped due to error: {e}")
                    continue

            executed = [t for t in all_transactions if t.status == 'EXECUTED']
            reg_season_executed = [t for t in executed if t.scoring_period <= reg_weeks]

            # --- Full season (unchanged behavior - includes playoff weeks) ---
            full = self._compute_period_transaction_stats(
                executed, year, TEAM_ATTRIBUTION_START_YEAR
            )
            league_wide_most_dropped[year] = full['league_wide_top3']
            per_team_most_dropped[year] = full['per_team_top3']
            sloppy_seconds_by_year[year] = full['sloppy']
            for owner, cnt in full['team_tally'].items():
                all_time_team_tally[owner] += cnt
            all_time_same_week_flagged += full['same_week_flagged']

            # --- Regular season only (new - excludes playoff weeks) ---
            reg = self._compute_period_transaction_stats(
                reg_season_executed, year, TEAM_ATTRIBUTION_START_YEAR
            )
            reg_league_wide_most_dropped[year] = reg['league_wide_top3']
            reg_per_team_most_dropped[year] = reg['per_team_top3']
            reg_sloppy_seconds_by_year[year] = reg['sloppy']
            for owner, cnt in reg['team_tally'].items():
                reg_all_time_team_tally[owner] += cnt
            reg_all_time_same_week_flagged += reg['same_week_flagged']

            if year < TEAM_ATTRIBUTION_START_YEAR:
                print(f"    {year}: {len(executed)} executed transactions "
                      f"({len(reg_season_executed)} regular season only) - "
                      f"league-wide only (team attribution unavailable this season)")
            else:
                print(f"    {year}: {len(executed)} executed transactions "
                      f"({len(reg_season_executed)} regular season only), "
                      f"{len(full['sloppy']['players'])} players with 2+ teams full-season "
                      f"({len(reg['sloppy']['players'])} regular-season-only)")

        result = {
            'leagueWideMostDropped': league_wide_most_dropped,
            'perTeamMostDropped': per_team_most_dropped,
            'sloppySeconds': {
                'byYear': sloppy_seconds_by_year,
                'allTimeTeamTally': dict(all_time_team_tally),
                'allTimeSameWeekAddsFlagged': all_time_same_week_flagged
            },
            'regularSeasonOnly': {
                'leagueWideMostDropped': reg_league_wide_most_dropped,
                'perTeamMostDropped': reg_per_team_most_dropped,
                'sloppySeconds': {
                    'byYear': reg_sloppy_seconds_by_year,
                    'allTimeTeamTally': dict(reg_all_time_team_tally),
                    'allTimeSameWeekAddsFlagged': reg_all_time_same_week_flagged
                }
            },
            'metadata': {
                'transactionDataYears': f'{TRANSACTION_DATA_START_YEAR}-{self.end_year}',
                'teamAttributionYears': f'{TEAM_ATTRIBUTION_START_YEAR}-{self.end_year}',
                'notes': (
                    f'{self.start_year}-{TRANSACTION_DATA_START_YEAR - 1}: no transaction data '
                    f'available via ESPN API (confirmed empirically for this league). '
                    f'{TRANSACTION_DATA_START_YEAR}: league-wide most-dropped only - '
                    f'per-team and sloppy-seconds stats are null this season because WAIVER-type '
                    f'transactions have no team attribution (confirmed 100% of that season\'s '
                    f'waiver moves, 0% broken in every other year 2019-2025). '
                    f'"Same-week adds" are pickups that cannot be ordered against a same-week drop '
                    f'from another team (scoring_period only resolves to the week) - these are '
                    f'excluded from team-level tallies rather than arbitrarily attributed. '
                    f'Top-level keys cover the full season including playoff weeks; '
                    f'"regularSeasonOnly" mirrors the same structure filtered to '
                    f'scoring_period <= that season\'s reg_season_count.'
                )
            }
        }

        print(f"  ✓ Transaction analysis complete")
        return result

    def save_output(self, data, filename):
        """Save to JSON"""
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        filepath = os.path.join(OUTPUT_DIR, filename)
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=2)
        
        print(f"  ✓ Saved to {filepath}")
    
    def run(self, skip_bench=False):
        """Main execution"""
        print(f"\n{'='*80}")
        print(" ESPN Fantasy Football Data Collector V2")
        print(" Using espn-api library")
        print(f"{'='*80}")
        print(f"League ID: {self.league_id}")
        print(f"Year Range: {self.start_year}-{self.end_year}")
        print(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        if skip_bench:
            print(f"⚠️  SKIPPING BENCH POINTS CALCULATION")
        self._clear_warnings_file()
        
        if not self.fetch_all_seasons():
            print("\n✗ Not every season could be fetched. Aborting so a partial dataset is not published.")
            sys.exit(1)
        
        all_matchups = self.process_all_matchups()
        if not self.fetch_schedule_data():
            print("\n✗ Per-week scores could not be fetched for every season. Aborting so inconsistent data is not published.")
            sys.exit(1)
        all_matchups = self.apply_weekly_scores(all_matchups)
        self.warn_missing_owners(all_matchups)
        team_stats = self.calculate_team_stats(all_matchups)
        team_stats = self.localize_logos(team_stats)
        team_stats = self.calculate_player_loyalty_stats(team_stats)
        h2h_stats = self.calculate_head_to_head_stats(all_matchups, team_stats)
        superlatives = self.calculate_superlatives(all_matchups)
        two_week_analysis = self.analyze_two_week_playoffs(all_matchups)
        keepers_by_year = self.fetch_keeper_data_by_year()
        season_stats = self.calculate_season_stats(
            all_matchups,
            skip_bench=skip_bench,
            keepers_by_year=keepers_by_year,
        )
        transaction_analysis = self.calculate_transaction_analysis()
        settings_history = self.calculate_settings_history()
        
        print(f"\n{'='*80}")
        print(" Saving Output Files")
        print(f"{'='*80}")
        
        self.save_output(all_matchups, 'matchups.json')
        self.save_output(team_stats, 'team_stats.json')
        self.save_output(h2h_stats, 'head_to_head.json')
        self.save_output(superlatives, 'superlatives.json')
        self.save_output(season_stats, 'season_stats.json')
        self.save_output(transaction_analysis, 'transaction_analysis.json')
        self.save_output(two_week_analysis, 'two_week_playoff_analysis.json')
        self.save_output(settings_history, 'settings_history.json')
        
        metadata = {
            'leagueId': self.league_id,
            'inceptionYear': self.start_year,
            'currentYear': self.end_year,
            'totalSeasons': len(self.all_seasons),
            'totalMatchups': len(all_matchups),
            'totalTeams': len(team_stats),
            'lastUpdated': datetime.now().isoformat()
        }
        self.save_output(metadata, 'metadata.json')
        
        print(f"\n{'='*80}")
        print(" Complete!")
        print(f"{'='*80}")
        print(f"\nTotal matchups: {len(all_matchups)}")
        print(f"Total teams: {len(team_stats)}")
        print(f"Seasons: {self.start_year}-{self.end_year}")
        self.report_warnings()

def main():
    parser = argparse.ArgumentParser(description='ESPN Fantasy Football Data Collector')
    parser.add_argument('--skip-bench', action='store_true', 
                        help='Omit best-bench stats from the output (bench totals are now gathered during the matchup pass, so this no longer saves time)')
    args = parser.parse_args()
    
    collector = ESPNDataCollectorV2(LEAGUE_ID, START_YEAR, END_YEAR, SWID, ESPN_S2)
    try:
        collector.run(skip_bench=args.skip_bench)
    except ESPNAccessDenied as e:
        print(f"\n✗ ESPN access denied - the SWID/espn_s2 cookies have likely expired: {e}")
        sys.exit(2)

if __name__ == "__main__":
    main()