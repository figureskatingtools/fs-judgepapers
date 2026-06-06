# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

Web app that generates judging packets for figure skating competitions. Users upload PDF exports from Figure Skating Manager (FSM); the backend splits, categorizes, and merges them into per-judge/referee/official PDF packets. UI is bilingual (Finnish default, English).

## Commands

```bash
# Full local stack (Functions backend + Vite dev server + SWA CLI auth emulator)
./start_locally.sh

# Frontend only (cd frontend)
npm install
npm run dev        # Vite dev server on :5173
npm run build      # tsc + vite build

# Backend only (cd infra/functions)
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
func start         # Azure Functions on :7071 (requires local.settings.json, see README)

# Deployment (manual; CI does this automatically on push to test/main)
./deploy_infra.sh --client-id <ENTRA_CLIENT_ID>   # Bicep, subscription-scoped
./deploy_backend.sh -g <resource-group>            # Functions ZIP deploy
./deploy_frontend.sh -g <resource-group>           # Vite build + server.js → Web App
```

There are no tests and no linter configured. Local dev requires accessing the app through the SWA CLI emulator (`:4280`), not Vite directly — Vite has no `/api` proxy; SWA CLI routes `/api/*` to the Functions host and emulates Easy Auth.

## Architecture

Three pieces, deployed separately:

1. **Frontend** (`frontend/`) — No-framework TypeScript SPA. Almost all UI logic lives in `src/main.ts` (~1200 lines of view-rendering functions that write `innerHTML`); `src/validate.ts` checks that each category/segment has the required FSM file set. In production it's served by `frontend/server.js`, a zero-dependency Node HTTP server that serves static files, exposes `/userinfo`, and proxies `/api/*` to the Function App.

   The site banner/nav comes from **`@figureskatingtools/shared-ui`** (published to GitHub Packages from the `figureskatingtools-site` repo) — `renderSiteNav` is rendered into `#site-nav-container` by `init()` once auth state is known, with the app's own nav (Competitions / New Competition) as `appNavItems` dropdown entries wired via `[data-nav-action]` listeners, and the user menu rendered into the nav's `#fst-nav-right` slot. **`npm install` requires auth**: GitHub Packages needs a token with `read:packages` even for reads — set `NODE_AUTH_TOKEN` (e.g. `NODE_AUTH_TOKEN=$(gh auth token) npm install`; the token substitution lives in `frontend/.npmrc`). CI uses the workflow `GITHUB_TOKEN` via `packages: read` permission. To change the shared banner (e.g. add a tool), edit `DEFAULT_TOOLS` in the figureskatingtools-site repo's `packages/shared-ui`, bump its version, and `npm update` here.

2. **Backend** (`infra/functions/`) — Python Azure Functions, all HTTP-triggered, defined in `function_app.py`. The PDF pipeline lives in plain modules called by the `generate_judging_papers` endpoint:
   - `processor.py` — orchestrator: parse CompetitionSchedule → extract segment names → split judge sheets → generate cover pages → merge per-person packets → ZIP. Runs on local temp dirs after downloading blobs.
   - `split_judges_sheets.py` — splits `*_JudgesSheetAll.pdf` into per-judge/referee PDFs by parsing role lines; also detects withdrawn skaters.
   - `create_cover_pages.py` — reportlab cover/segment pages, strikethrough start lists.
   - `combine_judging_papers.py` — date/time/panel extraction and final merge.
   - `categories.py` — table-driven category lookup (see below).
   - `competition_schedule.py` — parses the CompetitionSchedule PDF for start times.

3. **Infra** (`infra/main.bicep` + `modules/`) — subscription-scoped Bicep: resource group, storage, Function App, Web App, user-assigned managed identity for Easy Auth (federated credential, no client secret), RBAC. Per-env params in `infra/parameters/{test,prod}.bicepparam`. Custom domains (`judgepapers.figureskatingtools.com` prod, `test.judgepapers.figureskatingtools.com` test) are bound by `dns.bicep` (CNAME + asuid TXT in the shared `figureskatingtools.com` zone in `rg-fs-dns`, which is deployed by the separate landing-page repo) plus `webapp-customdomain.bicep`/`sni-enable.bicep` (hostname binding → managed cert → SNI, split across modules due to Azure's two-PUT constraint). Driven by the `customDomain` param / `CUSTOM_DOMAIN` GitHub environment variable.

### Auth chain (important when touching any endpoint)

All function routes are `AuthLevel.ANONYMOUS`; real auth is Entra ID Easy Auth on the Web App. Identity flows: Easy Auth injects `X-MS-CLIENT-PRINCIPAL*` headers → `server.js` extracts the email and forwards it as `X-Forwarded-User-Email` to the Function App → `get_user_email_from_header()` in `function_app.py` tries (in order) direct Easy Auth header, forwarded header, base64 SWA principal, then Bearer JWT claims. Every endpoint must call it and return 401 on `None`. `is_user_allowed()` currently allows all authenticated users and is the hook for a future allowlist.

**The Function App itself must be `AllowAnonymous`** (`function.bicep` `globalValidation`), *not* `requireAuthentication: true` — the proxy forwards only the email header, no bearer token, so Easy Auth enforcement would 401 every proxied request (`WWW-Authenticate: Bearer`, empty body) before the app's header check runs.

Because the function endpoint is public, a **shared secret** stops anyone from calling it directly with a spoofed email header: the Web App holds `PROXY_SHARED_SECRET`, `server.js` sends it as `X-Proxy-Secret` on every proxied call, and `_proxy_secret_ok()` (called first in `get_user_email_from_header`) rejects requests whose header doesn't match → 401. **Enforced only when the env var is set** (local/dev and brief pre-rollout windows fail open). The secret is a per-environment GitHub Environment secret, injected into the Function App via the `proxySharedSecret` Bicep param (in the authoritative `appSettings` array) and into the Web App via the deploy workflow's `az webapp config appsettings set`. Two rejected alternatives: inbound **IP restrictions** `403` the GitHub runner during the Flex deploy's sync-triggers/health-check and hang the pipeline; **Network Security Perimeter** can't hold `Microsoft.Web` apps (not an onboarded NSP resource type).

### Storage layout & dual credential pattern

Every competition has an immutable 8-char hex **id** (`uuid4().hex[:8]`) — the identifier in all API calls and table keys. Competition **names are not unique** (trial-and-error re-creation is allowed); the blob folder is `{sanitized-name}-{id}/`, stored on the entity as `FolderPath` (endpoints resolve id → FolderPath via `get_competition_entity`).

Blob container `fs-judgepapers`:
- `{name}-{id}/metadata.json` — id/name/createdBy/createdDate/language; its existence defines the competition "folder"
- `{name}-{id}/{PREFIX}_{Suffix}.pdf` — uploaded FSM exports
- `{name}-{id}/judgePapers/...` — generated output (ZIPs + merged PDFs get 5-day SAS links)

Tables: `competitions` is the **authoritative, permanent history** of every competition ever created (PartitionKey `GLOBAL`, RowKey = id; rows are never deleted — there is no blob-scan fallback anymore). Columns: `Name`, `FolderPath`, `Visible` (controls UI listing), `CreatedBy/CreatedDate`, `DeletedDate/DeletedBy`, and usage counters for statistics (`UploadedFileCount`, `GenerateRunCount`, `LastGeneratedDate`, maintained best-effort by `_bump_competition_counters`). Deleting a competition removes its blobs and `generatedpapers` rows but only soft-deletes the `competitions` row (`Visible=false` + delete audit). Legacy name-keyed rows are lazily migrated by `list_competitions` (`migrate_legacy_row`: new id-keyed row with `FolderPath` = old name folder, generatedpapers re-keyed). `generatedpapers` holds SAS download links (PartitionKey = competition id), `categories` is the category registry.

Every storage client helper supports two credential modes and new storage code must too: managed identity when `AzureWebJobsStorage__accountName` is set (production; SAS via user-delegation key), connection string via `AzureWebJobsStorage` otherwise (local dev; SAS via account key).

### Category/filename parsing

FSM filenames are `{CATEGORY_ABBREVIATION}{SEGMENT_MARKER}_{Suffix}.pdf` (e.g. segment markers `QUAL`/`FNL`, split suffixes `#N`). Abbreviations are not hardcoded — they live in the `categories` Azure Table (RowKey = abbreviation, with `DisplayName`, `DisplayNameFi`, `JudgingMethod` = ISU|MUPI) and are matched longest-prefix-first. `categories.py` caches the table in memory for 5 minutes. This parsing logic is duplicated in spirit on the frontend (`validate.ts` consumes the parsed structure from `get_competition_details`).

Human-readable segment names aren't in filenames; they're extracted from line 2 of each `JudgesSheetAll.pdf` (stripping the category display name prefix, with punctuation-tolerant fuzzy matching). This enrichment happens both in `function_app.py` (`get_competition_details`) and `processor.py`.

## Branch / Deploy Strategy

`test` → `main` promote via PRs. `.github/workflows/deploy.yml` deploys infra (Bicep), backend, and frontend to the matching GitHub environment: **push to `main` auto-deploys prod**; **`test` is manual-only** via `workflow_dispatch` (run the workflow from the branch whose code you want, pick the environment) — there is no `test`-branch push trigger. This mirrors the figureskatingtools-site repo. The workflow also patches the Entra app registration (redirect URIs, federated identity credential) and disables the Easy Auth token store.
