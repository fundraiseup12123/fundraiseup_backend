# Platform "All Campaigns" Tab - Quick User Guide

> **Target Audience:** Usman's Platform Operations & Campaign Review Team  
> **Route:** `/super-admin/campaigns`  
> **Location:** Platform Admin Console → **All Campaigns**

---

## 1. How to Access the Tab

1. Log in to the Admin Dashboard.
2. In the top-left header, click the **Platform Switcher** dropdown and select **"Platform Admin"**.
3. In the left navigation sidebar, click **"All Campaigns"** (under the Platform section).
4. *Direct URL:* `http://localhost:3000/super-admin/campaigns` (or your production domain).

> **Note on Permissions:**  
> By default, Super Admins and Platform Admins have full access. Super Admins can also grant access to Organization Admins and Team Members via the **"Manage Role Access"** button in the top right.

---

## 2. Key Features Overview

```
┌────────────────────────────────────────────────────────────────────────┐
│  Summary Metric Cards: Organizations | Campaigns | Active | Notes     │
├────────────────────────────────────────────────────────────────────────┤
│  Filters: [ 🔍 Search ] [ Org Filter ] [ Status Filter ] [ Sort By ]   │
├────────────────────────────────────────────────────────────────────────┤
│  ▼ Organization A (3 campaigns)                                        │
│     ├── Campaign 1  [Live]  [Landing View ↗]  [Popup View 🗖]  [Notes] │
│     └── Campaign 2  [Draft] [Landing View ↗]  [Popup View 🗖]  [Notes] │
└────────────────────────────────────────────────────────────────────────┘
```

### A. Directory & Organization Tree
- View every organization side-by-side with its campaigns.
- Each campaign displays: **Title**, **Slug**, **Created Date**, **Default Currency**, **Designation**, and **Live Status** (`Live` or `Draft`).
- Use **"Expand All"** or **"Collapse All"** to quickly navigate large numbers of campaigns.

### B. Live Previews & Direct Links
Each campaign has two preview modes:
1. **🌐 Landing Full View**:
   - Click the **eye icon** or **"Landing View"** to launch an in-app interactive preview.
   - Click **↗** to open the public landing page in a new browser tab.
   - Click **Copy** to copy the public landing page URL to your clipboard.
2. **🗖 Popup View**:
   - Click **"Popup View"** to preview the embedded modal donation widget.
   - Click **↗** to test the live standalone popup URL (`/c/[slug]/pop-up`).
   - Click **Copy** to copy the popup URL.

---

## 3. Shared Campaign Notes (Team Collaboration)

Anyone with access to the tab can review and post shared notes on any campaign. All notes are synchronized and visible across the team in real time.

### Opening the Notes Drawer
- Click the **"📝 Notes (x)"** button on any campaign row.
- The drawer slides open from the right, showing note history, author name, role badge, and creation timestamp.

### Using the Note Composer
The text composer at the bottom of the drawer includes:

- **1-Click Category Tags**:
  - `📌 Pinned`: Pin critical information or key notices.
  - `⚠️ Action`: Flag issues requiring immediate attention or follow-up.
  - `✅ Reviewed`: Mark that a campaign has passed QA/review.
  - `💡 Idea`: Suggest optimizations (copy, images, donation tiers).
  - `📣 Update`: Post general campaign updates or milestone logs.
- **Markdown Quick Formatting**:
  - `B` for **Bold**, `I` for *Italic*, `•` for Bullet list, `</>` for Code, `“ ”` for Quotes.
  - Highlight any text and click a format button to wrap it instantly.
- **Auto-Resizing & Draft Persistence**:
  - The text box expands naturally up to 240px as you type.
  - **Auto-Save Draft:** If you accidentally close the drawer or switch tabs, your unsent text is automatically saved and restored when you reopen the campaign.
- **Submitting & Shortcuts**:
  - Press <kbd>Ctrl + Enter</kbd> (or <kbd>Cmd + Enter</kbd> on Mac) to post immediately.
  - Or click the **"Post Note ➤"** button.
  - To discard a draft, click **"✕ Clear"**.

### Deleting a Note
- Click the **🗑** icon next to any note you authored (Super Admins can delete any note).
- An in-app confirmation modal appears displaying the note excerpt before deleting. Click **"Yes, Delete"** to confirm.

---

## 4. Searching & Filtering

| Tool | Purpose |
| :--- | :--- |
| **Search Box** | Instant search across campaign titles, internal names, and URL slugs. |
| **Org Dropdown** | Narrow down to a specific organization or view all. |
| **Status Dropdown** | Filter by **Active / Live**, **Draft**, or **Archived**. |
| **Sort Dropdown** | Sort campaigns by **Newest First**, **Oldest First**, **Title (A–Z)**, or **Most Notes**. |

---

## 5. Super Admin Controls: Role Access Management

Super Admins can configure who can view this tab:
1. Click **"Manage Role Access"** in the top right of the page.
2. Check or uncheck allowed roles (`Platform Admin`, `Organization Admin`, `Team Member`).
3. Click **"Save Role Permissions"**. Changes take effect immediately.

---

*Need help or feature additions? Contact the Platform Engineering Team.*
