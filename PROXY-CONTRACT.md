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
             │  adds  x-proxy-secret: <PROXY_SHARED_SECRET>
             │  adds  x-forwarded-user-email: <signed-in user's email>
             ▼
        https://func-fs-protocols-<hash>.azurewebsites.net/api/list_competitions
```

## Headers

| Header | Set by | Meaning |
| --- | --- | --- |
| `x-proxy-secret` | router | Must equal the Function App's `PROXY_SHARED_SECRET` app setting. Wrong/missing → the request is treated as unauthenticated (401). Case-insensitive; `X-Proxy-Secret` is equivalent. |
| `x-forwarded-user-email` | router | The signed-in user's email. Becomes the acting user for ownership/audit fields. |

Both are implemented in `infra/functions/storage_helpers.py`
(`_proxy_secret_ok`, `get_user_email_from_header`); every route calls
`function_app._require_user`, which returns `None` → HTTP 401 when no identity
can be established.

### Shared secret

`PROXY_SHARED_SECRET` is set on the Function App by `infra/modules/function.bicep`
from the `PROXY_SHARED_SECRET` GitHub environment secret. The same value must be
configured in the site repo as `PROXY_SHARED_SECRET_PROTOCOLGENERATOR`.

**If the setting is empty the gate is disabled** (`_proxy_secret_ok` returns
`True`) — that is the local-dev mode; deployed environments always set it.

### Identity precedence in `get_user_email_from_header`

The secret is checked **first** — if it fails, no identity is derived at all,
regardless of the other headers. Then, in order:

1. `x-ms-client-principal-name` — Easy Auth's own header (only present if
   something in front ever enables Easy Auth on this app; today nothing does).
2. `x-forwarded-user-email` — **the normal production path**, set by the router.
3. `x-ms-client-principal` — base64 JSON principal; `userDetails` is used.
4. `Authorization: Bearer <jwt>` — payload decoded (unverified) and the first of
   `preferred_username`, `email`, `upn`, `unique_name`, `emails[0]`, `name`,
   `oid` is used.

Anything else → `None` → 401.

## Testing it with curl

Against a local `func start` (port 7071) with the gate enabled:

```bash
# infra/functions/local.settings.json → "PROXY_SHARED_SECRET": "devsecret"
curl -s http://localhost:7071/api/list_competitions \
  -H 'x-proxy-secret: devsecret' \
  -H 'x-forwarded-user-email: tester@example.com'
```

Against the deployed Function App (needs the environment's real secret):

```bash
FUNC=https://func-fs-protocols-<hash>.azurewebsites.net
SECRET=$(az functionapp config appsettings list \
  -g rg-fs-protocols-test -n func-fs-protocols-<hash> \
  --query "[?name=='PROXY_SHARED_SECRET'].value" -o tsv)

curl -s "$FUNC/api/list_competitions" \
  -H "x-proxy-secret: $SECRET" \
  -H 'x-forwarded-user-email: tester@example.com'
```

Omitting either header must return **401** — that is the regression check after
any change to the auth helpers or to the router.
