# Figure Skating Judge Papers Generator

A web application for generating judging packets for figure skating competitions. Users upload PDF exports from Figure Skating Manager (FSM), and the system automatically splits, categorizes, and merges them into personalized PDF packets for each judge, referee, and technical official.

## Architecture

| Layer | Technology |
|---|---|
| **Frontend** | TypeScript + Vite, served by Node.js proxy server |
| **Backend** | Python Azure Functions (HTTP triggers), pypdf & reportlab |
| **Auth** | Microsoft Entra ID via Azure App Service Easy Auth |
| **Storage** | Azure Blob Storage (PDFs) + Azure Table Storage (metadata) |
| **Infrastructure** | Azure Bicep (subscription-scoped) |

### How It Works

1. **Upload** — Source PDF files are uploaded via the web UI to Azure Blob Storage
2. **Process** — Azure Function splits judge sheets, creates cover pages, and merges documents into per-judge packets
3. **Download** — Generated packets are stored in Blob Storage with SAS-linked download URLs

## Features

- **PDF Processing Pipeline** — Split → Categorize → Cover pages → Merge → ZIP
- **Multi-language UI** — Finnish (default) and English
- **Category Management** — Categories loaded from Azure Table Storage (IJS/MUPI judging methods)
- **Competition Workflow** — List, create, upload PDFs, generate and download judging packets
- **Serverless & Secure** — Azure Functions with Managed Identity for storage access

## Branch Strategy

| Branch | Environment | Purpose |
|---|---|---|
| `dev` | Development | Active development, feature branches merge here |
| `test` | Test | Staging/QA, promoted from dev via PR |
| `main` | Production | Stable releases, promoted from test via PR |

## Prerequisites

- Azure Subscription
- Azure CLI
- Azure Functions Core Tools
- Python 3.10+
- Node.js 18+

## Deployment

### 1. Infrastructure

```bash
./deploy_infra.sh --client-id <ENTRA_CLIENT_ID>
```

Deploys Azure resources (Resource Group, Storage Account, Function App, Web App, Application Insights, RBAC) using Bicep.

### 2. Backend

```bash
./deploy_backend.sh -g <resource-group>
```

Packages and deploys the Python Azure Functions via ZIP deployment.

### 3. Frontend

```bash
./deploy_frontend.sh -g <resource-group>
```

Builds the Vite frontend, bundles with the Node.js server, and deploys to Azure Web App.

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
    npm install
    npm run dev
    ```

## Project Structure

```
├── frontend/              # TypeScript + Vite web application
│   ├── src/               # Frontend source code
│   ├── server.js          # Node.js proxy server for production
│   └── vite.config.ts     # Vite configuration
├── infra/
│   ├── main.bicep         # Infrastructure-as-Code (subscription-scoped)
│   ├── modules/           # Bicep modules (storage, function, webapp, RBAC)
│   └── functions/         # Python Azure Functions (backend)
├── backend_build/         # Backend build artifacts
├── deploy_infra.sh        # Infrastructure deployment script
├── deploy_backend.sh      # Backend deployment script
├── deploy_frontend.sh     # Frontend deployment script
└── start_locally.sh       # Local development startup script
```

## License

See [LICENSE](LICENSE) for details.
