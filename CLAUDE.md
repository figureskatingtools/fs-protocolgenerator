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
  details, `coverPage`/`lastPage` (default|custom), a `files` registry
  (`fileId -> {filename, kind, size, blob}`), and ordered `categories` with slots.
- `uploads/{fileId}_{filename}` — every uploaded PDF/photo/XML.
- `schedule.pdf` — the source schedule (kept for re-parsing).
- `protocols/protocol_*.pdf` — generated outputs.

A file lives in **at most one slot**; files in no slot are "unassigned" (the UI
tray). Drag-and-drop = `assign_file`, which clears the file from its old slot and
sets it in the new one (`structure.assign_file`).

## Assembly order (`assemble.py`)

cover (custom or default) → event-info page → time-schedule page → for each
category in schedule order: *(synchro)* one team page per team → title PDF →
podium page (when a photo or name exists) → total results PDF → per segment
(results → panel → judges details) → last page (custom or default).

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
  A TEAMS file spans several events; `import_rosters` imports one `RegisteredEvent`
  at a time (the UI asks which when more than one is present). Names render
  "FAMILY Given", rosters sorted alphabetically.

## Defaults / backups

Generated in `generate_pages.py`: white "PROTOCOL" cover, "the last page
placeholder", and neutral placeholder boxes for missing podium/team photos. These
are deliberately simple — the real cover and last page are to be designed and
swapped in later.

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
