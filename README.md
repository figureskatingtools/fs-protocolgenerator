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

This repo holds the **backend only**. The user interface lives in
[`figureskatingtools-site`](https://github.com/figureskatingtools) and is served at
`https://figureskatingtools.com/protocolgenerator/`; that site's router does the Entra
login and proxies `/protocolgenerator/api/*` to this Function App, adding
`x-proxy-secret` and `x-forwarded-user-email` (see [PROXY-CONTRACT.md](PROXY-CONTRACT.md)).

- **Backend** — Python Azure Functions (`infra/functions/`) using `pypdf` (merge),
  `reportlab` (generated pages) and `pillow` (photo embedding).
- **Storage** — Azure Blob (uploaded files + `metadata.json` structure + generated output)
  and Table (`competitions`, `generatedprotocols`).
- **Hosting** — Flex-Consumption Function App + its storage account. IaC in `infra/`
  (Bicep). No Web App, no custom domain, no app registration in this repo.
- `frontend/` is the retired standalone SPA, kept for reference only.

## Local development

```bash
cd infra/functions && func start
curl -s http://localhost:7071/api/list_competitions \
  -H 'x-proxy-secret: devsecret' -H 'x-forwarded-user-email: tester@example.com'
```

Requires Azure Functions Core Tools and an Azurite storage emulator. For a UI, run the
router + Vite dev server from `figureskatingtools-site` against this backend.

Tests: `cd infra/functions && uv run --with-requirements requirements-dev.txt python -m pytest tests -q`

## Deployment

Pushing to `main` deploys prod via `.github/workflows/deploy.yml`; `test` is deployed by
manual `workflow_dispatch`. The workflow has two jobs — infrastructure (Bicep) and backend
(zip deploy). Manual equivalents: `deploy_infra.sh`, `deploy_backend.sh`. In practice only
the **test** environment has ever been provisioned for this tool. See `CLAUDE.md` for the
full architecture.
