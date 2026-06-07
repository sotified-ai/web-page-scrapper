# Docs Portal Manager

A single entry-point script designed to orchestrate crawl and repair workflows for documentation training portal tools. This script manages execution flow, error handling, re-login retries, and sequential assets/sidebar/dropdown repairs, keeping the underlying utility scripts modular and untouched.

---

## Table of Contents
1. [Overview](#overview)
2. [Prerequisites](#prerequisites)
3. [Installation & Setup](#installation--setup)
4. [Usage & Modes](#usage--modes)
    - [1. Crawl Mode (default)](#1-crawl-mode-default)
    - [2. Repair All](#2-repair-all)
    - [3. Repair Assets](#3-repair-assets)
    - [4. Repair Sidebar](#4-repair-sidebar)
    - [5. Repair Dropdowns](#5-repair-dropdowns)
5. [Command Line Reference](#command-line-reference)
6. [Repository Structure](#repository-structure)

---

## Overview

The Docs Portal Manager coordinates the following tasks:
*   **Authentication & Crawling**: Crawls authenticated portals using the core crawling engine, handles session states, saves credentials, and auto-retries with a clean login session if a crawl fails.
*   **Asset Repairing**: Fixes relative asset paths (images, links) inside saved HTML pages.
*   **Sidebar/Menu Generation**: Rebuilds the search index, main navigation sidebar, and portal table of contents from downloaded HTML pages.
*   **Dropdown/Accordion Fixing**: Modifies saved pages to fix visibility and functionality of interactive elements like accordion menus and dropdowns.

---

## Prerequisites

This manager script requires the following scripts to be present in the same directory (though they are ignored by Git by default if not tracked):
*   `build_training_portal.py`
*   `repair_training_portal_sidebar.py`
*   `repair_dropdown_sections.py`

You also need Python 3.8+ and any dependencies specified by the crawler (such as Playwright, BeautifulSoup, etc.).

---

## Installation & Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/sotified-ai/web-page-scrapper.git
   cd web-page-scrapper
   ```

2. **Add utility scripts**:
   Place the required crawler and repair scripts listed in [Prerequisites](#prerequisites) in the project directory.

3. **Initialize Python Environment**:
   Set up your virtual environment and install your dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   pip install -r requirements.txt # Install crawler-related requirements
   ```

---

## Usage & Modes

The manager is run using Python:

```bash
python portal_manager.py [mode] [options]
```

### 1. Crawl Mode (default)
Runs the crawler on a specified portal URL.
```bash
python portal_manager.py crawl --start-url "https://docs.example.com/..." --output "./output-dir"
```
*   **Session recovery**: If the crawler fails mid-run, the manager automatically clears the stale login state file, initiates a login-only flow, and resumes crawling.

### 2. Repair All
Sequentially runs dropdown repair, asset path repair, and sidebar rebuilding over your saved output directory.
```bash
python portal_manager.py repair-all --output "./output-dir"
```

### 3. Repair Assets
Repairs broken or absolute image and asset paths in the saved HTML pages.
```bash
python portal_manager.py repair-assets --output "./output-dir"
```

### 4. Repair Sidebar
Rebuilds the sidebar menu, table of contents, and search metadata based on the current set of saved pages.
```bash
python portal_manager.py repair-sidebar --output "./output-dir"
```

### 5. Repair Dropdowns
Fixes CSS/JS classes or structures for accordion sections and dropdowns within the saved HTML files.
```bash
python portal_manager.py repair-dropdowns --output "./output-dir"
```

---

## Command Line Reference

| Argument | Description | Default |
| :--- | :--- | :--- |
| `mode` | Action to execute: `crawl`, `repair-assets`, `repair-sidebar`, `repair-dropdowns`, `repair-all` | `crawl` |
| `--start-url` | The URL where the crawl should begin | `defaulturl` |
| `--output` | Target folder for saved files / output | `folder output` |
| `--storage-state` | Path to JSON file storing login state (cookies/session) | `storage_state.json` |
| `--allowed-domain` | Crawler domain restriction | `docs.example.com` |
| `--max-pages` | Upper limit of pages to crawl | `5000` |
| `--show-browser` | Launch browser in non-headless mode | *Disabled* |
| `--headless` | Run browser headlessly | *Disabled* |
| `--login-only` | Run login flow only, then save state and exit | *Disabled* |
| `--fresh` | Ignore existing state and start crawling from scratch | *Disabled* |
| `--refresh-images` | Force re-download images | *Disabled* |
| `--repair-output` | Alternate destination folder for repaired sidebar | *Disabled* |

---

## Repository Structure

To maintain clean version control, this repository follows a strict file tracking policy:
*   `portal_manager.py`: The main entry-point manager script (tracked).
*   `README.md`: Project documentation (tracked).
*   `.gitignore`: Controls untracked utility files, credentials, output directories, and local caches (tracked).
*   **All other files** (crawler utilities, output directories, and storage states) are ignored by git to keep the repository clean.