# CLAUDE.md — Protocol Generator architecture

This is a sibling of `fs-judgepapers` and `fs-scoremodifier` on
`figureskatingtools.com`. It assembles a competition **protocol** (one bound PDF)
from organizer-supplied result PDFs and photos. The tool is mostly *PDF creation
and connecting files together*; missing graphics fall back to generated defaults.

## Stack

- **Frontend** — vanilla TypeScript + Vite SPA (`frontend/`), shared nav from
  `@figureskatingtools/shared-ui`, the shared "Protocol" CSS tokens. No framework.
  A zero-dependency Node proxy (`frontend/server.js`) serves the build, exposes
  `/userinfo`, and proxies `/api/*` to the Function App (forwarding the Easy Auth
  user email + a shared secret).
- **Backend** — Python Azure Functions (`infra/functions/`), `AuthLevel.ANONYMOUS`
  behind the proxy. `pypdf` merges PDFs, `reportlab` draws generated pages,
  `pillow` embeds photos.
- **Storage** — Azure Blob (container `fs-protocolgenerator`) holds each
  competition folder; Tables `competitions` (permanent registry, soft-delete) and
  `generatedprotocols` (SAS download links).
- **Hosting** — App Service Web App + Flex-Consumption Function App, custom domain
  `protocols.figureskatingtools.com`. IaC in `infra/` (subscription-scoped Bicep).

## Data model — `metadata.json` (the structure document)

Each competition is a blob folder `"{name}-{id}"` containing:

- `metadata.json` — the **structure** (see `infra/functions/structure.py`): event
  details, `coverPage`/`lastPage`/`header`/`footer` (default|custom — header/footer
  are the optional competition-wide page chrome), a `files` registry
  (`fileId -> {filename, kind, size, blob}`), and ordered `categories` with slots.
- `uploads/{fileId}_{filename}` — every uploaded PDF/photo/XML.
- `schedule.pdf` — the source schedule (kept for re-parsing).
- `protocols/protocol_*.pdf` — generated outputs.

A file lives in **at most one slot**; files in no slot are "unassigned" (the UI
tray). Drag-and-drop = `assign_file`, which clears the file from its old slot and
sets it in the new one (`structure.assign_file`).

Synchro teams carry two photo slots: `team.photo` (the competition / kiss'n'cry
picture, slot kind `teamPhoto`) and `team.photoFallback` (an accreditation
fallback, slot kind `teamPhotoFallback`). Fallbacks are bulk-imported from one
optional ZIP (`upload_fallback_photos`, see `fallback_photos.py`, size cap
`MAX_ZIP_UPLOAD_SIZE` 100 MiB ≈ the platform HTTP limit): folders named like
categories or events (`SM-seniorit/`, `Aikuiset/`) holding
`Team-Name_Club-Name.jpeg` images. Matching is by normalized team name across the
whole competition — NFKD diacritic-folded, so ASCII filenames ("Helsinki-JaaLeidit")
match XML names ("Helsinki JääLeidit") — with the club part and the
folder-vs-category name only as ranking hints; matched images are
re-encoded (≤2000 px JPEG) and assigned to `photoFallback` (replacing any prior
fallback file), unmatched ones land in the tray and are reported. Generation uses
photo → fallback → placeholder.

## Assembly order (`assemble.py`)

cover (custom or default) → event-info page → time-schedule page → for each
category in schedule order: *(synchro)* one team page per team → protocol head
page PDF (the category's `titlePdf` slot; "Protocol Head Page" in the UI) →
podium page (when a photo or name exists) → total results PDF → per segment
(results → panel → judges details) → last page (custom or default).

Every *generated interior* page (event-info, schedule, podium, synchro team — not
the cover/last page, not inserted result PDFs) is stamped with a competition-wide
**header/footer band** — see `generate_pages._draw_chrome`. The band uses the
competition's uploaded `header`/`footer` graphic (drawn edge-to-edge) when present,
else the approved brand bands from `branding.py` (`assets/header.png` with the
competition name + dates·location printed to the right of its divider;
`assets/footer.png` slogan). The footer is omitted when `structure.footerEnabled`
is false (UI checkbox). `assemble._chrome_band` resolves any custom graphics' bytes
once and passes a `chrome` dict (custom bytes, footer_enabled, name, dates,
location) to each page builder.

**Branding (`branding.py` + `assets/` + `fonts/`).** The approved Figureskatingtools
brand kit (from the designer's `final/` folder): the **cover** is reproduced in
reportlab (`branding.cover_page`) from `cover.html` — gradient hairline, skate
lockup, watermark, "OFFICIAL PROTOCOL" eyebrow, then dynamic competition name
(balanced wrap), dates, location and organizer — using the bundled **Raleway** TTFs
(static weights 400/500/600/700, instanced from the Google Fonts variable font).
The static **last page** is likewise reproduced in reportlab
(`branding.draw_last_page`, geometry lifted from the designer's pre-render, which
remains at `assets/last_page.pdf` as the fonts-missing fallback). Header/footer are
the PNG bands.
The **competition-information page** (`branding.draw_event_info`, from
`competitionInformation.html`) and the **podium page** (`branding.draw_podium`,
from `podium.html`) are likewise reproduced in reportlab: the page-2 eyebrow +
title + gradient rule, label/value rows (Organiser, Authorised by, Held in,
Venue, Dates — each row drawn only when its value is set) and a Categories ·
Competition Units · Performances stat row (`assemble._competition_stats`, shown
only when non-zero). The source of truth is each segment's `unitCount` — the
competition units that performed it, auto-filled from the segment's results PDF on
upload/assign (`function_app._fill_segment_count_from_results`, mirroring the
podium autofill) and user-correctable via `edit_structure` `set_segment`
(`unitCount`) / the per-segment "Units" input. Performances = Σ segment counts;
a category's units = its largest segment; both fall back to a live
`results_parser.count_result_rows` parse (then the synchro team count) when a
category has no segment counts. The podium's 2-1-3
rostrum (1st centre/highest on the brand gradient, rank medallions, "<club> -
<name>" split into name + club, photo cover-cropped into a rounded box with a
gradient hairline, or plain white space when no photo). `generate_pages.event_info_page`
/`podium_page` delegate to these when `branding.fonts_available()` and fall back to
the plain ISU-style layouts otherwise. A custom uploaded cover/last page/header/footer
still overrides the brand default; `generate_pages.default_cover_page`/`default_last_page`
remain as plain fallbacks only if fonts/assets are missing.

## Backend routes (`function_app.py`)

`list/create/delete/extend_competition`, `get_competition_details`,
`save_event_settings`, `upload_file`, `get_file` (streams bytes for previews),
`assign_file`, `delete_file`, `parse_schedule`, `import_rosters`,
`upload_fallback_photos` (bulk fallback-picture ZIP), `edit_structure`
(manual add/remove/set ops), `generate_protocol`, plus the daily auto-deletion
timer.

## Parsers (calibration pending)

- `schedule_parser.py` — `parse_schedule_data(bytes)` auto-detects the format:
  - **DT_SCHEDULE OdfBody XML (preferred)** — structured `<Unit>` rows with ISO
    times, explicit discipline/segment in `ItemName`, the ice rink in
    `VenueName` (auto-fills `event.rink`/`event.dates`), and an ISU `Unit Code`
    used to group units into categories (so Advanced Novice L1 `…ADVNOV----` and
    L2 `…ADVNOV--01` stay distinct while a category's Short Program + Free Skating
    merge). The category `code` is stored for future roster auto-linking.
  - **Schedule PDF (fallback)** — calibrated against the Finnish "COMPETITION
    SCHEDULE" export: glued start/finish times, 2-space category|segment columns,
    multi-day segment merge, and synchro detected from the document title
    ("MUODOSTELMALUISTELUN…") via `structure.discipline_signal`.
- `dt_partic.py` — calibrated against real ISU/TAIKKARI OdfBody files. Joins
  **DT_PARTIC_TEAMS** (`<Team>`/`<Composition>`/`<Athlete Code>`) with
  **DT_PARTIC** (`<Participant Code GivenName FamilyName>`) on athlete `Code`.
  One TEAMS + one PARTIC file cover the **whole competition**. Names render
  "FAMILY Given", rosters sorted alphabetically; Name/Organisation are stripped
  (real exports carry trailing spaces).
- `roster_matching.py` — pure team→category placement, used by `import_rosters`.
  Teams register per **event** (`RegisteredEvent="…MLAIKU----"`) but often compete
  per **block** ("Aikuiset, Mupi L1"/"L2"; DT_SCHEDULE codes `…MLAIKU--01`), and
  block membership exists *only* in each block's total-results PDF. So matching is
  **results-first**: `match_teams` places a team into the category whose result
  rows name it (exact normalized name, then word-subset fuzzy — "JääLeidit" ⊂
  "Helsinki JääLeidit" — with the club abbreviation as tiebreak), and only falls
  back to the registered event when that maps to exactly one category
  (`categories_for_event`: trailing-dash-stripped code prefix → event-token
  fragment vs. category-name words, "MLAIKU"→"Aikuiset…" → ISU/Finnish label).
  Everything else is *reported*, never guessed: `withdrawn` (registered, on no
  sheet, all blocks parsed) or `unmatched` with an actionable reason. The report
  persists as `structure["rosterImport"]` (UI panel), the XMLs are archived under
  `rosters/`, and filling a `totalResults` slot triggers an automatic re-match
  (`_rematch_after_results`; the auto pass never moves an already-placed team on
  a non-exact hit). `_upsert_team` matches competition-wide and *moves* teams
  (keeping id/photos) so re-imports never duplicate.
- `results_parser.py` — `parse_result_rows(pdf_bytes)` extracts every placement
  row (`{rank, name, club}`; first occurrence per rank) and feeds podium pre-fill
  (`parse_top_three` → "<nation/club> - <name>", e.g. "HTK - Helsinki Finettes"),
  the info-page tallies (`count_result_rows`) and roster matching. When a
  totalResults slot is filled (`assign_file`/`upload_file`), the backend pre-fills
  *empty* podium name fields. Calibrated against real singles (Tikkurila Trophy)
  and synchro sheets: the place is often glued to the name in layout extraction
  ("1Shadows ETK 69.29 1"), and the nation/club is the last non-numeric column
  (often mixed-case: "KaTa", "SeiTL").

## Defaults / backups

The real cover, last page and header/footer art are now the approved brand kit (see
**Branding** above). `generate_pages.py` still holds the plain fallbacks
(`default_cover_page`, `default_last_page`) used only when the brand fonts/assets
are unavailable, plus neutral placeholder boxes for missing team photos. The podium
page lays the top three out in podium shape (1st centre/highest, 2nd left, 3rd
right) and leaves the photo area empty (no placeholder) when no podium photo is set.
The synchro team page sizes its photo box dynamically — roster rows (2 columns,
3 past 44 skaters) are reserved first so up to 32 names always fit above the
footer band, and the photo takes the remaining height (clamped 60–130 mm).

## Local development

```bash
cd frontend && NODE_AUTH_TOKEN=$(gh auth token) npm install   # shared-ui from GH Packages
./start_locally.sh    # Azurite + func + vite + SWA auth emulator
```

Backend tests (synthetic fixtures only — never commit real competition data,
the OdfBody exports contain minors' personal data):

```bash
cd infra/functions && uv run --with-requirements requirements-dev.txt python -m pytest tests -q
```

## Deploy

Push to `main` → prod via `.github/workflows/deploy.yml`. Manual: `deploy_infra.sh`,
`deploy_backend.sh`, `deploy_frontend.sh`. First-time auth app: `create_auth_app.sh`.

## Remaining cross-repo registration (not in this repo)

To list the tool in the site nav + changelog, in `figureskatingtools-site`:
add `{ id: 'protocolgenerator', label: 'Protocol Generator', subdomain: 'protocols',
enabled: true }` to `packages/shared-ui/src/nav.ts` `DEFAULT_TOOLS` (bump + publish),
and add this repo to `site/public/changelog-sources.json`.
