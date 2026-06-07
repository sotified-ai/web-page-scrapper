#!/usr/bin/env python3
"""
Repair the training portal sidebar/search/index without changing the crawler.

What this script fixes:
- rebuilds menu.js from the local pages folder, not from breadcrumb noise
- rebuilds search.js from the local pages folder
- regenerates index.html
- preserves the existing pages/ and assets/ directories

Why this helps:
The crawler output can be perfectly valid, while the sidebar becomes noisy if it was built
from breadcrumb-like paths such as "You are here:" or ">" strings. This script ignores
that and builds the tree from the actual saved folder structure under pages/.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from bs4 import BeautifulSoup


DEFAULT_ROOT = r"D:\Code-Projects\scrape_portal\training-portal"
DEFAULT_OUTPUT = None  # in-place by default


NOISY_SEGMENTS = {
    "",
    ">",
    "›",
    "you are here:",
    "you are here",
    "breadcrumb",
    "breadcrumbs",
    "home",
    "docs",
    "page",
}

DROP_FOLDER_NAMES = {
    "_assets",
    "assets",
    "css",
    "js",
    "fonts",
    "vendor",
    "bootstrap",
    "jquery",
}


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Training Portal</title>
  <style>
    :root {
      --bg:#f5f7fb;
      --panel:#ffffff;
      --panel2:#eef3ff;
      --text:#1e2430;
      --muted:#6b7484;
      --border:#dbe2f0;
      --accent:#2f4ea2;
      --shadow:0 8px 30px rgba(18,32,73,.08);
    }
    * { box-sizing:border-box; }
    html, body { height:100%; margin:0; }
    body {
      font-family:Segoe UI,Roboto,Arial,sans-serif;
      background:var(--bg);
      color:var(--text);
    }
    .app {
      display:grid;
      grid-template-columns:360px 1fr;
      height:100vh;
    }
    .sidebar {
      border-right:1px solid var(--border);
      background:linear-gradient(180deg,#fff 0%,#f8faff 100%);
      display:flex;
      flex-direction:column;
      min-width:280px;
    }
    .brand {
      padding:16px 16px 10px;
      border-bottom:1px solid var(--border);
    }
    .brand h1 {
      margin:0 0 4px;
      font-size:20px;
      color:var(--accent);
    }
    .brand p {
      margin:0;
      font-size:12px;
      color:var(--muted);
      line-height:1.45;
    }
    .controls {
      padding:12px 16px;
      border-bottom:1px solid var(--border);
      background:#f8faff;
    }
    .search {
      width:100%;
      padding:11px 12px;
      border:1px solid var(--border);
      border-radius:12px;
      outline:none;
      background:#fff;
      font-size:14px;
    }
    .stats {
      display:flex;
      gap:10px;
      margin-top:10px;
      font-size:12px;
      color:var(--muted);
      flex-wrap:wrap;
    }
    .results {
      padding:0 16px 8px;
      max-height:180px;
      overflow:auto;
    }
    .result {
      display:block;
      padding:8px 10px;
      border:1px solid var(--border);
      border-radius:10px;
      background:#fff;
      text-decoration:none;
      color:var(--text);
      margin-bottom:6px;
      font-size:13px;
    }
    .result:hover { background:var(--panel2); }
    .result .r-title { font-weight:700; margin-bottom:2px; }
    .result .r-meta { font-size:11px; color:var(--muted); }
    .tree-wrap {
      overflow:auto;
      padding:10px 12px 20px;
      flex:1;
    }
    details {
      background:rgba(255,255,255,.9);
      border:1px solid var(--border);
      border-radius:10px;
      margin-bottom:6px;
      overflow:hidden;
    }
    details > summary {
      list-style:none;
      cursor:pointer;
      padding:9px 11px;
      font-weight:600;
      color:var(--accent);
    }
    details > summary::-webkit-details-marker { display:none; }
    .page-link {
      display:block;
      text-decoration:none;
      color:var(--text);
      padding:8px 11px;
      border-top:1px solid var(--border);
      font-size:13px;
    }
    .page-link:hover, .page-link.active {
      background:var(--panel2);
    }
    .main {
      display:grid;
      grid-template-rows:auto 1fr;
      min-width:0;
    }
    .topbar {
      background:linear-gradient(90deg,#253b86 0%,#334ea0 100%);
      color:#fff;
      padding:12px 18px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:12px;
    }
    .doc-title {
      font-size:18px;
      font-weight:700;
      margin-bottom:3px;
    }
    .doc-meta {
      font-size:12px;
      opacity:.92;
      white-space:nowrap;
      overflow:hidden;
      text-overflow:ellipsis;
      max-width:920px;
    }
    .btn {
      appearance:none;
      border:1px solid rgba(255,255,255,.45);
      color:#fff;
      background:rgba(255,255,255,.08);
      padding:8px 11px;
      border-radius:10px;
      cursor:pointer;
      text-decoration:none;
      font-size:13px;
      white-space:nowrap;
    }
    .content {
      padding:14px;
      min-width:0;
      overflow:hidden;
    }
    .reader {
      width:100%;
      height:calc(100vh - 120px);
      border:1px solid var(--border);
      border-radius:16px;
      background:#fff;
      box-shadow:var(--shadow);
    }
    @media (max-width:1100px) {
      .app {
        grid-template-columns:1fr;
        grid-template-rows:auto 1fr;
      }
      .sidebar {
        max-height:42vh;
        border-right:0;
        border-bottom:1px solid var(--border);
      }
      .reader { height:58vh; }
      .doc-meta { max-width:60vw; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <h1>Training Portal</h1>
        <p>Offline documentation rebuilt from the saved pages folder.</p>
      </div>
      <div class="controls">
        <input id="search" class="search" type="search" placeholder="Search titles and page text..." />
        <div class="stats">
          <span id="countPages">0 pages</span>
          <span id="countFolders">0 folders</span>
        </div>
      </div>
      <div id="results" class="results"></div>
      <div id="tree" class="tree-wrap"></div>
    </aside>

    <main class="main">
      <div class="topbar">
        <div>
          <div id="docTitle" class="doc-title">Welcome</div>
          <div id="docMeta" class="doc-meta">Select a page from the sidebar.</div>
        </div>
        <div>
          <a id="openFile" class="btn" href="#" target="_blank" rel="noopener">Open page</a>
        </div>
      </div>
      <div class="content">
        <iframe id="reader" class="reader" src="about:blank" title="Document reader"></iframe>
      </div>
    </main>
  </div>

  <script src="menu.js"></script>
  <script src="search.js"></script>
  <script>
(function () {
  const menuData = window.PORTAL_MENU || { name: 'Documentation', children: [], pages: [] };
  const searchData = window.PORTAL_SEARCH || [];

  const reader = document.getElementById('reader');
  const docTitle = document.getElementById('docTitle');
  const docMeta = document.getElementById('docMeta');
  const search = document.getElementById('search');
  const results = document.getElementById('results');
  const tree = document.getElementById('tree');
  const openFile = document.getElementById('openFile');

  document.getElementById('countPages').textContent = searchData.length + ' pages';
  document.getElementById('countFolders').textContent = countFolders(menuData) + ' folders';

  function countFolders(node) {
    let n = (node.children || []).length;
    (node.children || []).forEach(child => { n += countFolders(child); });
    return n;
  }

  function esc(s) {
    return String(s || '').replace(/[&<>"]/g, m => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[m]));
  }

  function setPage(item) {
    if (!item) return;
    reader.src = item.url;
    docTitle.textContent = item.title || item.url;
    const meta = (item.breadcrumb && item.breadcrumb.length)
      ? item.breadcrumb.join(' > ')
      : ((item.navPath && item.navPath.length) ? item.navPath.join(' > ') : item.url);
    docMeta.textContent = meta;
    openFile.href = item.url;

    document.querySelectorAll('.page-link.active').forEach(el => el.classList.remove('active'));
    const active = document.querySelector('.page-link[data-url="' + CSS.escape(item.url) + '"]');
    if (active) active.classList.add('active');
  }

  function match(item, q) {
    if (!q) return false;
    const blob = [
      item.title,
      item.url,
      (item.breadcrumb || []).join(' '),
      (item.navPath || []).join(' '),
      item.text || ''
    ].join(' ').toLowerCase();
    return blob.includes(q);
  }

  function renderResults(items) {
    if (!items.length) {
      results.innerHTML = '';
      return;
    }

    results.innerHTML = items.slice(0, 40).map(item => `
      <a class="result" href="#" data-url="${esc(item.url)}">
        <div class="r-title">${esc(item.title)}</div>
        <div class="r-meta">${esc((item.breadcrumb || item.navPath || []).join(' > '))}</div>
      </a>
    `).join('');

    results.querySelectorAll('.result').forEach(a => {
      a.addEventListener('click', e => {
        e.preventDefault();
        const item = searchData.find(x => x.url === a.dataset.url);
        if (item) setPage(item);
      });
    });
  }

  function renderTree(node, container, depth) {
    const children = node.children || [];
    const pages = node.pages || [];

    if (depth === 0) {
      pages.forEach(page => {
        const a = document.createElement('a');
        a.href = page.url;
        a.className = 'page-link';
        a.dataset.url = page.url;
        a.textContent = page.title;
        a.addEventListener('click', e => {
          e.preventDefault();
          const item = searchData.find(x => x.url === page.url);
          if (item) setPage(item);
        });
        container.appendChild(a);
      });

      children.forEach(child => renderTree(child, container, depth + 1));
      return;
    }

    const detail = document.createElement('details');
    detail.open = depth <= 2;

    const summary = document.createElement('summary');
    summary.textContent = node.name;
    detail.appendChild(summary);

    pages.forEach(page => {
      const a = document.createElement('a');
      a.href = page.url;
      a.className = 'page-link';
      a.dataset.url = page.url;
      a.textContent = page.title;
      a.addEventListener('click', e => {
        e.preventDefault();
        const item = searchData.find(x => x.url === page.url);
        if (item) setPage(item);
      });
      detail.appendChild(a);
    });

    children.forEach(child => renderTree(child, detail, depth + 1));
    container.appendChild(detail);
  }

  search.addEventListener('input', () => {
    const q = search.value.trim().toLowerCase();
    renderResults(q ? searchData.filter(item => match(item, q)) : []);
  });

  renderTree(menuData, tree, 0);
  if (searchData.length) setPage(searchData[0]);
})();
  </script>
</body>
</html>
"""


@dataclass
class PageRecord:
    title: str
    url: str
    local_path: str
    nav_path: List[str]
    breadcrumb: List[str]
    text: str


def log(msg: str) -> None:
    print(msg, flush=True)


def normalize_segment(seg: str) -> str:
    seg = (seg or "").strip()
    if not seg:
        return ""
    cleaned = seg.replace("_", " ").replace("-", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    low = cleaned.lower()
    if low in NOISY_SEGMENTS or low in DROP_FOLDER_NAMES:
        return ""
    if low in {"you are here:", "you are here"}:
        return ""
    if cleaned in {">", "›"}:
        return ""
    return cleaned


def collapse_repeats(parts: Iterable[str]) -> List[str]:
    out: List[str] = []
    for p in parts:
        p = normalize_segment(p)
        if not p:
            continue
        if out and out[-1].lower() == p.lower():
            continue
        out.append(p)
    return out


def guess_title(html_text: str, fallback: str) -> str:
    soup = BeautifulSoup(html_text, "html.parser")
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)
        title = re.sub(r"\s+", " ", title).strip()
        return title
    h1 = soup.find("h1")
    if h1 and h1.get_text(strip=True):
        title = h1.get_text(" ", strip=True)
        return re.sub(r"\s+", " ", title).strip()
    return fallback


def extract_breadcrumb(soup: BeautifulSoup) -> List[str]:
    crumbs: List[str] = []
    for selector in (".breadcrumb a", ".breadcrumb span", ".breadcrumbs a", ".breadcrumbs span", "nav.breadcrumb a", "nav.breadcrumb span"):
        for el in soup.select(selector):
            text = normalize_segment(el.get_text(" ", strip=True))
            if text:
                crumbs.append(text)
    return collapse_repeats(crumbs)


def find_main(soup: BeautifulSoup):
    for selector in ("article", "main", ".topic-content", ".content", "#content", ".page-content", ".body-content", ".main-content"):
        node = soup.select_one(selector)
        if node:
            return node
    return soup.body or soup


def page_text(soup: BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def record_from_html(html_path: Path, pages_root: Path) -> Optional[PageRecord]:
    try:
        raw = html_path.read_text(encoding="utf-8")
    except Exception:
        return None
    if len(raw.strip()) < 50:
        return None

    soup = BeautifulSoup(raw, "html.parser")
    title = guess_title(raw, html_path.stem)
    breadcrumb = extract_breadcrumb(soup)
    main = find_main(soup)
    text = page_text(BeautifulSoup(str(main), "html.parser"))[:15000]

    rel = html_path.relative_to(pages_root)
    local_url = Path("pages") / rel
    nav_path = collapse_repeats(rel.parent.parts)

    return PageRecord(
        title=title,
        url=local_url.as_posix(),
        local_path=local_url.as_posix(),
        nav_path=nav_path,
        breadcrumb=breadcrumb if breadcrumb else nav_path,
        text=text,
    )


def build_records(pages_root: Path) -> List[PageRecord]:
    html_files = sorted(pages_root.rglob("*.html"))
    records: List[PageRecord] = []
    seen = set()
    for html_file in html_files:
        rec = record_from_html(html_file, pages_root)
        if not rec:
            continue
        if rec.url in seen:
            continue
        seen.add(rec.url)
        records.append(rec)
    return records


def ensure_child(node: Dict, name: str) -> Dict:
    children = node.setdefault("children", {})
    if name not in children:
        children[name] = {"name": name, "children": {}, "pages": []}
    return children[name]


def build_menu_tree(records: List[PageRecord]) -> Dict:
    root: Dict = {"name": "Documentation", "children": {}, "pages": []}
    for rec in records:
        node = root
        for part in rec.nav_path:
            node = ensure_child(node, part)
        node.setdefault("pages", []).append(
            {
                "title": rec.title,
                "url": rec.url,
                "breadcrumb": rec.breadcrumb,
            }
        )
    return root


def sort_tree(node: Dict) -> Dict:
    children = node.get("children", {})
    node["children"] = {
        k: sort_tree(v) for k, v in sorted(children.items(), key=lambda kv: kv[0].lower())
    }
    node["pages"] = sorted(node.get("pages", []), key=lambda p: p["title"].lower())
    return node


def to_menu_json(node: Dict) -> Dict:
    return {
        "name": node["name"],
        "children": [to_menu_json(child) for child in node.get("children", {}).values()],
        "pages": node.get("pages", []),
    }


def build_search_index(records: List[PageRecord]) -> List[Dict]:
    return [
        {
            "title": r.title,
            "url": r.url,
            "breadcrumb": r.breadcrumb,
            "navPath": r.nav_path,
            "text": r.text,
        }
        for r in records
    ]


def write_json_js(path: Path, var_name: str, data) -> None:
    path.write_text(
        f"window.{var_name} = {json.dumps(data, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )


def rebuild_portal(root: Path) -> int:
    pages_root = root / "pages"
    if not pages_root.exists():
        log(f"ERROR: pages folder not found: {pages_root}")
        return 1

    records = build_records(pages_root)
    if not records:
        log("ERROR: no pages found to rebuild portal from.")
        return 1

    tree = sort_tree(build_menu_tree(records))
    menu_data = to_menu_json(tree)
    search_data = build_search_index(records)

    write_json_js(root / "menu.js", "PORTAL_MENU", menu_data)
    write_json_js(root / "search.js", "PORTAL_SEARCH", search_data)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")

    log(f"Rebuilt portal from {len(records)} pages")
    log(f"Wrote: {root / 'menu.js'}")
    log(f"Wrote: {root / 'search.js'}")
    log(f"Wrote: {root / 'index.html'}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Rebuild a clean training portal sidebar from saved pages.")
    p.add_argument("--root", default=DEFAULT_ROOT, help="Existing training portal root containing pages/ and assets/")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().resolve()
    log(f"Portal root: {root}")
    return rebuild_portal(root)


if __name__ == "__main__":
    raise SystemExit(main())
