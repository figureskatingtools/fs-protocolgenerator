# fs-protocolgenerator — Protocol Generator

Part of the [figureskatingtools.com](https://figureskatingtools.com) family of tools
(alongside `fs-judgepapers` and `fs-scoremodifier`). It assembles an official-style
**competition protocol** — a single bound PDF — from the files an organizer produces.

The tool is mostly **PDF creation and connecting files together**: you upload the result
PDFs and photos from a competition, drop them into the right slots, and press **Generate**
to get one merged protocol PDF. Where graphics are missing (cover, podium/team photos, last
page), the tool supplies sensible **defaults/backups** so a protocol can always be produced.

## Workflow

1. **Create a competition** (name + dates).
2. **Upload the schedule PDF.** The backend parses it into the ordered list of
   categories and their segments and builds the matching slots.
3. **Fill in event details** (organizer, authorization org, city, ice rink) and drop files
   into slots: category title PDF, podium photo + names, total results PDF, and per segment
   the results / panel-of-judges / judges-details PDFs. Synchronized-skating categories also
   get team cards (org + name, team photo); rosters are imported by selecting both the
   `DT_PARTIC_TEAMS` and `DT_PARTIC` XML files together (joined on athlete code).
4. **Drag and drop** any uploaded file from one slot to another to fix misplacements; hover a
   file for a preview (photo thumbnail or PDF first-page preview).
5. **Generate** → one protocol PDF, assembled in canonical order, available via a download link.

## Assembled protocol order

Cover (custom or default) → event info page → time schedule → for each category in schedule
order: *(synchro)* team pages → title PDF → podium → total results → per segment (results →
panel → judges details) → optional last page (custom or default placeholder).

## Architecture

- **Frontend** — vanilla TypeScript + Vite SPA (`frontend/`), shared nav from
  `@figureskatingtools/shared-ui`, the shared "Protocol" design tokens.
- **Backend** — Python Azure Functions (`infra/functions/`) using `pypdf` (merge),
  `reportlab` (generated pages) and `pillow` (photo embedding).
- **Storage** — Azure Blob (uploaded files + `metadata.json` structure + generated output)
  and Table (`competitions`, `generatedprotocols`).
- **Hosting** — App Service Web App (Node proxy + Easy Auth) in front of the Function App,
  custom domain `protocols.figureskatingtools.com`. IaC in `infra/` (Bicep).

## Local development

```bash
./start_locally.sh      # Azurite + func backend + vite frontend + SWA auth emulator
```

Requires Azure Functions Core Tools, Node 22, and an Azurite storage emulator. Frontend
install needs a GitHub Packages token for `@figureskatingtools/shared-ui`:

```bash
cd frontend && NODE_AUTH_TOKEN=$(gh auth token) npm install
```

## Deployment

Pushing to `main` deploys prod via `.github/workflows/deploy.yml`. Manual scripts also exist:
`deploy_infra.sh`, `deploy_backend.sh`, `deploy_frontend.sh`. See `CLAUDE.md` for the full
architecture and auth chain.
