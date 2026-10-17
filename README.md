# The Other League — League Stats

A stats site for **The Other League**, a 10-team PPR ESPN fantasy football league running continuously since 2014. Built and maintained by the league commissioner.

🔗 **Live site:** https://bs0l.github.io/the-other-league/

## What's here

Every score, matchup, and roster move going back to 2014 is pulled straight from the ESPN Fantasy API and turned into a browsable stats site: standings, head-to-head records, all-time superlatives, season-by-season breakdowns, playoff history, and more.

- **All-time records** — career win/loss, points for/against, championships, playoff appearances, per-owner history across team-name and ownership changes
- **Head-to-head** — every pairing's full history, streaks, and margins
- **Superlatives** — highest/lowest scores, biggest blowouts, closest wins, longest streaks, weekly high/low scorers. Scores are true single-week scores: every playoff round is two weeks long, so each playoff matchup is split into its two weeks using ESPN's per-week scoring data, and two-week matchup records are shown beside the single-week ones where they apply
- **Season pages** — a dedicated breakdown for every year the league has played
- **Two-week playoff analysis** — every playoff matchup (all of them are two weeks long) compared week by week: how often a team trailing after week 1 won on the combined score (its own page)
- **Transactions & draft** — trade history, waiver activity, draft-pick retention

## How it works


- A Python collector (built on [`espn-api`](https://github.com/cwendt94/espn-api)) fetches every season's league data and computes stats into a set of JSON files
- A static HTML/JS frontend reads those JSON files and renders the site — no backend, no database
- Data refreshes automatically on a weekly cron job and is published via GitHub Pages

This repo holds the frontend (HTML/JS/CSS) that's deployed to GitHub Pages. The data collector runs separately on a home server and pushes updated JSON here.

## A note on data completeness

ESPN's API has real gaps and quirks in older seasons (some years lack reliable keeper, transaction, or bench data). Where the underlying data isn't trustworthy, this site labels it as such or shows it as N/A rather than guessing — accuracy over completeness.

## Tech

- Python (data collection) + `espn-api`
- Vanilla HTML/CSS/JS, Chart.js for visualizations
- Hosted on GitHub Pages

---

## Operations (for maintainers)

The collector script and a couple of credential/config files are **not** part of this repo — they're excluded via `.gitignore` because they contain private league data (real names, ESPN auth cookies) and live only on the home server that runs the collector. Only the generated `output/*.json` files and the frontend get pushed here.

### Pulling frontend changes before testing

The scheduled job already does a `git fetch` + hard reset before every run, so scheduled runs are always on the latest committed frontend/output. For a manual test run, pull first so you're not testing against a stale checkout:

```bash
ssh pi@<server-ip>
cd /home/pi/fantasystats/tot
git fetch
git reset --hard origin/main
```

### Running the collector manually

```bash
ssh pi@<server-ip>
source ~/fantasy-env/bin/activate
cd /home/pi/fantasystats/tot
python tot_collector.py
```

Add `--skip-bench` to omit the best-bench stats from the output (bench totals are now gathered during the matchup pass, so this no longer saves time):

```bash
python tot_collector.py --skip-bench
```

Output JSON lands in `output/*.json` in that directory. A manual run does **not** commit or push — that only happens via the scheduled wrapper script described below. Diff the output against what's already committed before pushing anything by hand.

### Files that must be manually copied to the server

These are intentionally gitignored and never travel with a normal `git pull` — they have to be copied over by hand (e.g. via `scp`) whenever they change:

| File | Purpose |
|---|---|
| `tot_collector.py` | The collector script itself |
| `tot_keepers.py` | Keeper identities, point overrides, and notes — contains real owner/player names |
| `.env` | ESPN auth cookies, shared across leagues this server runs |

```bash
scp tot_collector.py pi@<server-ip>:/home/pi/fantasystats/tot
scp tot_keepers.py pi@<server-ip>:/home/pi/fantasystats/tot
```

The `.env` file should be `chmod 600` on the server since it holds auth cookies.

### Local development (Windows)

Test runs on a dev PC mirror the Pi layout: the `.env` and the virtual environment live one level **above** the repo, so credentials and packages can never be committed. (The Pi's venv is `~/fantasy-env`; the local one is simply `venv`.)

```
<projects-folder>\
├── .env                     # TOT_SWID, TOT_S2, etc
├── venv\
├── the-other-league\        # this repo (plus tot_keepers.py, gitignored)
└── [other fantasy leagues]\
```

The collectors call `load_dotenv()`, which searches the script's folder and then each parent folder for a `.env`, so no path configuration is needed. On the Pi the hardcoded `/home/pi/fantasystats/.env` path is loaded first, and `load_dotenv()` never overrides variables that are already set.

**One-time setup (PowerShell):**

```powershell
cd <projects-folder>
python -m venv venv
.\venv\Scripts\Activate.ps1
pip install espn-api python-dotenv requests
```

If activation is blocked, run `Set-ExecutionPolicy -Scope Process Bypass` and try again. Your prompt should start with `(venv)` once it's active.

**Each test session:**

```powershell
cd <projects-folder>
.\venv\Scripts\Activate.ps1
cd the-other-league
python tot_collector.py --skip-bench
```

In a second terminal, from the same repo folder, serve the site and open <http://localhost:8000>:

```powershell
python -m http.server 8000
```

Notes:

- The leading `.\` on the activate command is required in PowerShell. Without it, PowerShell treats the path as a module name and fails with "module could not be loaded".
- `tot_keepers.py` must sit in the repo folder next to the collector; it is imported directly and is not searched for in parent folders.
- `output/*.json` is tracked in git, so a local run overwrites committed files. Check `git status` before committing frontend changes, and use `git restore output/` to discard test data.
- `%-d` in the settings-history date formatter isn't valid on Windows; the existing fallback produces zero-padded days locally, which is harmless.
- If you ever create the venv inside the repo folder instead, add `venv/` to `.gitignore`.

### Scheduling

A cron job runs a wrapper shell script weekly during the season, which:
1. `git fetch` + `git reset --hard origin/main` in the league directory
2. Runs the collector via the venv Python
3. On success, commits/pushes the updated `output/*.json` and sends a success notification (or a "completed with warnings" notification listing them - see below)
4. On failure (collector error or push failure), sends a failure notification with the trailing log lines instead

Logs append indefinitely to a dedicated log file.

### Configuration notes

- `END_YEAR` in the collector is computed dynamically (current year from September onward, previous year otherwise) — no manual bump needed each season
- Auth cookies are read from environment variables at runtime rather than hardcoded in the script
- Team logos: images uploaded directly to ESPN need an ESPN login to load, so a visitor's browser can't show them. The collector downloads those into `output/logos/` (named by ESPN's image id, so unchanged logos are never re-downloaded; unused files are deleted) and points `logoUrl` at the local copy. These images are committed to the public repo, which is why `run_tot.sh` stages the whole `output/` folder (`git add -A output`) rather than only `output/*.json`. Logos on public hosts (ESPN clipart, imgur) are linked as-is, and a team with no loadable logo shows an initials badge
- Score adjustments: ESPN's matchup total includes scoring adjustments (a dues bonus or penalty, for example, shown on the scoreboard as "Scoring Adjustment") but its per-week points do not. When an adjustment exactly explains that gap, the collector counts it in the week's score, so single-week scores match the ESPN scoreboard; an adjustment inside a two-week matchup can't be assigned to a week and raises a warning. A gap that no adjustment explains aborts the run instead of publishing inconsistent numbers
- Warnings: the collector collects data-quality warnings (teams with no resolvable owner, failed requests, skipped weeks, keeper points that may be undercounted, ...) and prints them in one block at the very end of the log. If `TOT_WARNINGS_FILE` is set (`run_tot.sh` sets it), the same text is written there, and `run_tot.sh` includes it in the notification email; no file is written when there are no warnings
- Exit codes: `1` means a season (or its per-week scores) could not be fetched or the data was inconsistent, so nothing is written or pushed; `2` means the ESPN session cookies expired and triggers the targeted "cookies expired" notification rather than a generic failure email

---

*This is a personal project for a private fantasy football league — not affiliated with ESPN.*