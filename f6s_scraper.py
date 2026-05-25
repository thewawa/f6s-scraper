#!/usr/bin/env python3
"""
F6S Programs scraper.

Scrapes program/accelerator/event listings from https://www.f6s.com/programs,
diffs them against the programs seen on previous runs, and reports only the
*new* entries.

F6S is server-rendered but sits behind Imperva Incapsula bot protection, so a
plain HTTP request (and even ordinary Playwright) gets a "you might be a bot"
page. We therefore drive real Chrome via patchright, a Playwright fork that
hides the automation fingerprint and passes the challenge.

Listings paginate by URL: https://www.f6s.com/programs?page=N (12 per page),
but every list/filter view hard-caps at ~10 pages (~120 programs) then wraps.
To build a broad baseline we therefore enumerate many filter "slices" (by
country, market, type, and sort) and dedup them — see --baseline.

Usage:
    python f6s_scraper.py --baseline      # FIRST run: broad baseline (~1k programs)
    python f6s_scraper.py                 # delta run: default 10 pages of open-now
    python f6s_scraper.py --pages 25      # scrape 25 pages (capped at ~10 anyway)
    python f6s_scraper.py --sort popular  # open-now (default) | popular | nearby

A real (visible) Chrome window opens while scraping: the site's Imperva bot
wall blocks headless browsers, so headless mode is intentionally not offered.

State is kept in state.json next to this script. New entries from each run are
written to output/new_programs_<timestamp>.json.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# patchright is a drop-in Playwright replacement patched to evade the CDP
# detection used by Imperva Incapsula (the bot wall F6S sits behind). Plain
# playwright — even with stealth tweaks — gets stuck on "Checking your browser".
from patchright.sync_api import TimeoutError as PWTimeoutError, sync_playwright

# The Windows console default codepage (e.g. cp1250) can't encode some glyphs we
# print; force UTF-8 on the streams so logging never crashes. File output uses
# its own UTF-8 encoding regardless.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

BASE = "https://www.f6s.com/programs"
# Sub-tab -> path segment. "open-now" is the default /programs view.
SORTS = {"open-now": "", "popular": "/popular", "nearby": "/nearby"}

# Each list/filter view caps at ~10 pages (~120 programs) then silently wraps to
# page 1. The only way to broaden coverage is to enumerate filter "slices" — each
# is a /programs/<slug> view with its own ~120-program window — and dedup them.
# A wrong/unknown slug just returns default content, which dedups harmlessly.
MAX_PAGES_PER_SLICE = 10

# The three sort views (each surfaces a different ordering of the catalogue).
SORT_SLICE_PATHS = ["", "/popular", "/nearby"]

# Country slices — the biggest coverage multiplier. F6S slugs are the lowercase,
# hyphenated country name.
COUNTRY_SLUGS = [
    "united-states", "united-kingdom", "canada", "australia", "ireland",
    "germany", "france", "spain", "italy", "netherlands", "belgium", "portugal",
    "switzerland", "austria", "sweden", "norway", "denmark", "finland", "iceland",
    "poland", "czech-republic", "slovakia", "hungary", "romania", "bulgaria",
    "greece", "croatia", "slovenia", "serbia", "ukraine", "estonia", "latvia",
    "lithuania", "luxembourg", "malta", "cyprus", "russia", "turkey", "israel",
    "india", "china", "japan", "south-korea", "singapore", "hong-kong", "taiwan",
    "malaysia", "indonesia", "thailand", "vietnam", "philippines", "pakistan",
    "bangladesh", "sri-lanka", "nepal", "kazakhstan",
    "united-arab-emirates", "saudi-arabia", "qatar", "kuwait", "bahrain", "oman",
    "jordan", "lebanon", "egypt", "morocco", "tunisia", "algeria",
    "nigeria", "kenya", "south-africa", "ghana", "ethiopia", "uganda", "tanzania",
    "rwanda", "senegal", "ivory-coast", "cameroon", "zambia", "zimbabwe", "botswana",
    "brazil", "mexico", "argentina", "chile", "colombia", "peru", "uruguay",
    "ecuador", "bolivia", "paraguay", "venezuela", "costa-rica", "panama",
    "guatemala", "dominican-republic", "puerto-rico", "new-zealand",
]

# Market / category slices (the F6S taxonomy is huge; this is a broad common set).
MARKET_SLUGS = [
    "mobile", "web", "software", "software-development", "start-ups", "consulting",
    "finance", "training-coaching", "media", "hardware",
    "artificial-intelligence", "machine-learning", "fintech", "saas", "healthcare",
    "biotech", "medtech", "health-and-wellness", "edtech", "e-commerce",
    "marketplaces", "blockchain", "cryptocurrency", "cybersecurity", "cleantech",
    "energy", "renewable-energy", "agtech", "agriculture", "food-and-beverage",
    "foodtech", "proptech", "real-estate", "insurtech", "legaltech", "hr",
    "marketing", "advertising", "social-media", "gaming", "robotics",
    "internet-of-things", "big-data", "analytics", "cloud", "logistics",
    "transportation", "mobility", "travel", "fashion", "retail", "manufacturing",
    "sustainability", "climate", "space", "deep-tech", "nanotech", "ar-vr",
    "developer-tools", "enterprise-software", "b2b", "b2c", "sports", "music",
    "entertainment", "education", "nonprofit", "impact",
]

# Program-type slices.
TYPE_SLUGS = [
    "accelerators", "incubators", "grants", "events", "competitions",
    "venture-funds", "angel-investors", "coworking",
]

SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = SCRIPT_DIR / "state.json"
OUTPUT_DIR = SCRIPT_DIR / "output"
# Persisted browser profile: keeps the cookie F6S sets once its JS challenge
# passes, so later pages/runs aren't re-challenged.
PROFILE_DIR = SCRIPT_DIR / ".browser-profile"

# Shared listing extractor (a JS arrow fn taking a document/root). Used both
# against the live DOM and against HTML fetched & parsed via DOMParser.
EXTRACT_FN_JS = r"""
(doc) => {
  const txt = el => el ? el.textContent.replace(/\s+/g, ' ').trim() : null;
  return [...doc.querySelectorAll('.result-item')].map(it => {
    const titleA   = it.querySelector('.result-description .title a');
    const profileA = it.querySelector('.organization-picture a');
    const subtitle = txt(it.querySelector('.subtitle'));
    let dates = null, location = null;
    if (subtitle) {
      const parts = subtitle.split('•').map(s => s.trim());
      if (parts.length >= 2) { dates = parts[0]; location = parts.slice(1).join(' • '); }
      else { location = parts[0]; }
    }
    return {
      name:       txt(titleA),
      profileUrl: profileA ? profileA.href : (titleA ? titleA.href : null),
      aboutUrl:   titleA ? titleA.href : null,
      deadline:   txt(it.querySelector('.data-overlay')),
      dates:      dates,
      location:   location,
      funding:    txt(it.querySelector('.result-extra')) || null,
      action:     txt(it.querySelector('.result-action a, .result-action')),
    };
  });
}
"""


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def slug_from_url(url: str | None) -> str | None:
    """Stable dedup key: the profile slug, e.g. .../imaginext-2026 -> imaginext-2026."""
    if not url:
        return None
    path = re.sub(r"https?://[^/]+/", "", url)
    path = path.split("?")[0].split("#")[0].strip("/")
    # Drop trailing /about so the profile and about links collapse to one key.
    path = re.sub(r"/about$", "", path)
    return path or None


def parse_listings(page) -> list[dict]:
    """Extract structured records from the .result-item nodes in the live DOM."""
    return page.evaluate(f"() => ({EXTRACT_FN_JS})(document)")


# In-page paginated fetcher. Runs inside the authenticated browser context, so
# each fetch carries the bot-clearance cookie and is same-origin (no CORS, no
# re-challenge). Walks ?page=1..maxPages, stopping when the view wraps back to
# its first program (the ~10-page cap) or returns a short/empty page.
FETCH_SLICE_JS = r"""
async ({ slicePath, maxPages, delayMs }) => {
  const EXTRACT = %s;
  const base = 'https://www.f6s.com/programs' + slicePath;
  const all = [];
  let page1First = null;
  for (let p = 1; p <= maxPages; p++) {
    let res;
    try { res = await fetch(base + '?page=' + p, { credentials: 'include', cache: 'no-store' }); }
    catch (e) { break; }
    if (!res.ok) break;
    const html = await res.text();
    if (/Checking your browser|might be a bot/i.test(html)) return { bot: true, records: all };
    const doc = new DOMParser().parseFromString(html, 'text/html');
    const recs = EXTRACT(doc);
    if (!recs.length) break;
    const first = recs[0].profileUrl;
    if (p === 1) page1First = first;
    else if (first === page1First) break;   // wrapped to start => cap reached
    all.push(...recs);
    if (recs.length < 12) break;             // last (partial) page
    await new Promise(r => setTimeout(r, delayMs));
  }
  return { bot: false, records: all };
}
""" % EXTRACT_FN_JS


def build_slice_list() -> list[tuple[str, str]]:
    """All (label, slicePath) pairs to enumerate, in priority order."""
    slices: list[tuple[str, str]] = []
    for s in SORT_SLICE_PATHS:
        slices.append((f"sort:{s or 'open-now'}", s))
    for slug in COUNTRY_SLUGS:
        slices.append((f"country:{slug}", f"/{slug}"))
    for slug in MARKET_SLUGS:
        slices.append((f"market:{slug}", f"/{slug}"))
    for slug in TYPE_SLUGS:
        slices.append((f"type:{slug}", f"/{slug}"))
    return slices


def looks_like_bot_wall(page) -> bool:
    body = (page.inner_text("body") or "").lower()
    return "might be a bot" in body or "enable javascript and cookies" in body


def launch_chrome(p):
    """Launch the user's real installed Chrome from a persistent profile.

    headless is intentionally NOT offered — Imperva blocks headless even via
    patchright, so we always run a real (visible) Chrome window. We also do NOT
    set a custom user-agent or inject init scripts; patchright's stealth is
    undone by those, re-exposing the automation fingerprint.
    """
    return p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        channel="chrome",
        headless=False,
        no_viewport=True,
    )


def open_programs(page) -> bool:
    """Load /programs once to clear the JS challenge and set the session cookie."""
    for attempt in range(1, 5):
        try:
            page.goto(BASE, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_selector(".result-item", timeout=20_000)
            return True
        except PWTimeoutError:
            if looks_like_bot_wall(page):
                wait = 4 * attempt
                log(f"  bot wall (attempt {attempt}); waiting {wait}s")
                time.sleep(wait)
            else:
                time.sleep(2)
    return False


def apply_records(records: list[dict], known: dict, run_at: str,
                  new_entries: list[dict]) -> int:
    """Merge scraped records into `known`, collecting first-seen ones. Returns
    the count of newly discovered programs."""
    added = 0
    for rec in records:
        slug = slug_from_url(rec.get("profileUrl"))
        if not slug or not rec.get("name"):
            continue
        rec["slug"] = slug
        if slug not in known:
            rec["first_seen"] = run_at
            known[slug] = rec
            new_entries.append(rec)
            added += 1
        else:
            known[slug]["last_seen"] = run_at
    return added


def scrape(pages: int, sort: str, delay: float) -> list[dict]:
    sort_path = SORTS[sort]
    records: dict[str, dict] = {}  # slug -> record (dedup within this run)

    with sync_playwright() as p:
        context = launch_chrome(p)
        page = context.pages[0] if context.pages else context.new_page()

        for n in range(1, pages + 1):
            url = f"{BASE}{sort_path}?page={n}"
            ok = False
            for attempt in range(1, 4):
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    page.wait_for_selector(".result-item", timeout=20_000)
                    ok = True
                    break
                except PWTimeoutError:
                    if looks_like_bot_wall(page):
                        wait = 5 * attempt
                        log(f"  bot wall on page {n} (attempt {attempt}); waiting {wait}s")
                        time.sleep(wait)
                    else:
                        log(f"  page {n}: no results / timeout (attempt {attempt})")
                        time.sleep(2)
            if not ok:
                log(f"  page {n}: giving up after retries — stopping")
                break

            items = parse_listings(page)
            if not items:
                log(f"  page {n}: empty — assuming end of results")
                break

            new_on_page = 0
            for it in items:
                slug = slug_from_url(it.get("profileUrl"))
                if not slug or not it.get("name"):
                    continue
                it["slug"] = slug
                if slug not in records:
                    records[slug] = it
                    new_on_page += 1
            log(f"  page {n}: {len(items)} items ({new_on_page} unique so far this run)")
            time.sleep(delay)

        context.close()

    return list(records.values())


def scrape_baseline(state: dict, run_at: str) -> list[dict]:
    """Build a broad baseline by enumerating filter slices and deduping them.

    Each slice is fetched (paginated) inside the authenticated browser context.
    State is saved incrementally so a long run is resumable / crash-tolerant.
    Mutates `state` and returns the list of newly discovered programs.
    """
    known: dict = state.setdefault("programs", {})
    new_entries: list[dict] = []
    slices = build_slice_list()
    total = len(slices)
    log(f"Baseline: enumerating {total} filter slices (<={MAX_PAGES_PER_SLICE} pages each)...")

    with sync_playwright() as p:
        context = launch_chrome(p)
        page = context.pages[0] if context.pages else context.new_page()
        if not open_programs(page):
            log("Could not get past the bot wall — aborting baseline.")
            context.close()
            return new_entries

        for i, (label, path) in enumerate(slices, 1):
            try:
                res = page.evaluate(
                    FETCH_SLICE_JS,
                    {"slicePath": path, "maxPages": MAX_PAGES_PER_SLICE, "delayMs": 175},
                )
            except Exception as exc:  # noqa: BLE001 - keep going on a bad slice
                log(f"[{i}/{total}] {label}: error ({type(exc).__name__}); skipping")
                continue

            if res.get("bot"):
                log(f"[{i}/{total}] {label}: hit bot wall mid-run — re-opening session")
                open_programs(page)

            recs = res.get("records", [])
            added = apply_records(recs, known, run_at, new_entries)
            log(f"[{i}/{total}] {label}: {len(recs)} listings, +{added} new "
                f"(total tracked {len(known)})")

            if i % 15 == 0:                 # checkpoint so progress survives a crash
                state["last_run"] = run_at
                save_state(state)
            time.sleep(0.35)                # gentle pacing between slices

        context.close()

    return new_entries


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log("state.json was corrupt — starting fresh")
    return {"programs": {}}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def mirror_state_to_git(total: int, new_count: int) -> None:
    """Commit & push state.json to the configured git remote.

    Used to keep github.com/thewawa/f6s-scraper in sync — the cloud routine
    that runs every Monday clones that repo and reads state.json. Silent
    no-op if not inside a git repo, nothing changed, or push fails (the
    scrape itself succeeded; the mirror is best-effort).
    """
    import subprocess
    try:
        inside = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=str(SCRIPT_DIR), capture_output=True, text=True,
        )
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return
        subprocess.run(["git", "add", "state.json"], cwd=str(SCRIPT_DIR), check=True)
        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet", "--", "state.json"],
            cwd=str(SCRIPT_DIR),
        )
        if diff.returncode == 0:
            log("  mirror: state.json unchanged, nothing to push")
            return
        msg = f"state: {total} programs ({new_count} new) @ {datetime.now():%Y-%m-%d %H:%M}"
        subprocess.run(["git", "commit", "-q", "-m", msg], cwd=str(SCRIPT_DIR), check=True)
        push = subprocess.run(
            ["git", "push", "-q"], cwd=str(SCRIPT_DIR),
            capture_output=True, text=True, timeout=60,
        )
        if push.returncode == 0:
            log(f"  mirror: pushed state.json ({total} programs)")
        else:
            log(f"  mirror: push failed: {(push.stderr or '').strip()[:160]}")
    except Exception as exc:  # noqa: BLE001 - mirror is best-effort
        log(f"  mirror: skipped ({type(exc).__name__}: {exc})")


def main() -> int:
    ap = argparse.ArgumentParser(description="Scrape new F6S program entries.")
    ap.add_argument("--pages", type=int, default=10,
                    help="Number of result pages to scrape (12 programs each). Default: 10.")
    ap.add_argument("--sort", choices=list(SORTS), default="open-now",
                    help="Which F6S list to scrape. Default: open-now.")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="Seconds to wait between pages (be polite). Default: 1.5.")
    ap.add_argument("--baseline", action="store_true",
                    help="Build a broad baseline by enumerating all filter slices "
                         "(countries, markets, types, sorts) and deduping. Use this "
                         "for the first run; ignores --pages/--sort.")
    args = ap.parse_args()

    run_at = datetime.now(timezone.utc).isoformat()
    state = load_state()
    known: dict = state.setdefault("programs", {})

    if args.baseline:
        new_entries = scrape_baseline(state, run_at)
    else:
        log(f"Scraping up to {args.pages} pages of '{args.sort}' …")
        scraped = scrape(args.pages, args.sort, args.delay)
        log(f"Collected {len(scraped)} unique programs this run.")
        new_entries = []
        apply_records(scraped, known, run_at, new_entries)

    state["last_run"] = run_at
    save_state(state)
    mirror_state_to_git(total=len(known), new_count=len(new_entries))

    OUTPUT_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_file = OUTPUT_DIR / f"new_programs_{stamp}.json"
    out_file.write_text(json.dumps(new_entries, indent=2, ensure_ascii=False), encoding="utf-8")

    print()
    if new_entries:
        log(f"Found {len(new_entries)} NEW program(s):")
        shown = new_entries if len(new_entries) <= 40 else new_entries[:40]
        for r in shown:
            loc = r.get("location") or "-"
            dl = r.get("deadline") or ""
            print(f"  - {r['name']}  [{loc}] {dl}")
            print(f"      {r.get('profileUrl')}")
        if len(new_entries) > len(shown):
            print(f"  ... and {len(new_entries) - len(shown)} more (see {out_file.name})")
    else:
        log("No new programs since last run.")
    log(f"New entries written to {out_file}")
    log(f"Total programs tracked: {len(known)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
