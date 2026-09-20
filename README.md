# Figure Skating Judge Papers Generator

A web application for generating judging packets for figure skating competitions. Users upload PDF exports from Figure Skating Manager (FSM), and the system automatically splits, categorizes, and merges them into personalized PDF packets for each judge, referee, and technical official.

> **The frontend now lives in the [figureskatingtools-site](https://github.com/figureskatingtools/figureskatingtools-site) repo** and is served at `https://figureskatingtools.com/judgepapers/`. This repo is backend-only (Python Functions + storage); the local `frontend/` directory is legacy and no longer built or deployed. See [PROXY-CONTRACT.md](PROXY-CONTRACT.md) for the router → Function App contract.

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
3. **Download** — Generated packets are stored in Blob Storage with SAS-linked download URLs

### Authentication

Sign-in is Microsoft Entra ID enforced by App Service Easy Auth on [figureskatingtools.com](https://figureskatingtools.com), which hosts every tool under its own path. One session covers all of them. The Function App deployed from this repo is anonymous at the platform level; the site router authenticates calls to it with a forwarded identity header (`x-forwarded-user-email`) plus a shared secret (`x-proxy-secret`) — see [PROXY-CONTRACT.md](PROXY-CONTRACT.md).

## Features

- **PDF Processing Pipeline** — Split → Categorize → Cover pages → Merge → ZIP
- **Multi-language UI** — Finnish (default) and English
- **Category Management** — Categories loaded from Azure Table Storage (ISU/MUPI judging methods); synchronized-skating categories can be switched between ISU and MUPI per competition
- **Competition Workflow** — List, create, upload PDFs, generate and download judging packets
- **Serverless & Secure** — Azure Functions with Managed Identity for storage access

## Judging Method: ISU vs MUPI

Every category has a judging method, **ISU** or **MUPI**, which decides which FSM exports a segment needs before packets can be generated:

| Method | Required per segment | Required once per category |
|---|---|---|
| MUPI | `StartListwithTimes`, `ISUPanelofJudgesandTechnicalPanel`, `JudgesSheetAll`, `RefereeSheet` | `CalculationSetupVerificationforReferee` |
| ISU | the MUPI set plus `PlannedProgramContent`, `TechnicalControllerSheet` and at least one `TechnicalSpecialistSheet` | `CalculationSetupVerificationforReferee` |

The method comes from the `categories` Azure Table (`JudgingMethod` column, one row per category abbreviation). The generated packets themselves do not depend on it: each official gets the sheets that exist for their role, so the method only controls the required-file check, the ISU/MUPI marker on the category card, and the `JudgingMethod` value recorded in the competition's statistics.

### Switching a synchronized skating category

Synchronized skating categories are judged under ISU in some competitions and under MUPI in others, so for them the table value is only a default. On the competition page, every synchronized skating category card shows an **ISU | MUPI** control in its header instead of the plain marker:

1. Click the method the competition uses. The card re-validates at once: switching to MUPI drops the three ISU-only files from the missing list, switching to ISU adds them back, and the Generate button follows.
2. The choice is saved immediately for **this competition only** and survives reloads. All `#N` split groups of the same category switch together.
3. Clicking the method that matches the table default clears the override, so a later correction of the table applies again to this competition.

The control appears only for categories whose table row has the competition type `Synchronized skating` (compared case-insensitively). Other categories cannot be switched; change their `JudgingMethod` in the table instead, which affects every competition.

### Where the choice lives

The override is stored in the competition's `metadata.json` next to the language setting:

```json
{ "judgingMethodOverrides": { "FSKXSYNCHRONMLAIKU": "ISU" } }
```

Keys are category abbreviations, values `ISU` or `MUPI`; only deviations from the table default are kept. The UI writes it through `POST /api/save_competition_settings` (`{ "id": "<competition id>", "settings": { "judgingMethodOverrides": { ... } } }`, full map on every save, invalid values are rejected with 400). `GET /api/get_competition_details` returns each file's effective `judgingMethod` together with `defaultJudgingMethod` and `judgingMethodOverridable`, plus the stored map as `judgingMethodOverrides`. `generate_judging_papers` reads the map from `metadata.json` itself when it records statistics, so the client cannot desynchronise it.

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
./deploy_infra.sh [--proxy-secret <SECRET>]
```

Deploys Azure resources (Resource Group, Storage Account, Function App, Application Insights, RBAC) using Bicep. No Web App, DNS or custom domain is created here — hosting and the domain belong to the figureskatingtools-site repo.

### 2. Backend

```bash
./deploy_backend.sh -g <resource-group>
```

Packages and deploys the Python Azure Functions via ZIP deployment.

### 3. Frontend

Deployed from the figureskatingtools-site repo, not from here.

## Local Development

### Quick Start

```bash
./start_locally.sh
```

This starts the Azure Functions backend, the Vite dev server, and SWA CLI for local auth emulation.

### Manual Setup

1. **Configure local settings** — Create `infra/functions/local.settings.json`:
    ```json
    {
      "IsEncrypted": false,
      "Values": {
        "AzureWebJobsStorage": "<YOUR_STORAGE_CONNECTION_STRING>",
        "FUNCTIONS_WORKER_RUNTIME": "python"
      }
    }
    ```

2. **Backend:**
    ```bash
    cd infra/functions
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    func start
    ```

3. **Frontend:**
    ```bash
    cd frontend
    NODE_AUTH_TOKEN=$(gh auth token) npm install   # token needs read:packages scope
    npm run dev
    ```

    > The frontend consumes `@figureskatingtools/shared-ui` (the shared site
    > navigation) from GitHub Packages, which requires an authenticated token
    > with `read:packages` even for installs. Grant the scope once with
    > `gh auth refresh -s read:packages`, or use a classic PAT.

## Project Structure

```
├── frontend/              # LEGACY — moved to figureskatingtools-site; not built or deployed
├── infra/
│   ├── main.bicep         # Infrastructure-as-Code (subscription-scoped)
│   ├── modules/           # Bicep modules (storage, function, RBAC)
│   └── functions/         # Python Azure Functions (backend)
├── PROXY-CONTRACT.md      # Router → Function App header contract
├── backend_build/         # Backend build artifacts
├── deploy_infra.sh        # Infrastructure deployment script
├── deploy_backend.sh      # Backend deployment script
└── start_locally.sh       # Local development startup script
```

## License

See [LICENSE](LICENSE) for details.
