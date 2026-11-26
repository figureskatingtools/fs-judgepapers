# Figure Skating Judge Papers Generator

A web application for generating judging packets for figure skating competitions. Users upload PDF exports from Figure Skating Manager (FSM), and the system automatically splits, categorizes, and merges them into personalized PDF packets for each judge, referee, and technical official.

> **The frontend now lives in the [figureskatingtools-site](https://github.com/figureskatingtools/figureskatingtools-site) repo** and is served at `https://figureskatingtools.com/judgepapers/`. This repo is backend-only (Python Functions + storage). See [PROXY-CONTRACT.md](PROXY-CONTRACT.md) for the router → Function App contract.

## Architecture

| Layer | Technology |
|---|---|
| **Frontend** | Hosted by the figureskatingtools-site router at `/judgepapers/` (not deployed from this repo) |
| **Backend** | Python Azure Functions (HTTP triggers), pypdf & reportlab |
| **Auth** | Microsoft Entra ID Easy Auth on the site router; identity reaches this backend as a forwarded header (see [PROXY-CONTRACT.md](PROXY-CONTRACT.md)) |
| **Storage** | Azure Blob Storage (PDFs) + Azure Table Storage (metadata) |
| **Infrastructure** | Azure Bicep (subscription-scoped) |

### How It Works

1. **Upload** — Source PDF files are uploaded via the web UI to Azure Blob Storage
2. **Process** — Azure Function splits judge sheets, creates cover pages, and merges documents into per-judge packets
3. **Download** — Generated packets are stored in Blob Storage; each view of the competition gives fresh download links valid for 15 minutes
   - **Copy link** gives a 5-day link to share anywhere (chat, WhatsApp, …): the same figureskatingtools.com link as the e-mail, capped at the competition's deletion date
4. **Send** — A merged judges'-papers PDF can be e-mailed (via Mailgun) to e.g. the competition manager: a predefined Finnish or English message with a 5-day download link on figureskatingtools.com (it redirects to the PDF, so no storage URL appears in the mail), CC'd to the Results Service e-mail

### Authentication

Sign-in is Microsoft Entra ID enforced by App Service Easy Auth on [figureskatingtools.com](https://figureskatingtools.com), which hosts every tool under its own path. One session covers all of them. The Function App deployed from this repo is anonymous at the platform level; the site router authenticates calls to it with forwarded identity headers (the Entra tenant and object id, `x-forwarded-user-tid` / `x-forwarded-user-oid`, plus e-mail and name for display) and a shared secret (`x-proxy-secret`, required — the backend refuses every request without it). Each competition is private to its owner and the users it is shared with on the site: this backend checks the platform's grants (read or write) on every route that touches a competition — see [PROXY-CONTRACT.md](PROXY-CONTRACT.md).

## Features

- **PDF Processing Pipeline** — Split → Categorize → Cover pages → Merge → ZIP
- **Category names as printed** — categories are named as FSM prints them on the uploaded `JudgesSheetAll`, in the competition's own language
- **Category Management** — Categories loaded from Azure Table Storage (ISU/MUPI judging methods); synchronized-skating categories can be switched between ISU and MUPI per competition
- **Competition Workflow** — List, create, upload PDFs, generate and download judging packets
- **Trainees** — judge and technical trainees named in the tool get their own packets, with copies of an official's sheets carrying the trainee's name (see [Trainees](#trainees))
- **Serverless & Secure** — Azure Functions with Managed Identity for storage access
- **Retention** — a competition's files and e-mail addresses are deleted when the competition is deleted on figureskatingtools.com (anonymous usage statistics stay). A record no competition stands behind any more (or one from before the shared competitions) is deleted the same way once it is more than 60 days old. Logs are kept 90 days.

## Judging Method: ISU vs MUPI

Every category has a judging method, **ISU** or **MUPI**, which decides which FSM exports a segment is expected to have:

| Method | Expected per segment | Expected once per category |
|---|---|---|
| MUPI | `StartListwithTimes`, `ISUPanelofJudgesandTechnicalPanel`, `JudgesSheetAll`, `RefereeSheet` | `CalculationSetupVerificationforReferee` |
| ISU | the MUPI set plus `PlannedProgramContent`, `TechnicalControllerSheet` and at least one `TechnicalSpecialistSheet` | `CalculationSetupVerificationforReferee` |

The method comes from the `categories` Azure Table (`JudgingMethod` column, one row per category abbreviation). The generated packets themselves do not depend on it: each official gets the sheets that exist for their role, so the method only controls the [missing-papers check](#missing-papers), the ISU/MUPI marker on the category card, and the `JudgingMethod` value recorded in the competition's statistics.

### Switching a synchronized skating category

Synchronized skating categories are judged under ISU in some competitions and under MUPI in others, so for them the table value is only a default. On the competition page, every synchronized skating category card shows an **ISU | MUPI** control in its header instead of the plain marker:

1. Click the method the competition uses. The card re-validates at once: switching to MUPI drops the three ISU-only files from the missing list, switching to ISU adds them back.
2. The choice is saved immediately for **this competition only** and survives reloads. All `#N` split groups of the same category switch together.
3. Clicking the method that matches the table default clears the override, so a later correction of the table applies again to this competition.

The control appears only for categories whose table row has the competition type `Synchronized skating` (compared case-insensitively). Other categories cannot be switched; change their `JudgingMethod` in the table instead, which affects every competition.

### Where the choice lives

The override is stored in the competition's `metadata.json`:

```json
{ "judgingMethodOverrides": { "FSKXSYNCHRONMLAIKU": "ISU" } }
```

Keys are category abbreviations, values `ISU` or `MUPI`; only deviations from the table default are kept. The UI writes it through `POST /api/save_competition_settings` (`{ "id": "<competition id>", "settings": { "judgingMethodOverrides": { ... } } }`, full map on every save, invalid values are rejected with 400). `GET /api/get_competition_details` returns each file's effective `judgingMethod` together with `defaultJudgingMethod` and `judgingMethodOverridable`, plus the stored map as `judgingMethodOverrides`. `generate_judging_papers` reads the map from `metadata.json` itself when it records statistics, so the client cannot desynchronise it.

`save_competition_settings` stores only these two settings keys, `judgingMethodOverrides` and `trainees` (any other key is a 400), each replaced wholesale. It reads the existing `metadata.json` strictly: if storage can't be read it answers 503 `Storage unavailable, try again` and writes nothing. The write is conditional on the file being unchanged since that read; otherwise the save re-reads, re-merges and retries, and after three attempts answers 409 `Settings changed meanwhile, try again`. A save therefore never wipes the file's other contents, and a concurrent save of the other key (or a rename) is not lost. Two saves of the same key still end with the last one, since each sends its full value. `get_competition_details` and `generate_judging_papers` answer the same 503 when `metadata.json` can't be read, never empty settings; a missing file means no settings. `metadata.json` itself (and `init.md`) can't be removed through `delete_file`.

## Missing papers

The competition page marks what is missing, in the file list itself:

Papers are expected only where some are uploaded. A competition is rarely uploaded in one go: a later segment's papers (a Free Skating in reverse order of the Short Program's result) can be made only once the earlier one is skated, and often only the first day exists before the competition. A category may also have a single segment.

- **A missing file.** A segment is checked once one of its files is uploaded: each file it should still have (per the table above) is a `MISSING` row in that segment. The calculation setup is expected once the category has such a segment.
- **A scheduled segment that is not uploaded yet.** When `CompetitionSchedule.pdf` is uploaded, every segment in it is looked up among the uploads by the category and segment FSM prints (on the schedule, as the title of the segment's `StartListwithTimes` and on line 2 of its `JudgesSheetAll`). A segment with entries and no papers is a quiet grey *Not uploaded yet* note in its category, or in a grey category of its own when nothing of the category is uploaded. It is not an error, and the category keeps its green check. Segments with 0 entries are left out.
- **A forgotten category.** The one case where the schedule marks something `MISSING`: no segment of a category has any papers (nothing uploaded, or only its calculation setup), and its first scheduled segment is skated on a day for which another category's papers are uploaded. A first segment waits for no result, so on a day that is being prepared it should be there. The category's later segments stay quiet notes, and so does a category that starts on a day nothing is uploaded for.
- **A category with only its calculation setup** and no schedule to say when it skates is shown as *no segment papers yet*: neither an error nor a green check.

With anything marked `MISSING`, **Generate Papers** asks first: it lists what is missing, and the papers are generated only after ticking *Generate anyway, without the missing papers*. A segment without its panel or its time schedule is left out of the papers; any other missing sheet is simply not in the packets. Segments that are merely not uploaded yet are never asked about. The button is disabled only when no segment at all could be generated.

The check is made by the site from `GET /api/get_competition_details`, whose `scheduleCheck` lists the schedule's segments as `found`, `missing` or `notNeeded` (`null` without a readable schedule). `generate_judging_papers` itself does not refuse anything.

## Trainees

A trainee follows a segment as a judge or as a technical specialist without being on the FSM panel. Trainees are named in this tool, not in FS Manager, and stored in the competition's `metadata.json`, so they survive a re-import of the exports:

```json
{ "trainees": [
    { "name": "Väinö Åkerström", "role": "judge",     "segments": ["FSKMSINGLES-JUNIOR----QUAL000100--"] },
    { "name": "Väinö Åkerström", "role": "technical", "segments": ["FSKXICEDANCEJUNIOR----QUAL000100--"] }
] }
```

`segments` are FSM file prefixes. The UI sends the full list through `save_competition_settings` (`settings.trainees`; at most 50 trainees, a malformed list is rejected with 400; names are stored in Unicode NFC) and gets it back from `get_competition_details` as `trainees`. When generating:

- Each trainee gets a personal packet like an official's: the cover (role `Judge Trainee` / `Technical Trainee`), then for each segment the usual time schedule or segment cover and a copy of an official's sheets with the trainee's name printed over the official's: **Judge No. 1's** judges' sheets for a judge trainee (the next judge's when Judge No. 1 has none), the **Technical Specialist 1** sheet for a technical trainee (**Technical Specialist 2** in a Pattern Dance segment). Withdrawn skaters' pages are left out as for the officials.
- A trainee who is the same person as a panel member of that day gets one packet with both, and so does one trainee listed in both roles. "The same person" means the names differ only in case, spacing and diacritics: `Vaino Akerstrom` joins FSM's `Väinö ÅKERSTRÖM`, but `Annamaria Xu` does not join `Anna-Maria XU`, although both would give the file name `annamaria_xu`. Such a trainee gets a packet of their own named `annamaria_xu-trainee` (on every day, if the look-alike is on any of the trainee's days), with their own name on the cover and the sheets. Two trainees who are different people but would give the same packet file name (e.g. `Anna-Maria Xu` and `Annamaria Xu`) are refused when saving: `trainee N: name is too close to trainee M's (they would share one packet); use the same spelling for the same person`. A name with no a-z/0-9 letter after transliteration (e.g. all Cyrillic) gets a packet file named `trainee-` + 8 hex digits of a hash of the name.
- A trainee task is dropped where the same person already holds those sheets (judge or referee of the segment; TS 1 or TS 2).
- A stored segment that is not among the current files is kept and ignored.
- If a sheet's layout can't be stamped safely, the official's copy goes in unchanged. If there is no sheet to copy (no named judge, no Technical Specialist sheet, or an unreadable one; an unreadable sheet costs the trainee that segment, not the whole run), the segment gets only its cover and time schedule. The generate response then ends with one warning, e.g. `Warning: 3 page(s) of trainee sheets still show the original name. 1 trainee segment(s) got no sheets to copy.`
- The day document's check block counts the trainees (`… · 2 trainees`).

The official's name stays in the copied sheets' text layer (covered on the page and in print, but found by a text search of the PDF).

## Branch Strategy

| Branch | Environment | Purpose | Deploy trigger |
|---|---|---|---|
| `test` | Test | Active development and staging/QA, feature branches merge here | Manual (`workflow_dispatch`) — run the workflow from the `test` branch |
| `main` | Production | Stable releases, promoted from test via PR | Automatic on push to `main` |

## Prerequisites

- Azure Subscription
- Azure CLI
- Azure Functions Core Tools
- Python 3.10+
- Node.js 18+

## Deployment

### 1. Infrastructure

```bash
read -rs MAILGUN_API_KEY && export MAILGUN_API_KEY   # optional, kept out of argv
./deploy_infra.sh -g <resource-group> [--proxy-secret <SECRET>] [--example-video-url <URL>] [--orphan-purge true|false]
```

Without `--orphan-purge`, the script keeps the Function App's current `ORPHAN_PURGE_ENABLED` (the nightly orphan deletion); if it can't read it (no app yet, no permission) it deploys `false` (dry run) and says so, so a manual run never re-enables a purge the workflow deployed off.

Deploys Azure resources (Resource Group, Storage Account, Function App, Application Insights with a 90-day Log Analytics workspace, RBAC) using Bicep. No Web App, DNS or custom domain is created here — hosting and the domain belong to the figureskatingtools-site repo.

### 2. Backend

```bash
./deploy_backend.sh -g <resource-group>
```

Packages and deploys the Python Azure Functions via ZIP deployment.

### 3. Frontend

Deployed from the figureskatingtools-site repo, not from here.

## Local Development

1. **Configure local settings** — Create `infra/functions/local.settings.json`:
    ```json
    {
      "IsEncrypted": false,
      "Values": {
        "AzureWebJobsStorage": "<YOUR_STORAGE_CONNECTION_STRING>",
        "FUNCTIONS_WORKER_RUNTIME": "python",
        "PROXY_SHARED_SECRET": "local-dev-secret"
      }
    }
    ```
    `PROXY_SHARED_SECRET` is required: without it every request is refused
    (401). Without `PLATFORM_STORAGE_ACCOUNT` the competition access check
    can't run, so only admin calls (`x-forwarded-user-admin: 1`) work locally
    — see [PROXY-CONTRACT.md](PROXY-CONTRACT.md#local-testing).
    Optional: `MAILGUN_API_KEY` (without it `send_judge_papers` only logs the
    subject and recipients instead of sending — the body isn't logged because
    it holds a live download link; use the send dialog's Preview to see it), `EXAMPLE_VIDEO_URL`, and the
    `MAILGUN_API_BASE` / `MAILGUN_DOMAIN` / `EMAIL_FROM_ADDRESS` overrides (see
    `infra/functions/email_sender.py`).

2. **Backend:**
    ```bash
    cd infra/functions
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    func start
    ```

3. **UI:** run the frontend from the
   [figureskatingtools-site](https://github.com/figureskatingtools/figureskatingtools-site)
   repo and point its judgepapers function-app URL at `http://localhost:7071`.

## Project Structure

```
├── infra/
│   ├── main.bicep         # Infrastructure-as-Code (subscription-scoped)
│   ├── modules/           # Bicep modules (storage, function, RBAC)
│   └── functions/         # Python Azure Functions (backend)
├── PROXY-CONTRACT.md      # Router → Function App header contract
├── deploy_infra.sh        # Infrastructure deployment script
└── deploy_backend.sh      # Backend deployment script
```

## Licence

The source code is licensed under the GNU Affero General Public License, version 3 only (SPDX:
`AGPL-3.0-only`); see [LICENSE](LICENSE).

The figureskatingtools name, logo and document templates (including
`infra/functions/assets/logo.png`) are not covered by this licence and remain the property of
their owner; all rights reserved.

The bundled fonts in `infra/functions/assets/fonts/` keep their own licence, the SIL Open Font
License 1.1 (`OFL.txt`, `LiberationSans-OFL.txt`).
