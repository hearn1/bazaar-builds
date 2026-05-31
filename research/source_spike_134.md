# Source Spike #134: bazaarplanner.com + thebazaarzone.com

Spike date: 2026-05-31  
Issue: hearn1/bazaar-builds#134  
Researcher: Claude Code (automated)

---

## Summary verdicts

| Source | Verdict | One-line reason |
|---|---|---|
| bazaarplanner.com | **DEFER** | Statistical data promised but not yet live; no usable endpoint exists today. |
| thebazaarzone.com | **NO-GO** | Season 2 only (last updated May 2025); no Season 3 heroes; /builds/ is JS-rendered; stale relative to Mobalytics coverage. |

---

## bazaarplanner.com

### What it is
A board-simulator tool for The Bazaar. The site hosts a BazaarPlannerMod client-side tracker that users install to record their runs locally. The "Builds" tab is intended to aggregate telemetry from participating users into win-rate statistics and leaderboard data.

### Fetch results
- `GET https://www.bazaarplanner.com/` — 200 OK, static HTML. Homepage explicitly states the Builds feature is "currently collecting data / in development"; no live data displayed.
- `GET https://www.bazaarplanner.com/builds` — 403 Forbidden (not accessible via static fetch).
- `GET https://www.bazaarplanner.com/api/builds` — 403 Forbidden (not accessible via static fetch).
- No JSON/XHR endpoint could be confirmed via WebSearch, site: search, or indirect fetches.

### Structure assessment
- **Data type**: Aggregated user telemetry (simulated win-rate vs top builds). Would be a statistical signal, not editorial.
- **Item name determinism**: Unknown — no data is exposed. The mod tracker presumably captures item names from client state but the output format is undocumented.
- **Patch-scoping**: Unknown — no documented temporal windowing.
- **Freshness**: Moot while no data is live.
- **ToS/access**: No robots.txt or ToS observed. The /builds and /api routes returned 403, which may indicate authentication-gating rather than a public endpoint.

### Recommendation
**DEFER.** The statistical premise is sound — if and when the Builds tab goes live, it could be a valuable complement to bazaardb. The 403 on /builds and /api/builds suggests the data layer may exist but is not yet public (or is protected). Re-evaluate when the feature is announced as live. No HTML scraping path exists as a fallback; the site explicitly gates the data behind a JavaScript-rendered authenticated view.

---

## thebazaarzone.com (DotGG)

### What it is
A DotGG-network editorial site covering The Bazaar. Publishes tier lists, hero guides, meta analysis, and patch notes. Not statistically derived — editorial content authored by the site's team.

### Fetch results
- `GET https://thebazaarzone.com/` — 200 OK. Homepage shows static HTML nav with hero guides, a builds section, and a patch news feed. The most recent patch entries in the feed are "Hotfix: June 6, 2025" and "Season 3 Patch Notes 3.0.0: June 4, 2025" (from the homepage static render), suggesting the site has Season 3 awareness in its news feed but editorial build content has not caught up.
- `GET https://thebazaarzone.com/tier-list/` — 200 OK. **Static HTML with embedded tier data.** Covers Mak, Pygmalien, Dooley, Vanessa with tier ratings (A+, A, B+, B, B-) and named builds (e.g. Peacewrought, Self Poison, Weaponized Core, Tortuga). Editorial commentary included. **Updated: May 20, 2025 / Version 2.0.0.** Season 3 heroes (Jules, Stelle, Karnok) are absent.
- `GET https://thebazaarzone.com/metagame/` — 200 OK. Index page linking to meta state articles; most recent is "Season 2 Patch 2.0.0" dated May 22, 2025.
- `GET https://thebazaarzone.com/builds/` — Page loads but actual build data is **JS-rendered** (displays a "Loading…" message in place of content; AdBlock notice). Not reachable via static fetch.
- WebSearch: No indexed content from thebazaarzone.com for Season 3 heroes (Jules, Stelle, Karnok). Google's title for tier-list/ says "May 2026" but the page content date is May 2025 — likely a stale search-snippet title.

### Structure assessment
- **Data type**: Editorial — named build tiers, item mentions, short commentary. No sample sizes, no win rates, no core/support classification.
- **Item name determinism**: Moderate. Items appear as inline text in editorial prose and HTML elements, not structured JSON fields. Less reliable than Mobalytics `window.__PRELOADED_STATE__`.
- **Season coverage**: Season 2 only as of spike date. No evidence of Season 3 (Jules, Stelle, Karnok) editorial coverage on the builds/tier-list pages.
- **Render requirement**: The primary `/builds/` aggregation page is JS-rendered and inaccessible via static fetch. The tier-list page is static but stale.
- **Value as third editorial voice**: The issue (#134) noted its sole value is as a third agreeing editorial voice feeding cross-source agreement (after #133). With stale Season 2 data only and JS-rendered primary build listing, it cannot currently serve that role.
- **ToS**: DotGG network. No explicit scraping prohibition observed, but the JS rendering barrier limits viable fetch approaches to browser-render.

### Recommendation
**NO-GO.** Three compounding blockers: (1) Season 3 heroes entirely absent — cannot contribute evidence for Jules, Stelle, or Karnok builds; (2) Last editorial update May 2025 / Season 2, over a year stale; (3) The primary aggregation page (`/builds/`) requires browser-render to access. Even if a browser-render path were added, the content would be stale until the site catches up to Season 3. Revisit only if the site publishes Season 3 content and a browser-render path is available.

---

## Notes on fetch methodology

- All fetches via WebFetch (static HTML only; no JS execution).
- thebazaarzone.com /builds/ confirmed JS-rendered — the negative static-fetch result is itself a valid finding per task spec.
- bazaarplanner.com /builds and /api/builds returned 403; no public endpoint confirmed via WebSearch either.
- No fabricated data: all findings reflect actual HTTP responses and visible static HTML content.
