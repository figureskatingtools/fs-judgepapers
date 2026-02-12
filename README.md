# Figure Skating Judge Papers Generator (Azure Function)

This project automates the creation of judging packets for figure skating competitions using a serverless Azure Function. It processes raw PDF exports (Start Lists, Judges Sheets, Panel info) stored in Azure Blob Storage and compiles them into personalized PDF files for each judge, referee, and technical official.

## Architecture

1.  **Input**: Source PDF files are uploaded to a folder within an Azure Blob Storage container (`fs-judgepapers`).
2.  **Processing**: An Azure Function (`generate_judging_papers`) is triggered via HTTP. It downloads the files, processes them (splitting, cover page generation, merging), and generates the packets.
3.  **Output**: The resulting PDF packets are uploaded back to the same Blob Storage container under a `judgePapers/` subfolder.

## Features

- **Serverless Processing**: Runs on Azure Functions (Python v2 model).
- **Managed Identity**: Uses Azure Managed Identity for secure access to Blob Storage.
- **PDF Manipulation**: Splits large judge sheets, generates custom cover pages, and merges documents.
- **Daily Summaries**: Aggregates individual packets into daily master files.

## Prerequisites

- **Azure Subscription**
- **Azure CLI**
- **Azure Functions Core Tools**
- **Python 3.10+**

## Deployment

### 1. Infrastructure (Bicep)

Deploy the Azure resources (Storage Account, Function App, Application Insights) using the provided Bicep files.

```bash
cd infra
az deployment sub create --location swedencentral --template-file main.bicep
```

### 2. Function Code

Deploy the Python function code to the created Function App.

```bash
cd infra/functions
func azure functionapp publish <function-app-name>
```

*Note: Replace `<function-app-name>` with the name outputted by the Bicep deployment.*

## Usage

1.  **Upload Source Files**:
    Upload your competition PDF files to the `fs-judgepapers` container in your Storage Account. Place them in a specific folder (e.g., `competition-2026`).
    
    Required files per segment:
    - `*_ISUPanelofJudgesandTechnicalPanel.pdf`
    - `*_StartListwithTimes.pdf`
    - `*_JudgesSheetAll.pdf`
    - Role-specific files (`*_RefereeSheet.pdf`, `*_TechnicalControllerSheet.pdf`, etc.)

2.  **Trigger the Function**:
    Send an HTTP POST request to your Function App URL.

    **URL**: `https://<function-app-name>.azurewebsites.net/api/generate_judging_papers`
    
    **Body**:
    ```json
    {
        "workingFolder": "competition-2026"
    }
    ```

3.  **Retrieve Results**:
    Once the function completes (returns 200 OK), check the `fs-judgepapers` container. You will find the generated PDFs in `competition-2026/judgePapers/`.

## Local Development

1.  **Configure Settings**:
    Create `infra/functions/local.settings.json`:
    ```json
    {
      "IsEncrypted": false,
      "Values": {
        "AzureWebJobsStorage": "<YOUR_STORAGE_CONNECTION_STRING>",
        "FUNCTIONS_WORKER_RUNTIME": "python"
      }
    }
    ```
    *Note: For local development, a connection string is required as Managed Identity works only in Azure.*

2.  **Install Dependencies**:
    ```bash
    cd infra/functions
    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Run Locally**:
    ```bash
    func start
    ```
