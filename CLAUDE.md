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
(balanced wrap), dates, location and organizer — using the bundled **Outfit** and
**Manrope** TTFs. The fully-static **last page** is the designer's HTML pre-rendered
once to `assets/last_page.pdf` and inserted as-is. Header/footer are the PNG bands.
A custom uploaded cover/last page/header/footer still overrides the brand default;
`generate_pages.default_cover_page`/`default_last_page` remain as plain fallbacks
only if fonts/assets are missing.

## Backend routes (`function_app.py`)

`list/create/delete/extend_competition`, `get_competition_details`,
`save_event_settings`, `upload_file`, `get_file` (streams bytes for previews),
`assign_file`, `delete_file`, `parse_schedule`, `upload_roster`, `edit_structure`
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
- `dt_partic.py` — calibrated against real ISU OdfBody files. Joins
  **DT_PARTIC_TEAMS** (`<Team>`/`<Composition>`/`<Athlete Code>`) with
  **DT_PARTIC** (`<Participant Code GivenName FamilyName>`) on athlete `Code`.
  One TEAMS + one PARTIC file cover the **whole competition**: `import_rosters`
  groups teams by `RegisteredEvent` and distributes each event's teams to the
  matching category (`function_app._category_for_event` matches by the category's
  stored `code` first, then by event label vs. a synchro category name); events
  with no matching category are returned as `unmatched`. Names render
  "FAMILY Given", rosters sorted alphabetically.
- `results_parser.py` — `parse_top_three(pdf_bytes)` reads ranks 1–3 from a
  category's total-results PDF and returns "<nation/club> - <name>" strings (e.g.
  "SCT - Lotta TERHO", "HTK - Helsinki Finettes"). When a totalResults slot is
  filled (`assign_file`/`upload_file`), the backend pre-fills *empty* podium name
  fields. Calibrated against a real ISU singles sheet (Tikkurila Trophy): the place
  is often glued to the name in layout extraction ("1Lotta TERHO …"), and the
  nation/club is the last non-numeric column (often mixed-case: "KaTa", "PoriTa").
  Still heuristic — refine against synchro totals when a sample is available.

## Defaults / backups

The real cover, last page and header/footer art are now the approved brand kit (see
**Branding** above). `generate_pages.py` still holds the plain fallbacks
(`default_cover_page`, `default_last_page`) used only when the brand fonts/assets
are unavailable, plus neutral placeholder boxes for missing team photos. The podium
page lays the top three out in podium shape (1st centre/highest, 2nd left, 3rd
right) and leaves the photo area empty (no placeholder) when no podium photo is set.

## Local development

```bash
cd frontend && NODE_AUTH_TOKEN=$(gh auth token) npm install   # shared-ui from GH Packages
./start_locally.sh    # Azurite + func + vite + SWA auth emulator
```

## Deploy

Push to `main` → prod via `.github/workflows/deploy.yml`. Manual: `deploy_infra.sh`,
`deploy_backend.sh`, `deploy_frontend.sh`. First-time auth app: `create_auth_app.sh`.

## Remaining cross-repo registration (not in this repo)

To list the tool in the site nav + changelog, in `figureskatingtools-site`:
add `{ id: 'protocolgenerator', label: 'Protocol Generator', subdomain: 'protocols',
enabled: true }` to `packages/shared-ui/src/nav.ts` `DEFAULT_TOOLS` (bump + publish),
and add this repo to `site/public/changelog-sources.json`.
