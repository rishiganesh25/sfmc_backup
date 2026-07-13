# SFMCVault

SFMCVault is a version-control framework for Salesforce Marketing Cloud (SFMC). It automatically backs up your emails, templates, content blocks, images, Data Extensions, Automations, automation activities, and Journeys from SFMC into a Git repository, giving you a full audit trail of every change -- who changed what, when, and exactly what was different.

**Why SFMCVault?**

SFMC has no built-in version history for Content Builder assets. If someone edits an email or deletes a content block, that change is gone forever. SFMCVault solves this by syncing every asset into Git-tracked files with:

- Full HTML content (including AMPscript and dynamic content)
- Human-readable changelogs with inline diffs
- Metadata manifests (subject lines, preheaders, modified dates, who made the change)
- Automatic daily syncs via GitHub Actions

---

## What Gets Backed Up

SFMCVault backs up eight categories of SFMC assets, spanning Content Builder, Data Extensions, Automation Studio, and Journey Builder:

| Category | What's Captured | Format | Output Directory |
|---|---|---|---|
| **Emails** | Template-based, HTML paste, and text-only emails. Template-based emails are compiled from their template skeleton + slot content into the full HTML. Subject line and preheader are saved as a header comment. | `.html` / `.txt` | `email-content/emails/` |
| **Templates** | Email templates (layouts) | `.html` | `email-content/templates/` |
| **Content Blocks** | Freeform, Text, HTML, Text + Image, Image, Styling, Einstein Content, Code Snippet, Live Setting, Interactive Email Form, and Interactive CloudPage blocks — plus a few related Content Builder asset types that share the same query: web pages, web templates, default templates, JSON messages, Journey Builder templates, and package definitions.<sup>*</sup> | `.html` / `.txt` / `.json` | `email-content/content-blocks/` |
| **Images** | PNG, JPEG, GIF, WebP, SVG, BMP, TIFF, and PDF assets (downloaded as binary files, with SHA-256 change detection) | binary | `email-content/images/` |
| **Data Extensions** | Data Extension definitions and full field schemas (name, field type, length, primary key, required, default value, ordinal) — retrieved via the SOAP API | `.json` | `email-content/data-extensions/` |
| **Automations** | Automation definitions including status, schedule, and ordered steps | `.json` | `email-content/automations/` |
| **Automation Activities** | SQL Query, SSJS Script, Import, Data Extract, and File Transfer activities. Query SQL and SSJS code are also written out as standalone `.sql` / `.ssjs` files for readable diffs. | `.json` (+ `.sql` / `.ssjs`) | `email-content/automation-activities/` |
| **Journeys** | Journey Builder interactions with their full canvas — activities, triggers, goals, version, and status | `.json` | `email-content/journeys/` |

Each category also gets a `manifest.json` with metadata for every asset.

<sup>*</sup> Content Blocks are matched by SFMC asset type ID: `195, 196, 197, 198, 199, 202, 203, 205, 206, 214, 220, 227, 230, 231, 232, 233, 236`. Any asset with one of these types is stored in `content-blocks/`. Note that this does **not** include Button, Dynamic Content, Smart Capture, Social Share/Follow, Reference, Image Carousel, Custom, or A/B Test blocks — those asset types are not currently queried by the sync script.

---

## Prerequisites

- **Python 3.10+**
- **Git**
- **A GitHub account** (or any Git hosting provider)
- **SFMC API credentials** (Installed Package with Server-to-Server integration)

---

## Setup Guide

### Step 1: Create SFMC API Credentials

1. Log in to SFMC and navigate to **Setup > Apps > Installed Packages**
2. Click **New** to create a new package (e.g., "SFMCVault Backup")
3. Click **Add Component** and select **API Integration**
4. Choose **Server-to-Server** as the integration type
5. Grant the following **Read** permissions so every category can be backed up:
   - **Assets** — Read (under Content Builder) — emails, templates, content blocks, images
   - **Automations** — Read — automations and automation activities
   - **Journeys** — Read — Journey Builder interactions
   - **Data Extensions** — Read — Data Extension definitions (SOAP API)
   - **List and Subscribers** — Read — Data Extension field definitions

   > If you only grant **Assets**, SFMCVault still works but will skip Data Extensions, Automations, Automation Activities, and Journeys.
6. Save the package and note down these three values:
   - **Client ID**
   - **Client Secret**
   - **Subdomain** (the `XXXXX` part from your SFMC URL: `https://XXXXX.auth.marketingcloudapis.com`)

### Step 2: Fork the Repository

Fork this repo and create a private repo

### Step 3: Set Up Automated Daily Sync (GitHub Actions)

The repository includes a GitHub Actions workflow at `.github/workflows/sync-sfmc-emails.yml` that runs the sync automatically every day at midnight UTC.

To enable it:

1. Go to **Settings > Secrets and variables > Actions** in your GitHub repo
2. Add these three **repository secrets**:

   | Secret Name | Value |
   |---|---|
   | `SFMC_CLIENT_ID` | Your SFMC Client ID |
   | `SFMC_CLIENT_SECRET` | Your SFMC Client Secret |
   | `SFMC_SUBDOMAIN` | Your SFMC subdomain |

3. Go to the **Actions** tab and enable the workflow if prompted
4. Optionally, click **Run workflow** to trigger it manually

From now on, the workflow will:
- Run `scripts/sync.py` daily
- Commit any changes to `email-content/` with a detailed commit message
- Skip the commit if nothing has changed

---

## Output Structure

After the first sync, your repository will look like this:

```
email-content/
├── emails/
│   ├── 12345_Welcome_Email.html
│   ├── 67890_Promo_Campaign.html
│   └── manifest.json
├── templates/
│   ├── 11111_Main_Template.html
│   └── manifest.json
├── content-blocks/
│   ├── 22222_Header_Block.html
│   ├── 33333_Footer_Block.html
│   └── manifest.json
├── images/
│   ├── 44444_Logo.png
│   ├── 55555_Banner.jpg
│   └── manifest.json
├── data-extensions/
│   ├── abc-def-123_Master_Subscribers.json
│   └── manifest.json
├── automations/
│   ├── 78901_Nightly_Import.json
│   └── manifest.json
├── automation-activities/
│   ├── queries/
│   │   ├── 55501_Active_Subscribers.json
│   │   ├── 55501_Active_Subscribers.sql
│   │   └── manifest.json
│   ├── scripts/
│   │   ├── 66601_Cleanup_Script.json
│   │   ├── 66601_Cleanup_Script.ssjs
│   │   └── manifest.json
│   ├── imports/
│   ├── data-extracts/
│   ├── file-transfers/
│   └── manifest.json
├── journeys/
│   ├── 90123_Onboarding_Journey.json
│   └── manifest.json
├── CHANGELOG.md
└── .commit-summary
```

### File Naming Convention

Each file is named `{sfmc_asset_id}_{sanitized_name}.{ext}`, e.g., `139009_HK_Autocumulus_Main_Message.html`. The SFMC asset ID prefix ensures filenames are unique even if two assets have the same name.

### Manifest Files

Each `manifest.json` contains metadata for every asset in that category:

```json
{
  "file": "12345_Welcome_Email.html",
  "id": 12345,
  "name": "Welcome Email",
  "customerKey": "abc-def-123",
  "assetType": "templatebasedemail",
  "status": "Active",
  "category": "Onboarding",
  "subject": "Welcome to our platform!",
  "preheader": "Get started today",
  "createdDate": "2025-01-15T10:30:00Z",
  "modifiedDate": "2026-03-20T14:45:00Z",
  "modifiedBy": "Jane Smith",
  "contentSource": "views.html.content+slots",
  "templateId": 11111,
  "templateName": "Main Template"
}
```

### CHANGELOG.md

A running history of every sync, showing what was added, modified, or deleted -- with inline diffs for modified assets:

```markdown
## 2026-04-15T12:00:00Z

### Added (1)
- `emails/99999_New_Campaign.html` -- "New Campaign" (modified in SFMC by Jane on 2026-04-14)

### Modified (2)
- `emails/12345_Welcome_Email.html` -- "Welcome Email" (modified in SFMC by John on 2026-04-15)

### Unchanged: 48 asset(s)
```

---

## Reviewing Changes

### In GitHub UI

- **Commit history** — click the "commits" link on the repo page to see all syncs
- **File history** — open any file and click **History** to see every change to that specific email
- **Blame view** — open any file and click **Blame** to see who changed each line
- **CHANGELOG.md** — browse to `email-content/CHANGELOG.md` for a human-readable summary

### Via Git CLI

```bash
# View all sync commits
git log --oneline

# See what changed in a specific commit
git show <commit-hash>

# View full change history for one email
git log -p -- email-content/emails/12345_Welcome_Email.html

# Compare two points in time
git diff <old-hash> <new-hash> -- email-content/
```

---

## Notes

1. **SFMC API rate limits** — The script fetches each email, template, and content block individually (not just via bulk query) to ensure complete HTML content. For large SFMC accounts with thousands of assets, this may take several minutes and could approach API rate limits. If you hit rate limit errors, consider increasing the timeout values in the script or running the sync during off-peak hours.

2. **Template-based emails** — SFMC stores template-based emails (type 207) as a template skeleton with separate slot content. SFMCVault automatically compiles these together to produce the full HTML you see in the SFMC UI. The `contentSource` field in the manifest tells you how the content was extracted (`views.html.content+slots` means it was compiled from template + slots).

3. **Image storage** — Images are stored as binary files. Git is not ideal for tracking binary file changes over time (the repo size will grow). If you have a very large image library, consider adding `email-content/images/` to `.gitignore` or using [Git LFS](https://git-lfs.github.com/).

4. **AMPscript and personalization** — SFMCVault saves the raw source HTML with AMPscript tags intact (e.g., `%%=v(@variable)=%%`). It does NOT render or resolve personalization — you get the exact source code, which is what you want for version control.

5. **Credentials security** — Never commit your SFMC credentials to the repository. Always use environment variables locally or GitHub Secrets for the automated workflow. The Client Secret provides full API access to your SFMC account.

6. **Multiple Business Units** — The API credentials are scoped to a single SFMC Business Unit. If you need to back up multiple BUs, create separate installed packages for each and run the sync with different credentials (you can use separate branches or repos).

7. **Deleted assets** — When an asset is deleted from SFMC, the script removes the corresponding file and records the deletion (with the last known content) in the CHANGELOG. The deleted content remains in Git history permanently.

8. **First run** — The first sync will show all assets as "added". Subsequent runs will correctly detect added, modified, deleted, and unchanged assets.

---

## License

MIT
