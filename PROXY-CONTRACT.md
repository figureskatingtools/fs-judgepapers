# Proxy contract

This repo is **backend-only**. The Function App deployed from `infra/` has no
login surface of its own: it is called exclusively, server-to-server, by the
router in the **figureskatingtools-site** repo, which owns the domain, the
Entra Easy Auth session and the static frontend at
`https://figureskatingtools.com/judgepapers/`.

This document is the contract between that router and this backend. Both sides
must change together.

## Request path

```
Browser
  └─ https://figureskatingtools.com/judgepapers/api/<route>      (session cookie, Easy Auth)
       │  router strips the /judgepapers prefix and re-issues the request
       ▼
     https://func-fs-judgepapers-<hash>.azurewebsites.net/api/<route>
       + x-proxy-secret: <PROXY_SHARED_SECRET>
       + x-forwarded-user-tid: <Entra tenant id>
       + x-forwarded-user-oid: <Entra object id>
       + x-forwarded-user-email: <signed-in user's email, lowercased>
       + x-forwarded-user-name: <display name, encodeURIComponent-encoded>
       + x-forwarded-user-admin: 1                 (site admins only)
```

The Function App keeps the default `/api` route prefix — the router only removes
its own `/judgepapers` segment, so no backend route ever changes.

The Function App is **`AllowAnonymous`** at the platform level
(`infra/modules/function.bicep`, `authsettingsV2.globalValidation`). It must be:
the router forwards identity headers, not a bearer token, so Easy Auth
enforcement would 401 every proxied request before the app's own header check
runs. CORS is empty (`allowedOrigins: []`) because no browser ever calls this
host directly.

## Headers

| Header | Direction | Meaning |
|---|---|---|
| `x-proxy-secret` | router → function | Proof the request came from the router. Compared against the `PROXY_SHARED_SECRET` app setting. |
| `x-forwarded-user-tid` | router → function | The signed-in principal's Entra **tenant id** (lowercase GUID). Identity. |
| `x-forwarded-user-oid` | router → function | The signed-in principal's Entra **object id** (lowercase GUID). Identity. |
| `x-forwarded-user-email` | router → function | E-mail, lowercased and trimmed. **Display/audit only** (`CreatedBy`, `SentBy`, `importedBy`, the send rate limit) — never authorises anything. |
| `x-forwarded-user-name` | router → function | Display name, `encodeURIComponent`-encoded (decoded with `urllib.parse.unquote`). Display only. |
| `x-forwarded-user-admin` | router → function | `1` only when the router's `isAdminRequest` is true; absent otherwise. Any other value is "not admin". |

The router sets all of these itself and drops any copy a browser sends. Header
names are matched case-insensitively.

### `PROXY_SHARED_SECRET`

- Set on the Function App via the `proxySharedSecret` Bicep parameter (injected
  from the per-environment GitHub Environment secret `PROXY_SHARED_SECRET`).
- The router holds the same value per environment as
  `PROXY_SHARED_SECRET_JUDGEPAPERS`.
- `_proxy_secret_ok()` **fails closed**: an empty or missing app setting rejects
  every request, and the header is compared with `hmac.compare_digest`. A
  missing or mismatched `x-proxy-secret` makes `get_caller()` return `None`,
  and every endpoint turns that into **401** — except `GET health` (see below),
  which checks no secret.
- Rotate by updating both sides (this repo's env secret + the site repo's
  `PROXY_SHARED_SECRET_JUDGEPAPERS`) and redeploying; old and new hosts can run
  in parallel during a migration only while they share the same value.

### Identity: `get_caller()`

`infra/functions/function_app.py` builds the caller **only** from the
`x-forwarded-user-*` headers above, and only after the secret check:

1. **`_proxy_secret_ok()` gate** — if it fails, `None` (401). Nothing below is
   consulted.
2. `x-forwarded-user-tid` and `x-forwarded-user-oid` must both be GUIDs
   (trimmed, lowercased) — otherwise `None` (401).
3. `email` (lowercased), `name` (URL-decoded) and `is_admin`
   (`x-forwarded-user-admin == "1"`) complete the `Caller`.

There are **no fallbacks**: `X-MS-CLIENT-PRINCIPAL-NAME`, the base64
`X-MS-CLIENT-PRINCIPAL` principal and `Authorization: Bearer` JWTs are ignored
(a request carrying only those is a 401). Every endpoint except the two below
must call `get_caller(req)` and return **401** when it returns `None`.
`is_user_allowed(caller)` is the (currently allow-everyone) tool-wide hook.

**`GET health`** needs neither identity nor the shared secret: it answers
`{"status": "ok", "service": "fs-judgepapers"}` without touching storage, for
the site's admin diagnostics (the router pings it with the secret anyway).

**`GET download_judge_papers`** needs no identity. It serves the link e-mailed by
`send_judge_papers` (`https://figureskatingtools.com/judgepapers/download?t=<token>`),
and the recipient isn't signed in. The router proxies that exact public path
(excluded from Easy Auth) to `/api/download_judge_papers?t=<token>` without a
user header; the endpoint still requires the shared secret (`_proxy_secret_ok()`,
fail closed, 401), then resolves the 32-character token against the
`sentemails` table and answers `302` to a 15-minute read SAS (404 for a
malformed token; 410 when the link expired or its data is gone — no
`sentemails` row any more, the competition deleted, or the file removed; a
storage failure is a retryable 5xx, never a 410). The token is the
authorisation: no competition access check applies.

## Competition access

A competition is private to its owner and the users it is shared with on the
site (read or write). The platform (figureskatingtools-site) owns the grants;
this backend reads them through `platform_access.py` (kept **byte-identical**
with fs-protocolgenerator and fs-scoremodifier) from two platform tables that
hold no personal data, read-only through Storage Table Data Reader grants
scoped to each table (site repo's `shared-data-access.bicep`):

| Table (setting, default) | PK / RK | Used for |
|---|---|---|
| `PLATFORM_IDENTITIES_TABLE`, `useridentities` | `IDENTITY` / `<tid>_<oid>` | → the platform `UserId` |
| `PLATFORM_ACCESS_TABLE`, `competitionaccess` | `<platform guid>` / `<UserId>` | → `Role`: `owner` > `write` > `read` |

Both live in the account named by `PLATFORM_STORAGE_ACCOUNT`. An admin
(`x-forwarded-user-admin: 1`) counts as owner without any lookup. Answers are
cached in-process for 30 s, so a revoked share stops working within that time.

Every route that takes a competition (`id`, `workingFolder`, `competition` or
`platformId`) loads the record, takes its `PlatformId` and checks the caller's
role (`_competition_for` / `_access_error`) **before** touching anything:

| Need | Routes |
|---|---|
| none (signed in) | `check_user_permission`, `get_categories` |
| read | `get_competition_details`, `resolve_competition` (incl. creating the empty bound record), `list_competitions` (filtered) |
| write | `save_competition_settings`, `generate_judging_papers`, `preview_judge_papers_email`, `send_judge_papers`, `upload_file`, `import_platform_file`, `delete_file` |
| owner | `delete_competition` — **POST only**, id from `?id=` or a JSON body `{"id"}` (405 for any other method) |

- `list_competitions` returns only the records whose `PlatformId` the caller
  can read; an admin gets every live record (and only an admin's listing
  re-keys legacy name-keyed rows).
- `resolve_competition` checks read access to the `platformId` first and
  **never adopts an unbound record by name** any more (that let anyone capture
  another user's record): it finds the record bound to that GUID or creates a
  new one. The client-sent `name` renames an existing record only for a
  caller with write or owner; a read caller gets the stored name back and
  changes nothing.
- `get_competition_details` lists the generated files with a fresh read-only
  SAS link each (15 minutes, minted after the access check). Only the blob
  name is stored (`generatedpapers.BlobName`); the 5-day SAS URLs older rows
  hold are never returned (only their blob name is used).
- A record with no `PlatformId` (pre-platform legacy) is **admin-only**.
- An unknown or deleted (hidden) record is the same 404 as no access.
- `CreatedBy` keeps the creator's e-mail as history only; ownership is the
  platform's `owner` grant.

Errors (plain-text bodies starting with the code, like this tool's other
`platform_*` errors):

| Situation | Response |
|---|---|
| No access, unknown or deleted record, unbound record and caller not admin | **404** `platform_not_found: …` |
| Read-only caller on a write route | **403** `platform_read_only: …` |
| Read or write caller on an owner route | **403** `platform_owner_required: …` |
| The access tables can't be read (unset `PLATFORM_STORAGE_ACCOUNT`, missing table, RBAC, network) | **503** `platform_unavailable: …` — fail closed, never a grant |

## Rollout: revoke the old 5-day links

Before this change every download link was a 5-day user-delegation SAS (the
deployed app has only `AzureWebJobsStorage__accountName`, so the account-key
branch never ran in Azure). Dropping the stored URLs does not invalidate links
already handed out, so **right after this change is deployed to an
environment**, revoke the account's user delegation keys (needs Storage Account
Contributor or Owner on the account):

```bash
# test
az storage account revoke-delegation-keys -g rg-fs-judgepapers-test -n stfsjudge6sk7jbtiojqc4
# prod
az storage account revoke-delegation-keys -g rg-fs-judgepapers-prod -n stfsjudgeyb6bgkpr7dehk
```

The account is `stfsjudge` + the same `uniqueString` suffix as the Function App
(`func-fs-judgepapers-<suffix>`). Every user-delegation SAS on the account
stops working at once, including the 15-minute links minted in the last
minutes; the frontend fetches a fresh one on the next click. The e-mailed
judges' papers links are unaffected: `download_judge_papers` signs its short
SAS when the link is clicked. Run it after the deploy, never before: the old
code would keep minting 5-day links.

## Local testing

`func start` from `infra/functions/` serves the same routes on
`http://localhost:7071`. The secret is required locally too: add
`"PROXY_SHARED_SECRET": "local-dev-secret"` to the `Values` block of
`infra/functions/local.settings.json` and send it with the identity headers:

```bash
H=(-H "x-proxy-secret: local-dev-secret"
   -H "x-forwarded-user-tid: 00000000-0000-4000-8000-000000000001"
   -H "x-forwarded-user-oid: 00000000-0000-4000-8000-000000000002"
   -H "x-forwarded-user-email: markus@example.com")

curl -s "http://localhost:7071/api/check_user_permission" "${H[@]}"
# {"allowed": true, "email": "markus@example.com"}

# Missing/wrong secret, or no tid/oid → 401, whatever else is sent
curl -s -o /dev/null -w '%{http_code}\n' "http://localhost:7071/api/check_user_permission" \
  -H "x-proxy-secret: wrong" -H "x-forwarded-user-email: markus@example.com"
# 401
```

Competition routes also need the platform's access tables. Without
`PLATFORM_STORAGE_ACCOUNT` (and an identity with read access to the site's
`useridentities` / `competitionaccess` tables) they answer 503
`platform_unavailable` — except for an admin, who needs no lookup:

```bash
curl -s "http://localhost:7071/api/list_competitions" "${H[@]}" -H "x-forwarded-user-admin: 1"
```

The same headers work against a deployed Function App
(`https://func-fs-judgepapers-<hash>.azurewebsites.net/api/...`) using that
environment's real secret — useful for isolating whether a failure is in the
router or in the backend.
