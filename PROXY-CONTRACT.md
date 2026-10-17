# Proxy contract — how requests reach this Function App

This repo is **backend-only**. Nothing browses the Function App directly: the
Protocol Generator UI is served by the `figureskatingtools-site` router
(`figureskatingtools.com/protocolgenerator/`), which handles the Entra (Easy
Auth) login and forwards every `/protocolgenerator/api/*` request here as
`/api/*`, server-to-server.

```
Browser ── https://figureskatingtools.com/protocolgenerator/api/list_competitions
             │  (Easy Auth session cookie)
             ▼
        site router (server.js)
             │  strips the /protocolgenerator prefix
             │  drops any x-forwarded-user-* / x-proxy-secret the browser sent
             │  adds  x-proxy-secret: <PROXY_SHARED_SECRET>
             │  adds  x-forwarded-user-tid / -oid / -email / -name [/ -admin]
             ▼
        https://func-fs-protocols-<hash>.azurewebsites.net/api/list_competitions
```

## Headers

Set **only** by the router, which drops any client copy of them.

| Header | Meaning |
| --- | --- |
| `x-proxy-secret` | Must equal the Function App's `PROXY_SHARED_SECRET` app setting (compared with `hmac.compare_digest`). Wrong or missing → 401. |
| `x-forwarded-user-tid` | The signed-in user's Entra tenant id (GUID). **Identity.** |
| `x-forwarded-user-oid` | The signed-in user's Entra object id (GUID). **Identity.** |
| `x-forwarded-user-email` | E-mail, lowercased. Display and audit only (`CreatedBy`), never used to authorise anything. Optional. |
| `x-forwarded-user-name` | Display name, `encodeURIComponent`-encoded (decoded with `urllib.parse.unquote`). Optional. |
| `x-forwarded-user-admin` | `1` when the router counts the user as a platform admin; absent otherwise. Any other value means "not admin". |

Header names are case-insensitive.

Implemented in `infra/functions/storage_helpers.py` (`_proxy_secret_ok`,
`get_caller` → `Caller(tid, oid, email, name, is_admin)`). Every route calls
`function_app._require_user`, which returns `None` → **401** when:

- `PROXY_SHARED_SECRET` is **empty or unset**. The gate **fails closed**; there
  is no "disabled" mode, locally included;
- `x-proxy-secret` is missing or does not match;
- `x-forwarded-user-tid` or `x-forwarded-user-oid` is missing or not a GUID.

There are **no other identity sources**. `X-MS-CLIENT-PRINCIPAL-NAME`,
`X-MS-CLIENT-PRINCIPAL` (base64 principal) and `Authorization: Bearer` tokens
are ignored, and so is an e-mail without the tid/oid pair. The forwarded headers
are trusted only because the secret matched, which is why the secret check must
never fail open.

The one exception is `GET health`. It needs neither identity nor the secret and
answers `{"status": "ok", "service": "fs-protocolgenerator"}` without touching
storage, for the site's admin diagnostics.

### Shared secret

`PROXY_SHARED_SECRET` is set on the Function App by `infra/modules/function.bicep`
from the `PROXY_SHARED_SECRET` GitHub environment secret. The same value must be
configured in the site repo as `PROXY_SHARED_SECRET_PROTOCOLGENERATOR`.

## Competition access

The platform registry (site repo) decides who may see or change a competition.
`infra/functions/platform_access.py` reads two platform tables on
`PLATFORM_STORAGE_ACCOUNT` through Storage Table Data Reader grants (site repo's
`shared-data-access.bicep`). The file is **byte-identical** in fs-judgepapers and
fs-scoremodifier; change it in all three.

- `useridentities` (override: `PLATFORM_IDENTITIES_TABLE`): `IDENTITY` / `<tid>_<oid>` → `UserId`
- `competitionaccess` (override: `PLATFORM_ACCESS_TABLE`): `<platform guid>` / `<UserId>` → `Role`
  (`owner` > `write` > `read`)

`role_for(platform_id, tid, oid, is_admin)` returns the caller's role. Answers
are cached in-process for 30 s, so a revoked share stops working within that
time. An admin (`x-forwarded-user-admin: 1`) is `owner` everywhere, without a
lookup.

Every route that names a competition, by this tool's record id or by a
`platformId`, loads the record, takes its `PlatformId` and needs one of these
(`function_app._require_competition` / `_check_access`):

| Need | Routes |
| --- | --- |
| **read** | `list_competitions` (filtered to the records the caller can read; admin: all), `get_competition_details` (incl. the generated protocols' download links), `get_file`, `resolve_competition` |
| **write** | `delete_competition`, `save_event_settings`, `upload_file`, `import_platform_file`, `assign_file`, `delete_file`, `parse_schedule`, `import_rosters`, `upload_fallback_photos`, `edit_structure`, `generate_protocol`, `delete_protocol` |

`check_user_permission` and `health` name no competition.

- `resolve_competition` with read access may create a **new, empty** bound
  record. Only a writer's call renames an existing record or backfills its event
  fields from the request body. It never adopts an unbound record by name; it
  only returns records already bound to that `platformId`.
- A legacy record without `PlatformId` is **admin-only**.
- A soft-deleted record answers like an unknown one.

### Error codes

JSON bodies `{"error": "<code>", "message": "…"}`:

| Status | `error` | When |
| --- | --- | --- |
| 401 | (plain text `Unauthorized`) | The gate failed (secret or identity, see above). |
| 404 | `platform_not_found` | No grant, unknown identity, unknown or deleted record, or a legacy unbound record and the caller is not an admin. Existence is never revealed. |
| 403 | `platform_read_only` | A read grant on a write route. |
| 503 | `platform_unavailable` | The access tables could not be read (network, RBAC, missing table, `PLATFORM_STORAGE_ACCOUNT` unset). Fails closed; access is never granted. |

`resolve_competition` keeps its retention-clock answers too: 410
`platform_deleted`, and 404 `platform_not_found` / 503 `platform_unavailable`
when the clock cannot confirm a new binding.

## Testing it with curl

Against a local `func start` (port 7071). The gate is always on, so set a secret:

```bash
# infra/functions/local.settings.json → "PROXY_SHARED_SECRET": "devsecret"
# Without PLATFORM_STORAGE_ACCOUNT only an admin passes the access check.
curl -s http://localhost:7071/api/list_competitions \
  -H 'x-proxy-secret: devsecret' \
  -H 'x-forwarded-user-tid: 11111111-2222-4333-8444-555555555555' \
  -H 'x-forwarded-user-oid: aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee' \
  -H 'x-forwarded-user-email: tester@example.com' \
  -H 'x-forwarded-user-admin: 1'
```

Against the deployed Function App (needs the environment's real secret):

```bash
FUNC=https://func-fs-protocols-<hash>.azurewebsites.net
SECRET=$(az functionapp config appsettings list \
  -g rg-fs-protocols-test -n func-fs-protocols-<hash> \
  --query "[?name=='PROXY_SHARED_SECRET'].value" -o tsv)

curl -s "$FUNC/api/list_competitions" \
  -H "x-proxy-secret: $SECRET" \
  -H 'x-forwarded-user-tid: <your tenant id>' \
  -H 'x-forwarded-user-oid: <your object id>'
```

Omitting the secret, the tid or the oid must return **401**, and a competition
you hold no grant on must return **404 `platform_not_found`**. Those are the
regression checks after any change to the auth helpers or to the router.
