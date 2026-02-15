# Judge Paper Creator - User Instructions

Welcome to the **Judge Paper Creator**! This application helps you quickly and efficiently create judging packets for figure skating competitions. Simply upload PDF exports from Figure Skating Manager (FSM), and the system will automatically process them into organized packets for each judge, referee, and technical official.

## Table of Contents

- [Getting Started](#getting-started)
- [Step-by-Step Guide](#step-by-step-guide)
  - [1. Signing In](#1-signing-in)
  - [2. Creating a New Competition](#2-creating-a-new-competition)
  - [3. Uploading PDF Files](#3-uploading-pdf-files)
  - [4. Validating Files](#4-validating-files)
  - [5. Generating Judge Papers](#5-generating-judge-papers)
  - [6. Downloading Files](#6-downloading-files)
- [Understanding the System](#understanding-the-system)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Support](#support)

---

## Getting Started

Before you begin, make sure you have:
- **Access to the application** — Contact the administrator if you haven't received login credentials
- **PDF exports from Figure Skating Manager** — The system requires specific PDF files exported from FSM
- **A modern web browser** — Chrome, Firefox, Edge, or Safari recommended

---

## Step-by-Step Guide

### 1. Signing In

When you first visit the application, you'll see the welcome screen.

![Welcome Screen](screenshots/welcome-screen.png)
*Screenshot placeholder: Welcome screen with "Sign In" button*

**Steps:**
1. Click the **"Sign In"** button
2. Enter your Microsoft credentials
3. Authorize the application when prompted

Once signed in, you'll see your name in the top-right corner and access to the main navigation menu.

![Signed In View](screenshots/signed-in-view.png)
*Screenshot placeholder: Main interface after successful sign-in*

---

### 2. Creating a New Competition

To start working on a new competition:

![Welcome Dashboard](screenshots/welcome-dashboard.png)
*Screenshot placeholder: Welcome dashboard with "New Competition" button*

**Steps:**
1. Click **"New Competition"** from the top navigation or welcome screen
2. Enter the competition details:
   - **Competition Name** — A descriptive name for your event (e.g., "Spring Championships 2026")
   - **Date** — The date of the competition
3. Click **"Create"** to save the competition

![Create Competition Form](screenshots/create-competition-form.png)
*Screenshot placeholder: Create competition form with input fields*

After creation, the competition will appear in your competitions list, and you'll be automatically taken to the competition details page.

---

### 3. Uploading PDF Files

Once your competition is created, it's time to upload the PDF files exported from Figure Skating Manager.

![Competition Details Page](screenshots/competition-details.png)
*Screenshot placeholder: Competition details page with upload area*

**Steps:**
1. Open the competition from the competitions list
2. Locate the **Upload Files** section
3. Click **"Choose Files"** or drag and drop PDF files into the upload area
4. Select all required PDF files from Figure Skating Manager:
   - Judge sheets
   - Start lists
   - Program content sheets
   - Other required documents
5. Click **"Upload"** to begin the upload process

![File Upload in Progress](screenshots/upload-progress.png)
*Screenshot placeholder: File upload with progress indicator*

**Important Notes:**
- You can upload multiple files at once
- Supported file format: PDF only
- The system will automatically organize files by category

---

### 4. Validating Files

After uploading, the system automatically validates your files to ensure all required documents are present.

![Validation in Progress](screenshots/validation-progress.png)
*Screenshot placeholder: Validation status screen*

The validation process checks:
- ✓ All required PDF types are present
- ✓ Files are properly formatted and readable
- ✓ Judge names and categories are correctly identified
- ✓ No duplicate or conflicting files

**Validation Results:**

If validation is successful:
![Validation Success](screenshots/validation-success.png)
*Screenshot placeholder: Green checkmark with "All files validated successfully" message*

If there are issues:
![Validation Errors](screenshots/validation-errors.png)
*Screenshot placeholder: Warning message listing missing or problematic files*

**What to do if validation fails:**
1. Review the error messages to identify which files are missing or incorrect
2. Export the missing files from Figure Skating Manager
3. Upload the additional files
4. The system will automatically re-validate

---

### 5. Generating Judge Papers

Once all files are validated, you can generate the judge papers.

![Generate Papers Button](screenshots/generate-button.png)
*Screenshot placeholder: "Generate Papers" button highlighted*

**Steps:**
1. Click the **"Generate Papers"** button
2. Wait while the system processes your files (this may take a few minutes)
3. The system will:
   - Split judge sheets by individual judges
   - Create cover pages for each packet
   - Organize documents by segment and category
   - Merge everything into per-judge PDF packets
   - Create ZIP archives for easy distribution

![Generation in Progress](screenshots/generation-progress.png)
*Screenshot placeholder: Progress bar showing paper generation status*

---

### 6. Downloading Files

When generation is complete, download links will appear.

![Download Links Available](screenshots/download-links.png)
*Screenshot placeholder: List of download links for judge packets and ZIP files*

**Available downloads:**
- **Individual Judge Packets** — Separate PDF for each judge, referee, and technical official
- **ZIP Archives** — All packets bundled by role or segment
- **Combined Booklets** — Complete sets organized by category

**Download Options:**
1. Click any download link to save files to your computer
2. Use the **"Copy Link"** button to share download URLs with others
3. Download all files at once using the master ZIP archive

![Downloaded Files](screenshots/downloaded-files.png)
*Screenshot placeholder: File explorer showing downloaded judge packets*

---

## Understanding the System

### How the System Works

The Judge Paper Creator follows a simple pipeline:

```
Upload PDFs → Validate → Split by Judge → Add Cover Pages → Merge → Generate ZIP → Download
```

1. **Upload** — Your PDF files are securely stored in Azure Blob Storage
2. **Split** — Judge sheets are automatically separated by judge name
3. **Categorize** — Documents are organized by segment and category (IJS/MUPI methods)
4. **Cover Pages** — Professional cover pages are generated with competition details
5. **Merge** — All documents for each judge are combined into a single PDF packet
6. **ZIP Archives** — Packets are bundled for easy distribution

### Supported PDF Types

The system recognizes and processes these PDF types from Figure Skating Manager:
- Judge evaluation sheets
- Start lists (starting orders)
- Program content sheets
- Technical panel sheets
- Referee sheets

### Language Support

The application supports multiple languages:
- **Finnish** (default)
- **English**

You can switch languages using the language selector in the interface.

---

## Troubleshooting

### Common Issues and Solutions

#### Problem: "File upload failed"
**Solutions:**
- Check your internet connection
- Ensure the file is a valid PDF
- Try uploading files one at a time
- Refresh the page and try again

#### Problem: "Validation failed - missing required files"
**Solutions:**
- Review the error message to see which files are missing
- Export the required files from Figure Skating Manager
- Upload the missing files
- Ensure file names haven't been modified after export

#### Problem: "Generation taking too long"
**Solutions:**
- Large competitions with many judges may take 5-10 minutes to process
- Do not refresh the page while generation is in progress
- If stuck for more than 15 minutes, contact support

#### Problem: "Download links not working"
**Solutions:**
- Links are valid for a limited time — regenerate if expired
- Check your browser's download settings
- Try copying the link and opening it in a new tab
- Disable browser extensions that might block downloads

#### Problem: "Cannot access the application"
**Solutions:**
- Ensure you have been granted access by the administrator
- Check that you're using the correct Microsoft account
- Clear your browser cache and cookies
- Try signing in using an incognito/private browsing window

---

## FAQ

**Q: How long are the generated files stored?**
A: Generated files are typically stored for 30 days after creation. Download them promptly after generation.

**Q: Can I edit the competition after files are generated?**
A: Yes, you can upload additional files or regenerate papers at any time.

**Q: Can multiple people work on the same competition?**
A: Yes, all authorized users can access and work on any competition.

**Q: What happens if I upload the wrong file?**
A: Simply upload the correct file. The system will use the most recent version.

**Q: Can I delete a competition?**
A: Contact the administrator if you need to remove a competition from the system.

**Q: How do I export PDFs from Figure Skating Manager?**
A: Refer to the Figure Skating Manager documentation for export instructions. Ensure you export all judge sheets, start lists, and program content sheets.

**Q: Are there any file size limits?**
A: Individual PDF files should be under 50 MB. Contact support if you have larger files.

**Q: Can I use this offline?**
A: No, the application requires an internet connection to process files and generate papers.

---

## Support

### Getting Help

If you encounter issues or have questions not covered in these instructions:

**Email Support:**
- Send bug reports or feature requests to: [markus@lintuala.fi](mailto:markus@lintuala.fi)

**What to Include in Support Requests:**
1. Description of the problem
2. Steps to reproduce the issue
3. Screenshots (if applicable)
4. Browser and operating system information
5. Competition name and date (if relevant)

### Tips for Success

✓ **Export all required PDFs from FSM before starting**
✓ **Use descriptive competition names** (include year and location)
✓ **Don't modify PDF filenames** after exporting from FSM
✓ **Test with a small competition first** to familiarize yourself with the workflow
✓ **Download files promptly** after generation
✓ **Keep your browser updated** for best performance

---

## Additional Resources

- **README.md** — Technical documentation and deployment information
- **Figure Skating Manager** — Refer to FSM documentation for export procedures
- **Azure Status** — Check [Azure Status](https://status.azure.com/) if experiencing service issues

---

*Last Updated: February 2026*
