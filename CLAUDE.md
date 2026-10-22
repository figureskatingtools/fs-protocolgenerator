# CLAUDE.md — Protocol Generator architecture

This is a sibling of `fs-judgepapers` and `fs-scoremodifier` on
`figureskatingtools.com`. It assembles a competition **protocol** (one bound PDF)
from organizer-supplied result PDFs and photos. The tool is mostly *PDF creation
and connecting files together*; missing graphics fall back to generated defaults.

**This repo is backend-only.** The UI lives in the `figureskatingtools-site` repo
and is served at `https://figureskatingtools.com/protocolgenerator/`; that site's
router handles the Entra login and proxies `/protocolgenerator/api/*` to this
Function App with `x-proxy-secret` + `x-forwarded-user-tid`/`-oid`/`-email`/`-name`
(+ `-admin`) (see **PROXY-CONTRACT.md** and **Competition access** below).

## Stack

- **Backend** — Python Azure Functions (`infra/functions/`), `AuthLevel.ANONYMOUS`
  behind the site router's proxy. `pypdf` merges PDFs, `reportlab` draws generated
  pages, `pillow` embeds photos.
- **Storage** — Azure Blob (container `fs-protocolgenerator`) holds each
  competition folder; Tables `competitions` (permanent registry, soft-delete) and
  `generatedprotocols` (one row per generated PDF: file name, size,
  description — no link; links are minted per request, see **Competition access**).
- **Hosting** — Flex-Consumption Function App only (no Web App, no Easy Auth
  provider, no DNS/custom domain in this repo). IaC in `infra/`
  (subscription-scoped Bicep: `modules/storage.bicep`, `modules/function.bicep`,
  `modules/roleassignment.bicep`). App Insights writes to its own Log Analytics
  workspace `log-fs-protocols-<env>`; both keep logs **90 days**
  (`logRetentionDays`).
- **Frontend (elsewhere)** — vanilla TypeScript + Vite, `site/src/protocolgenerator/`
  in `figureskatingtools-site`.

## Data model — `metadata.json` (the structure document)

Each competition is a blob folder `"{name}-{id}"` containing:

- `metadata.json` — the **structure** (see `infra/functions/structure.py`): event
  details, `coverPage`/`lastPage`/`header`/`footer` (default|custom — header/footer
  are the optional competition-wide page chrome), a `files` registry
  (`fileId -> {filename, kind, size, blob}`), and ordered `categories` with slots.
- `uploads/{fileId}_{filename}` — every uploaded PDF/photo/XML.
- `schedule.pdf` — the source schedule (kept for re-parsing).
- `rosters/teams.xml`, `rosters/partic.xml` — the last imported synchro roster pair.
- `protocols/protocol_*.pdf` — generated outputs.

No XML is stored with a date of birth (see **Dates of birth** below).

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

**Team-page settings.** Two settings — whether synchro team pages are produced at
all, and how much of each roster is printed (`NAME_MODES` = `full` | `firstNames` |
`none`; `none` keeps the page but drops the skater list) — resolve through three
levels: **competition → category → team**. Competition-wide `structure.teamPages`
(`{enabled, nameMode}`) is the base; `category.pageEnabled` / `category.nameMode`
and `team.pageEnabled` / `team.nameMode` override it, `None` meaning *inherit* at
both lower levels. Nothing is copied down, so changing a category immediately moves
every team that has not overridden it. Every read goes through the resolvers in
`structure.py` (`team_pages_defaults`, `category_page_enabled`,
`category_name_mode`, `team_page_enabled(structure, category, team)`,
`team_name_mode(structure, category, team)`), which default a metadata.json written
before the feature to today's behaviour — pages on, full names — so there is no
migration.

A team also carries `team.textFields`, up to `MAX_TEAM_TEXT_FIELDS` (6 — exactly the
number `generate_pages._text_block` can draw inside `TEXT_MAX_H`, so a stored row is
never one the page silently drops) free-typed rows
printed on its page ("Theme: Spies"), each `{id, label, value}` — a flat list, in
stored order, with no segment dimension. `team_text_rows(team)` is what drops the
rows blank on both sides and hands the renderer plain `{label, value}` data;
`sanitize_text_fields(rows)` normalises what the client sends and strips the
`segmentId` rows written while the feature still attached them to segments (which
`team_text_rows` likewise ignores rather than choking on).

## Assembly order (`assemble.py`)

cover (custom or default) → event-info page → time-schedule page → for each
category in schedule order: *(synchro)* one team page per team → protocol head
page PDF (the category's `titlePdf` slot; "Protocol Head Page" in the UI) →
podium page (when a photo or name exists) → total results PDF → per segment
(results → panel → judges details) → last page (custom or default).

A synchro team whose resolved `team_page_enabled` is false — switched off
competition-wide, by its category or by itself — contributes **no page at all**:
no photo, no names, no text rows. It still competed, so `_competition_stats`
(and with it the information page's Competition Units / Performances) is deliberately
unaffected.

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

`list_competitions`, `delete_competition` (POST only, owner only; record id in
the `id` query param or a JSON body `{"id"}`), `resolve_competition` — the only
route that creates a competition, always bound to a platform one (platform
competition GUID → tool record: `PlatformId` lookup → create; an unbound record
is **never adopted by name** any more — that let anyone claim another user's
record; soft-deleted rows are never resurrected;
optional `dates`/`venue` seed `event.dates` (dd.MM.yyyy, marked
`event.datesAuto` so a schedule parse may refine it until the user saves the
event form) and `event.rink` — create seeds, a writer's hit backfills empty
fields; on a writer's
`PlatformId` hit whose sanitized platform name differs from the stored `Name`,
`_sync_competition_name` MERGE-writes `Name` first (authoritative — on failure the
old name is reported) and `_rename_structure` mirrors it into `metadata.json`
best-effort: `name` always, `event.title` (the printed "Protocol title") only while
it still equals the old record name, so a customised title survives; the structure
is read once and written once together with the dates/venue backfill; `FolderPath`
never changes (blob paths and protocol filenames hang off it); create never
renames, and a read-only caller's resolve changes nothing),
`get_competition_details`,
`save_event_settings`, `upload_file`, `import_platform_file` (copies a file out
of the platform's shared competition file pool — see below), `get_file`
(streams bytes for previews),
`assign_file`, `delete_file`, `parse_schedule` (body upload *or* a pool
reference, see below), `import_rosters`,
`upload_fallback_photos` (bulk fallback-picture ZIP), `edit_structure`
(manual add/remove/set ops — including `set_team_pages {enabled?, nameMode?}` for the
competition-wide team-page defaults, `set_category`'s `pageEnabled` / `nameMode` for
a category's, and `set_team`'s `pageEnabled` / `nameMode` / `textFields` for a team's
overrides and its free-text rows; the two overrides are tri-state everywhere below
the competition level — an explicit `null` clears back to *inherit* — and
`textFields` is replaced wholesale like `members`), `generate_protocol`, plus the
daily auto-deletion
timer (see **Retention** below).

### Competition access (platform grants)

The platform registry owns who may see or change a competition; this tool only
enforces it. `platform_access.py` — **byte-identical** in fs-judgepapers and
fs-scoremodifier, change it in all three; keep the names `role_for`,
`role_satisfies`, `PlatformAccessUnavailable`, `clear_cache` stable — maps the
router's `x-forwarded-user-tid`/`-oid` through the platform's `useridentities`
table to a user id and reads that user's `Role` (`owner` > `write` > `read`) from
`competitionaccess` (both on `PLATFORM_STORAGE_ACCOUNT`, Table Data Reader
grants from the site repo; names overridable with `PLATFORM_IDENTITIES_TABLE` /
`PLATFORM_ACCESS_TABLE`; 30 s in-process cache). An admin
(`x-forwarded-user-admin: 1`, trusted only behind the fail-closed secret) is
`owner` everywhere without a lookup. E-mail is never an identity: it is kept only
as the `CreatedBy` audit field.

Every route that names a competition goes through one helper,
`_require_competition(caller, comp_id, need)` (record → `PlatformId` →
`_check_access` → `role_for`); `resolve_competition` calls `_check_access` on the
`platformId` directly and `list_competitions` filters with `_role_for`. **read**:
`list_competitions`, `get_competition_details` (incl. the protocol download
links), `get_file`, `resolve_competition` (creating an empty bound record is
allowed; renaming/backfilling an existing one needs write). **owner** (an admin
counts as owner): `delete_competition`. **write**: everything else. Refusals are
JSON: **404 `platform_not_found`** (no grant, unknown or
soft-deleted record, or a legacy record without `PlatformId` for a non-admin —
legacy records are admin-only), **403 `platform_read_only`** (read grant on a
write route), **403 `platform_owner_required`** (read/write grant on an owner
route), **503
`platform_unavailable`** (tables unreadable or `PLATFORM_STORAGE_ACCOUNT` unset —
fail closed). A refused call writes nothing. `tests/test_competition_access.py`
holds the route × role matrix and fails when a new HTTP route is not classified,
declares no methods, or lets a state-changing route answer `GET` (only `health`,
`check_user_permission`, `list_competitions`, `get_competition_details`,
`get_file` take GET); `tests/test_proxy_gate.py` the gate (fail-closed secret,
header-only identity).

**Download links are short-lived and never stored.** `get_competition_details`
mints each generated protocol's link after the access check
(`_generated_protocols` → `sh.mint_read_urls`): one user delegation key per
request, a read-only SAS per blob, valid `sh.DOWNLOAD_LINK_TTL` (15 min; start
backdated `SAS_CLOCK_SKEW` 5 min), `expiration` in the response. The blob name is
derived (`sh.protocol_blob_name(folder, basename(FileName))`), never taken from a
row; `generate_protocol` records only `FileName`/`FileSize`/`Description`
(`sh.record_generated_protocol`, REPLACE), and legacy rows' 5-day
`Url`/`ExpirationDate` are ignored and scrubbed on read (`sh.scrub_stored_link`).
Never add a stored or long-lived link back: a revoked user must lose access within
minutes. `get_file` streams bytes, no URL. `tests/test_download_links.py`.

### Retention: one platform clock

There is **one retention rule per competition, owned by the platform**; this tool
has no clock of its own and no extend route. The site's platform registry holds
every competition's deletion date (created + 60 d at midnight UTC, extended a week
at a time only through the platform's `POST /api/competitions/{id}/extend`) and
mirrors the non-personal part into its own table **`retentionclock`** on the
`stfsplat*` account (PK `COMPETITION` / RK the lowercase platform GUID;
`DeletionDate`, `Status`, `PurgedUtc`; override the name with
`PLATFORM_CLOCK_TABLE`). `platform_clock.py` reads only that table, through a
**Storage Table Data Reader** grant scoped to it (site repo's
`shared-data-access.bicep`) — never the registry's `competitions` table. The file
is **byte-identical** in fs-judgepapers and fs-scoremodifier — change it in all
three. `read_platform_clock` returns None when the id is not a GUID, the row does
not exist or `PLATFORM_STORAGE_ACCOUNT` is unset, and raises
`PlatformClockUnavailable` on any other failure, a missing table (`TableNotFound`)
included. One table client per request or sweep (`_platform_table()`, passed as
`table_client=`).

- Every record is bound (`PlatformId`, always stored as
  `pc.normalize_platform_id` — trimmed, lowercase GUID — so differently-cased ids
  never make a second record; a non-GUID is 400 `invalid_platform_id`);
  `resolve_competition` is the only creation path. A record's `DeletionDate` is
  only a **cached copy** of the platform's date (`sh.sync_local_deletion_date`,
  refreshed by resolve, `get_competition_details` and the timer) — absent until
  the platform reports one — for display only. `resolve_competition` and
  `get_competition_details` return it as `deletionDate` (null when there is none).
  `resolve_competition` answers **410 `platform_deleted`** — creating nothing —
  when the platform competition is deleted or purged. A **new** binding (a
  create) must be confirmed by the platform: 404 `platform_not_found` when it
  has no such competition, 503 `platform_unavailable` when it cannot be read (a
  record bound to a nonexistent competition would never be deleted); with
  `PLATFORM_STORAGE_ACCOUNT` unset (local dev) it binds without a date. An existing
  bound record still resolves on its cached date when the platform is unreachable
  or does not know it.
- The **04:00 UTC** timer (`_run_auto_delete`, after the platform's 03:00 sweep)
  deletes a bound record's data **only on the platform's verdict**
  (`platform_says_delete`: `Status == "deleted"` or `PurgedUtc`) — never on a date
  alone (the platform may be in dry-run, or the competition just extended). A
  platform that **cannot be read, or `PLATFORM_STORAGE_ACCOUNT` unset, means skip
  + `logging.error`** — never delete. Legacy rows with no `PlatformId` go on their
  stored date (`ensure_deletion_date` backfills `LEGACY_DELETION_DATE` for those
  only).
- **Orphans** (no platform competition can decide about them): a live record
  with no `PlatformId` (or a non-GUID one, or no `FolderPath`), or whose clock
  row a *successful* read did not find, is deleted by `_delete_competition_data`
  (`auto-cleanup`, `CreatedBy` blanked, the row kept as history) once it is
  **more than 60 days old** — `CreatedDate`, else the Table `Timestamp`
  (`platform_clock.record_age_reference`); younger orphans are kept with a
  warning, and a read error is never "not found" (skipped). Rails: one probe of
  the clock table per run (`platform_clock.orphan_sweep_allowed`) — unreadable,
  unconfigured, or **zero rows while records are bound** (wrong table/account)
  aborts the orphan step for that run (`logging.error`); `ORPHAN_PURGE_ENABLED`
  (Bicep param `orphanPurgeEnabled`, default on; GitHub var
  `ORPHAN_PURGE_ENABLED=false` is the kill switch) off → it only logs "would
  delete". `_run_auto_delete` logs and returns the run's counts (`deleted`,
  `skipped`, `failed`, `orphaned`, `orphan_kept`, `orphan_dry_run`,
  `orphan_skipped`).
- No e-mail address outlives the data: `_delete_competition_data` merges
  `CreatedBy=""` and `DeletedBy="user"|"auto-cleanup"`, and every nightly run ends
  with `_scrub_hidden_rows`, which blanks `CreatedBy`/`DeletedBy` on soft-deleted rows
  deleted before that rule. Logs carry no addresses (nor fallback-photo or pool
  filenames) and are kept 90 days. Covered by `tests/test_platform_clock.py`.

### Shared competition file pool + auto-assignment

`upload_file` and `import_platform_file` share `_register_upload(structure,
folder_path, filename, body, params)` — file-id mint, blob write, `files{}`
entry and the optional slot assignment (`slotKind` + `categoryId`/`segmentId`/
`teamId`/`role`, with the podium / unit-count / title-discipline side effects).
A target the structure no longer has leaves the file in the tray instead of
failing the request.

`POST import_platform_file?competition=&name=[&source=upload|fsm]` copies a
file the platform holds in `competition-data/<PlatformId>/<uploads|fsm>/<name>`
(read-only, via `sh.get_platform_container_client()` / `PLATFORM_STORAGE_ACCOUNT`
+ `PLATFORM_DATA_CONTAINER`) into this competition, stamping `meta.poolName`.
`source` defaults to `upload` (what people uploaded); `fsm` is what the HOVTP
listener pushed. The pool folder comes from the competition's bound `PlatformId`
plus that fixed folder name, never from the client. JSON errors carry a code so
the frontend can fall back to a direct upload: 409 `not_bound`, 503
`platform_not_configured`, 404 `pool_file_not_found`, 502
`platform_unavailable`, 413 `file_too_large`, 400
`unsupported_type`/`invalid_source`/`missing_parameter`.

`POST parse_schedule?competition=&poolName=[&source=upload|fsm][&force=true]`
with an **empty body** parses the schedule straight out of that same pool
(`DT_SCHEDULE_FSK….xml` or `…_CompetitionSchedule.pdf`) instead of from an
uploaded body — the bytes come from the shared `_read_pool_file(entity,
filename, source)` helper, so the binding check, folder resolution, size cap and
error codes are literally `import_platform_file`'s (409 `not_bound`, 503, 502,
404, plus a plain-text 400 `invalid_source`). Everything after that is the
upload path unchanged (409 unless `force`, `schedule.xml|pdf` kept,
`parse_schedule_data`, event auto-fill); the success JSON adds
`"source": {"poolName": "<filename>", "source": "upload|fsm"}` (the query value
`upload` reads the `uploads/` folder, `fsm` reads `fsm/`). The frontend uses this automatically: a
bound competition with no categories and a schedule in the pool parses it on
open, and the Schedule section offers a "Use it" / "Replace from it" button.

`import_rosters` takes the synchro roster pair the same way: besides the inline
`teamsXml`/`particXml` strings, the JSON body may carry `teamsPool`/`particPool`
= `{name, source: upload|fsm}` (FSM pushes `DT_PARTIC_TEAMS_<code>.xml` and
`DT_PARTIC_<code>.xml` over HOVTP; both already without birth dates, stripped
again all the same). Each reference reads through
`_read_pool_file` with the same JSON errors; inline XML wins over a reference to
the same file, a reference without a name is 400 `missing_parameter`, and a
pooled file whose content is not the type it was referenced as
(`dt_partic.document_type`: root `DocumentType` — any other declared type, e.g.
`DT_PARTIC_UPDATE`, is neither — else `<Composition>` vs `<Participant>`) is 400
`wrong_document_type` — the pool is picked by filename.
The success JSON adds `"source": {teams?, partic?}` → `{poolName, source}`.
`rosterImport.files` (`{teams?, partic?}` → `{at, poolName?, source?}`) records
which files are in use and when each was supplied; a pass that does not supply a
file — the automatic re-match included — keeps its entry, and a fresh TEAMS file
drops the `partic` entry (it is never joined with the archived DT_PARTIC). A report
written before `files` existed seeds it from its own `at` on the next pass (the
archived TEAMS, plus the archived PARTIC when a re-match reads it). The
frontend auto-imports the newest pool pair on open when a synchro category has no
roster import yet, and offers "Update from them" once a pool file is newer than
its `files` entry.

`autoAssigned` marks a placement made by filename recognition rather than by a
human: `&autoAssigned=1` on either upload route tags the file **only** when the
slot assignment actually succeeded, and any later manual `assign_file` (including
a move back to the tray) drops the tag — unless that call declares itself
automatic with body `"autoAssigned": true`.

## Parsers (calibration pending)

- `schedule_parser.py` — `parse_schedule_data(bytes)` auto-detects the format:
  - **DT_SCHEDULE OdfBody XML (preferred)** — structured `<Unit>` rows with ISO
    times, explicit discipline/segment in `ItemName`, the ice rink in
    `VenueName` (auto-fills `event.rink`/`event.dates`), and an ISU `Unit Code`
    used to group units into categories (so Advanced Novice L1 `…ADVNOV----` and
    L2 `…ADVNOV--01` stay distinct while a category's Short Program + Free Skating
    merge). The category `code` (`unit_code[:22]`) is stored and is what the
    frontend's filename auto-assignment matches FSM's exported PDFs against —
    their names start with the very same RSC — so it must stay verbatim.
  - **Schedule PDF (fallback)** — calibrated against the Finnish "COMPETITION
    SCHEDULE" export: glued start/finish times, 2-space category|segment columns,
    multi-day segment merge, and synchro detected from the document title
    ("MUODOSTELMALUISTELUN…") via `structure.discipline_signal`.
- `dt_partic.py` — calibrated against real ISU/TAIKKARI OdfBody files. Joins
  **DT_PARTIC_TEAMS** (`<Team>`/`<Composition>`/`<Athlete Code>`) with
  **DT_PARTIC** (`<Participant Code GivenName FamilyName>`) on athlete `Code`.
  One TEAMS + one PARTIC file cover the **whole competition**. Names render
  "FAMILY Given", rosters sorted alphabetically; Name/Organisation are stripped
  (real exports carry trailing spaces). That "FAMILY Given" shape is load-bearing:
  `generate_pages._given_names` reverses it for the team page's given-names mode.
- `roster_matching.py` — pure team→category placement, used by `import_rosters`.
  Teams register per **event** (`RegisteredEvent="…MLAIKU----"`) but often compete
  per **block** ("Aikuiset, Mupi L1"/"L2"; DT_SCHEDULE codes `…MLAIKU--01`), and
  block membership exists *only* in each block's total-results PDF. So matching is
  **results-first**: `match_teams` places a team into the category whose result
  rows name it (exact normalized name, then word-subset fuzzy — "JääLeidit" ⊂
  "Helsinki JääLeidit" — with the club abbreviation as tiebreak), and only falls
  back to the registered event when that maps to exactly one category
  (`categories_for_event`: trailing-dash-stripped code prefix at a dash boundary
  — bare senior `FSKXSYNCHRON` must not swallow `FSKXSYNCHRONJUNIOR` → event-token
  fragment vs. category-name words, "MLAIKU"→"Aikuiset…" → ISU/Finnish label, a
  bare synchro code reading "Senior"; each pass drops hits whose *name* signals
  another discipline, so "SM-JUNIORI Naiset" never competes with "SM-JUNIORI
  Muodostelma" — names only, since placing a team flips a category's stored
  `discipline` to synchro and would otherwise skew a partial import).
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

## Dates of birth (`odf_birth_dates.py`)

FS Manager's participant files (`DT_PARTIC`, `DT_PARTIC_TEAMS`, their `_UPDATE`s)
carry each skater's `BirthDate`; nothing here uses it and the site's privacy
notice says it is never stored. `odf_birth_dates.py` holds a **byte-identical
copy** of the site repo's two sections "birth dates out of participant files"
and "participant files uploaded by users" (`infra/functions/function_app.py`
there; the HOVTP listener has the first one too) — change the site first, copy
the sections verbatim; `tests/test_birth_dates.py` pins them by SHA-256 (and
compares with a site checkout when `FIGURESKATINGTOOLS_SITE` names one). Only
`BirthDate` attributes/elements go, every other byte stays; a file with none
comes back as the same bytes. Applied before every write:
`_register_upload` (`upload_file`, `import_platform_file`) and `parse_schedule`
via `strip_upload` (by content, like the platform: an `OdfBody` declaring a
participant type or none; anything else untouched), and `_run_roster_match`
via `strip_birth_dates` on every roster XML it is handed (inline or pool), which
is then both parsed and archived under `rosters/` in that form. A file that
cannot be cleaned (not XML, UTF-16, a strip that would change anything else) is
**422 `birth_dates_not_removed`** (JSON `{error, message}`, the platform pool's
code) and nothing is written. Files stored before this rule keep theirs until
the competition is deleted or the roster re-imported.

## Output privacy (`image_sanitize.py`, `pdf_sanitize.py`)

The published protocol must hold nothing beyond its visible pages. Every photo
(podium, team, fallback, custom header/footer/cover image) goes through
`image_sanitize.clean_jpeg`, which rebuilds the picture from its decoded pixels
(EXIF orientation applied first) — Pillow would otherwise copy a JPEG's COM
comment into the re-save; without Pillow nothing is embedded. Then, right before
the single `writer.write`, `pdf_sanitize.sanitize` strips annotations (links,
comments, widgets), page actions, page/XObject XMP and private data, the APPn/COM
segments of every embedded JPEG (inserted PDFs included, lossless — APP0/APP14
kept; image XObjects reached via /XObject, soft masks, tiling patterns and Type 3
glyphs, plus inline BI/ID/EI images), page/XObject `/AF` associated files, any
outline/name tree/form/open action/structure tree, sets the Document
Info to exactly Title (event title) / Author (`event.organization`) / Creator /
Producer, and frees every object not reachable from the trailer
(`collect_garbage` — pypdf's `remove_unreferenced` misses self-referencing
clusters). A JPEG it cannot decode or parse raises `SanitizeError` and
`generate_protocol` answers 422 instead of publishing it. Generation-path logs print exception *types* only, since a message can
quote page text. `tests/test_pdf_privacy.py` checks all of it with pikepdf
(test-only dependency) on deliberately dirty, invented inputs.

## Output size

Photos are embedded at the size they are printed, not as uploaded:
`generate_pages._fit_image_box` and `branding._load_reader` pass the drawn box to
`clean_jpeg(…, box=(w_pt, h_pt))`, which downscales to `image_sanitize.PRINT_DPI`
(200) at that size — never upscaling — and encodes q85 with `optimize=True`
(large JPEGs scale down while decoding via `Image.draft`). Every generated page is
its own reportlab canvas, so the header/footer band arrives once per page;
`pdf_sanitize.dedupe` (after `sanitize`, before the write) merges the identical
copies — repeating pypdf's single-pass `compress_identical_objects` until stable,
since an image only matches its twins once their `/SMask`s have merged.
`branding.py` sets `rl_config.useA85 = 0` so reportlab writes binary streams
rather than ASCII85 (+25%). `tests/test_image_size.py` covers it.

**HEIC/HEIF** (iPhone default) decodes through `pillow-heif`'s opener, registered
in `image_sanitize`. `_register_upload` (both upload routes) converts a
`.heic`/`.heif` to a metadata-free full-resolution JPEG (q92) stored as
`<stem>.jpg`, so browser previews and filename matching keep working; one that
won't decode is a 400 (`unreadable_image` on `import_platform_file`). The
fallback ZIP accepts them too.

## Defaults / backups

The real cover, last page and header/footer art are now the approved brand kit (see
**Branding** above). `generate_pages.py` still holds the plain fallbacks
(`default_cover_page`, `default_last_page`) used only when the brand fonts/assets
are unavailable, plus neutral placeholder boxes for missing team photos. The podium
page lays the top three out in podium shape (1st centre/highest, 2nd left, 3rd
right) and leaves the photo area empty (no placeholder) when no podium photo is set.
The synchro team page sizes its photo box dynamically — the free-text block and the
roster rows (2 columns, 3 past 44 skaters, or past 32 when text rows share the page)
are reserved first so up to 32 names always fit above the footer band, and the photo
takes the remaining height (clamped 60–130 mm, or 60–185 mm when the names are
switched off and it can grow). `TEXT_MAX_H` (45 mm, the gap under the photo included)
is what buys that guarantee: it is sized so the photo box never has to fall back on
its 60 mm floor, which would be the one way the last roster line could slip into the
footer band. `_text_block` truncates to that budget — at `TEXT_ROW_H` it admits six
"Label  Value" rows (8 + 6 × 5.5 = 41 mm), which is why
`structure.MAX_TEAM_TEXT_FIELDS` (and the frontend's mirror of it) is 6: the cap on
what may be entered is exactly what prints. `generate_pages._given_names` reverses `dt_partic`'s "FAMILY Given"
convention for the `firstNames` mode, returning an entry that does not follow it —
a hand-edited name — whole rather than blank.

## Local development

Backend only: `cd infra/functions && func start`, then call it with the proxy
headers (`x-proxy-secret`, `x-forwarded-user-tid`/`-oid`, and `x-forwarded-user-admin: 1`
unless `PLATFORM_STORAGE_ACCOUNT` points at real access tables — see
**PROXY-CONTRACT.md**). `PROXY_SHARED_SECRET` must be set in
`local.settings.json`: the gate fails closed.
To drive it from a UI, run the router + Vite dev server in `figureskatingtools-site`
pointed at this `func start` instance.

Backend tests (synthetic fixtures only — never commit real competition data,
the OdfBody exports contain minors' personal data):

```bash
cd infra/functions
python3.13 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests -q
```

## Deploy

**Flow (no long-lived `test` branch):** branch from `main` → dispatch
`deploy.yml` on that branch with `environment=test` → PR to `main` → merge →
push to `main` deploys prod → the branch is auto-deleted on merge. `main` is
guarded by a ruleset: PR required, the `pytest` check
(`.github/workflows/ci.yml`, runs on every PR) must pass, code-owner review
required (`.github/CODEOWNERS`: `* @mmaraa`), no force-push/deletion; repository
admins can bypass, which is how the owner merges their own PRs. The `prod` GitHub
environment only accepts deployments from `main` (deployment branch policy), so
a dispatch with `environment=prod` from any other branch is rejected; `test`
accepts any branch, and holds whichever branch was deployed last.

Push to `main` → prod via `.github/workflows/deploy.yml`; `test` via manual
`workflow_dispatch`. Two jobs only: **deploy-infra** (`az deployment sub create`
on `infra/main.bicep`, params `resourceGroupName` + `proxySharedSecret`) and
**deploy-backend** (run the pytest suite, then zip +
`az functionapp deployment source config-zip`). Manual equivalents:
`deploy_infra.sh`, `deploy_backend.sh`. deploy-backend runs
`python -m pytest tests -q` *before* packaging, so a red suite blocks the zip
deploy.

Both environments are live: **test** and **prod** (`rg-fs-protocols-prod`,
Function App `func-fs-protocols-wcpslt4m33wkk`), prod auto-deployed on every
push to `main`. There is no `protocols.figureskatingtools.com` binding,
redirect or teardown to worry about — the UI is served by the site app at
`/protocolgenerator/`.

All jobs in `deploy.yml` pin `runs-on: ubuntu-26.04` rather than `ubuntu-latest`
([actions/runner-images#14748](https://github.com/actions/runner-images/issues/14748)
flips `-latest` from 24.04 to 26.04 between 2026-10-19 and 2026-11-19); bump that
label deliberately — verify via a `test`-environment dispatch first, roll back to
`ubuntu-24.04` if needed — and never set it back to `ubuntu-latest`.

Dependabot version updates (weekly, grouped per ecosystem, `.github/dependabot.yml`,
read from the default branch `main` only) open their PRs against `main` like any
feature branch: CI tests them, they wait for code-owner review, and merging one
deploys prod — dispatch its branch to `test` first when the update is risky.

Required GitHub environment config: secrets `AZURE_CLIENT_ID`,
`PROXY_SHARED_SECRET`; vars `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`,
`LOCATION`, `RESOURCE_GROUP_NAME`, optional `ORPHAN_PURGE_ENABLED` (`false` =
the nightly orphan step only logs; unset = on), plus optional `PLATFORM_STORAGE_ACCOUNT` (the
site repo's `stfsplat*` account for this environment — unset leaves
`import_platform_file` off, **stops auto-deletion of every bound competition**
(logged as an error each night) and makes every non-admin competition request a
503 `platform_unavailable` — so it is required in practice). `PROXY_SHARED_SECRET`
is required too: empty, every request but `/api/health` is a 401. (`AUTH_CLIENT_ID`, `AUTH_APP_OBJECT_ID` and
`CUSTOM_DOMAIN` are gone.)

## Cross-repo handoff (figureskatingtools-site)

The deploy job's summary prints the Function App URL and its system-assigned
principal id; set them in the site repo's matching GitHub environment as
`FUNCTION_APP_URL_PROTOCOLGENERATOR` and `TOOL_PRINCIPAL_ID_PROTOCOLGENERATOR`,
and copy this environment's `PROXY_SHARED_SECRET` there as
`PROXY_SHARED_SECRET_PROTOCOLGENERATOR`.
