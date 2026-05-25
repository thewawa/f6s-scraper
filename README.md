# F6S Programs Scraper

Scrapes program / accelerator / event listings from
[f6s.com/programs](https://www.f6s.com/programs), remembers what it has seen
before, and on each run reports only the **new** entries.

## How it works

- F6S sits behind **Imperva Incapsula** bot protection (the `reese84` cookie).
  A plain HTTP request — and even ordinary Playwright, headless or not — gets
  stuck on a *"Checking your browser / we think you might be a bot"* page.
- To get past it the scraper drives your real installed **Chrome** via
  [`patchright`](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright), a Playwright
  fork patched to hide the automation fingerprint Imperva looks for.
- Listings paginate by URL: `…/programs?page=N`, **12 programs per page**.
- Each listing's profile slug (e.g. `imaginext-2026`) is used as a stable key.
  Slugs are diffed against `state.json`; anything not seen before is "new".

> **Headless is not supported.** Imperva blocks headless browsers even through
> patchright, so a real (visible) Chrome window opens while scraping. Don't
> interact with it; it closes itself when the run finishes.

## Setup (one time)

```powershell
cd "C:\Users\User\Documents\claude code\f6s-scraper"
pip install -r requirements.txt
python -m patchright install chromium    # installs the browser driver
```

You also need Google **Chrome** installed (the script launches it via
`channel="chrome"`).

## The ~120-per-view cap (important)

Each list/filter view (`/programs`, `/programs/popular`, `/programs/software`,
`/programs/germany`, …) **hard-caps at ~10 pages / ~120 programs**, then silently
serves page 1 again. The *"15,025 results"* figure is a grand total, not a
browsable list — most of it is closed/historical programs not surfaced anywhere.

So there are two modes:

| Mode | What it does | When |
|------|--------------|------|
| `--baseline` | Enumerates ~180 filter slices (countries, markets, types, sorts) and dedups them into one big set (~1,000+ programs). | **First run**, to seed the baseline. |
| *(default)* | Scrapes the `open-now` view (top ~120 by deadline). Fast. | **Recurring runs**, to catch newly-added programs. |

> Why not a proxy service (ScraperAPI etc.)? It wouldn't help: the cap is
> server-side per query (identical from any IP), and authenticated requests
> aren't rate-limited here. Broadening coverage is a *query-enumeration*
> problem, which proxies don't solve. A proxy would only matter for headless
> server-side runs or massive continuous volume.

## Usage

```powershell
# FIRST: build the baseline (opens Chrome ~5-10 min; enumerates all slices)
python f6s_scraper.py --baseline

# THEN: recurring delta runs — report only programs added since last run
python f6s_scraper.py                 # default: 10 pages of "open now"
python f6s_scraper.py --sort popular  # open-now (default) | popular | nearby
python f6s_scraper.py --delay 2.0     # seconds between pages (be polite; default 1.5)
```

After the baseline, anything with a slug not already in `state.json` is treated
as new. Re-running `--baseline` periodically (e.g. weekly) refreshes coverage
and still flags only genuinely new slugs.

> **Don't run two instances at once** (or back-to-back within a few seconds):
> they share the `.browser-profile` Chrome profile, and the lock contention
> makes a run come up empty. Let one finish before starting the next.

## Output

- **`state.json`** — the cumulative database of every program ever seen, keyed
  by slug. Each record stores `first_seen` and `last_seen` timestamps. Delete
  this file to reset and treat everything as new again.
- **`output/new_programs_<timestamp>.json`** — just the new entries from that
  run. One file per run.

Each record looks like:

```json
{
  "name": "ImagiNext 2026",
  "profileUrl": "https://www.f6s.com/imaginext-2026",
  "aboutUrl": "https://www.f6s.com/imaginext-2026/about",
  "deadline": "by May 23",
  "dates": "May 22-23",
  "location": "Mumbai, India",
  "funding": null,
  "action": "Book by May 23 More info",
  "slug": "imaginext-2026",
  "first_seen": "2026-05-22T12:56:27+00:00"
}
```

## Scheduling

To check for new programs automatically, run it on a schedule with Windows Task
Scheduler. Because the run needs a visible Chrome window, schedule it under your
own (interactive) user session — *"Run only when user is logged on"* — rather
than as a background service. A daily trigger works well:

```
Program:   python
Arguments: "C:\Users\User\Documents\claude code\f6s-scraper\f6s_scraper.py"
Start in:  C:\Users\User\Documents\claude code\f6s-scraper
```

(Run `--baseline` manually once first to seed `state.json`; the scheduled job
then just catches new additions in the default `open-now` view. Optionally add a
second weekly task with `--baseline` to refresh broad coverage.)

## Notes / limits

- The `.browser-profile/` folder is a dedicated Chrome profile that persists the
  bot-clearance cookie, so repeat runs skip the challenge. It's separate from
  your normal Chrome profile and is safe to delete (you'll just be re-challenged
  once on the next run).
- The default `open-now` list is ordered by application deadline, so the deepest
  pages are the furthest-out deadlines — not necessarily the most recently
  *added* programs. The diff still catches any genuinely new slug regardless of
  where it appears in the list.
- Be considerate: keep `--pages` and request rate reasonable.
