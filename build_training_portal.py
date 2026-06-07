#!/usr/bin/env python3
"""
Authenticated documentation docs crawler + offline training portal builder.

Flow:
1) Open the start URL in a browser and log in manually once.
2) Save Playwright storage state to storage_state.json.
3) Expand the sidebar tree and collect sidebar page URLs only.
4) Scrape each page: extract main content, download images, rewrite links.
5) Generate training-portal/ with index.html, menu.js, search.js, pages/, assets/.

The portal works offline via file:// (no fetch for JSON).
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import html
import os
import json
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, unquote

import requests
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

DEFAULT_START_URL = (
    "https://docs.example.com/docs/Welcome.html#"
)
DEFAULT_DOMAIN = "docs.example.com"
DEFAULT_OUTPUT = "training-portal"
DEFAULT_STORAGE = "storage_state.json"
DEFAULT_MAX_PAGES = 5000
CHECKPOINT_NAME = "crawl_checkpoint.json"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

SKIP_URL_PARTS = ("/logout", "/signout", "/login")
LOGIN_URL_PARTS = ("/login", "/signin", "/sign-in", "/auth", "/sso")
LOGIN_TEXT_HINTS = ("sign in", "log in", "login to", "username", "user name")
AUTH_SUCCESS_HINTS = ("logout", "sign out")
DOC_PAGE_HINTS = (
    "sidenav",
    "off-canvas-list",
    "topic-content",
    "tree-node",
    "submenu-toggle",
    "navigation-wrapper",
)
HTML_EXTS = (".html", ".htm")

PAGE_STYLES = """
:root { --text:#1e2430; --muted:#6b7484; --border:#dbe2f0; --accent:#2f4ea2; }
* { box-sizing:border-box; }
body {
  margin:0; padding:28px 32px 48px;
  font-family:Segoe UI,Roboto,Arial,sans-serif;
  font-size:15px; line-height:1.65; color:var(--text);
  background:#fff; max-width:1100px;
}
.breadcrumb { font-size:13px; color:var(--muted); margin-bottom:18px; }
.breadcrumb span + span::before { content:" › "; color:#aab4c8; }
.doc-title { font-size:28px; line-height:1.25; margin:0 0 20px; color:var(--accent); }
.doc-content h1,.doc-content h2,.doc-content h3,.doc-content h4 { color:#253b86; margin-top:1.4em; }
.doc-content table { border-collapse:collapse; width:100%; margin:16px 0; font-size:14px; }
.doc-content th,.doc-content td { border:1px solid var(--border); padding:8px 10px; vertical-align:top; }
.doc-content th { background:#eef3ff; }
.doc-content img { max-width:100%; height:auto; }
.doc-content pre,.doc-content code { font-family:Consolas,Monaco,monospace; font-size:13px; }
.doc-content pre { background:#f5f7fb; border:1px solid var(--border); border-radius:8px; padding:12px; overflow:auto; }
.doc-content ul,.doc-content ol { padding-left:1.4em; }
.doc-content a { color:var(--accent); }
.doc-content blockquote { border-left:4px solid #c9d6f5; margin:16px 0; padding:8px 16px; color:#4a5568; background:#f8faff; }
"""

EXPAND_SIDEBAR_JS = """
() => {
    let count = 0;
    const sidebar = document.querySelector('ul.sidenav') ||
                    document.querySelector('.sidenav') ||
                    document.querySelector('ul.off-canvas-list') ||
                    document.querySelector('.off-canvas-list') ||
                    document.querySelector('.navigation-wrapper') ||
                    document;
    const toggles = sidebar.querySelectorAll('.submenu-toggle-container');
    for (const toggle of toggles) {
        try {
            if (toggle.getAttribute('aria-expanded') === 'true') continue;
            toggle.scrollIntoView({ behavior: 'instant', block: 'center' });
            for (const type of ['mousedown', 'mouseup', 'click']) {
                toggle.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
            }
            count++;
        } catch (e) {}
    }
    return count;
}
"""

SCROLL_SIDEBAR_JS = """
async () => {
    const sidebar = document.querySelector('ul.sidenav') ||
                    document.querySelector('.sidenav') ||
                    document.querySelector('nav') ||
                    document.body;
    let last = -1;
    for (let i = 0; i < 20; i++) {
        sidebar.scrollTo(0, sidebar.scrollHeight);
        await new Promise(r => setTimeout(r, 400));
        if (sidebar.scrollHeight === last) break;
        last = sidebar.scrollHeight;
    }
    sidebar.scrollTo(0, 0);
}
"""

EXTRACT_SIDEBAR_JS = """
() => {
    const sidebar = document.querySelector('ul.sidenav') ||
                    document.querySelector('.sidenav') ||
                    document.querySelector('ul.off-canvas-list') ||
                    document.querySelector('.off-canvas-list') ||
                    document.querySelector('.navigation-wrapper') ||
                    document.querySelector('nav');
    if (!sidebar) return [];

    function cleanUrl(href) {
        let u = href.split('#')[0].split('?')[0];
        if (u.endsWith('/')) u = u.slice(0, -1);
        return u;
    }

    function navPathForLink(link) {
        const parts = [];
        let node = link.closest('li.tree-node') || link.closest('li');
        while (node) {
            const parentUl = node.parentElement;
            if (parentUl && parentUl.tagName === 'UL') {
                const folderEl = parentUl.previousElementSibling;
                if (folderEl) {
                    const text = (folderEl.innerText || folderEl.textContent || '')
                        .replace(/[\\r\\n\\t]+/g, ' ').trim().split('  ')[0];
                    if (text) parts.unshift(text);
                }
            }
            node = parentUl ? parentUl.closest('li.tree-node') || parentUl.closest('li') : null;
        }
        return parts;
    }

    const seen = new Set();
    const out = [];
    for (const a of sidebar.querySelectorAll('a[href]')) {
        const href = a.href || '';
        if (!href || href.startsWith('javascript:') || href.startsWith('mailto:') || href.startsWith('tel:')) continue;
        const url = cleanUrl(href);
        if (!url || seen.has(url)) continue;
        const title = (a.innerText || a.textContent || '').replace(/\\s+/g, ' ').trim();
        if (!title) continue;
        seen.add(url);
        out.push({ url, title, navPath: navPathForLink(a) });
    }
    return out;
}
"""


@dataclass
class SidebarItem:
    url: str
    title: str
    nav_path: List[str]


@dataclass
class PageRecord:
    url: str
    title: str
    local_path: str
    breadcrumb: List[str]
    nav_path: List[str]
    text: str


class AuthenticationRequired(RuntimeError):
    pass


def sidebar_item_to_dict(item: SidebarItem) -> Dict:
    return {"url": item.url, "title": item.title, "nav_path": item.nav_path}


def sidebar_item_from_dict(data: Dict) -> SidebarItem:
    return SidebarItem(
        url=data["url"],
        title=data["title"],
        nav_path=list(data.get("nav_path") or []),
    )


def record_to_dict(record: PageRecord) -> Dict:
    return asdict(record)


def record_from_dict(data: Dict) -> PageRecord:
    return PageRecord(**data)


def checkpoint_path(out_root: Path) -> Path:
    return out_root / CHECKPOINT_NAME


def save_checkpoint(
    out_root: Path,
    start_url: str,
    prefix: str,
    sidebar_items: List[SidebarItem],
    records: List[PageRecord],
) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    payload = {
        "start_url": start_url,
        "prefix": prefix,
        "sidebar_items": [sidebar_item_to_dict(i) for i in sidebar_items],
        "records": [record_to_dict(r) for r in records],
    }
    checkpoint_path(out_root).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_checkpoint(out_root: Path, start_url: str, prefix: str) -> Tuple[Optional[List[SidebarItem]], List[PageRecord]]:
    path = checkpoint_path(out_root)
    if not path.exists():
        return None, []

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, []

    if data.get("start_url") != start_url or data.get("prefix") != prefix:
        return None, []

    sidebar_items = [sidebar_item_from_dict(i) for i in data.get("sidebar_items") or []]
    records = [record_from_dict(r) for r in data.get("records") or []]
    if not sidebar_items:
        return None, records
    return sidebar_items, records


def clear_checkpoint(out_root: Path) -> None:
    path = checkpoint_path(out_root)
    if path.exists():
        path.unlink()


def recover_record_from_file(item: SidebarItem, prefix: str, out_root: Path) -> Optional[PageRecord]:
    pages_root = out_root / "pages"
    local_rel = url_to_local_page(item.url, prefix)
    out_file = pages_root / local_rel
    if not out_file.exists() or out_file.stat().st_size <= 50:
        return None
    try:
        html = out_file.read_text(encoding="utf-8")
        soup = BeautifulSoup(html, "html.parser")
        title = guess_title_from_html(html, item.title)
        breadcrumb = breadcrumb_from_soup(soup) or item.nav_path
        text = page_text(soup)[:12000]
        return PageRecord(
            url=item.url,
            title=title,
            local_path=f"pages/{local_rel}",
            breadcrumb=breadcrumb,
            nav_path=item.nav_path,
            text=text,
        )
    except Exception:
        return None


def merge_records_from_disk(
    sidebar_items: List[SidebarItem],
    prefix: str,
    out_root: Path,
    records: List[PageRecord],
) -> List[PageRecord]:
    by_url = {r.url: r for r in records}
    for item in sidebar_items:
        if item.url in by_url:
            continue
        recovered = recover_record_from_file(item, prefix, out_root)
        if recovered:
            by_url[item.url] = recovered
    ordered = []
    seen: Set[str] = set()
    for item in sidebar_items:
        if item.url in by_url and item.url not in seen:
            ordered.append(by_url[item.url])
            seen.add(item.url)
    return ordered


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def log_phase(step: int, total: int, name: str) -> None:
    print()
    print("=" * 80, flush=True)
    log(f"STEP {step}/{total}: {name}")
    print("=" * 80, flush=True)


def normalize_url(url: str) -> str:
    p = urlparse(url)
    return urlunparse((p.scheme.lower(), p.netloc.lower(), p.path, "", "", ""))


def safe_segment(text: str, limit: int = 80) -> str:
    text = unquote(text or "").strip()
    text = re.sub(r"[\x00-\x1f<>:\"/\\|?*]+", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if not text:
        text = "page"
    if len(text) > limit:
        digest = hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()[:8]
        text = text[: max(20, limit - 10)].rstrip() + "_" + digest
    return text


def path_parts(path: str) -> List[str]:
    return [p for p in path.split("/") if p]


def derive_allowed_prefix(start_url: str) -> str:
    p = urlparse(start_url)
    path = p.path
    if not path.endswith("/"):
        path = path.rsplit("/", 1)[0] + "/"
    return path


def is_doc_url(url: str, domain: str, prefix: str) -> bool:
    try:
        p = urlparse(url)
        if p.netloc.lower() != domain.lower():
            return False
        if not p.path.startswith(prefix):
            return False
        if any(x in p.path.lower() for x in SKIP_URL_PARTS):
            return False
        return p.path.lower().endswith(HTML_EXTS)
    except Exception:
        return False


def url_to_local_page(url: str, prefix: str) -> str:
    p = urlparse(url)
    rel = p.path[len(prefix) :].lstrip("/") if p.path.startswith(prefix) else p.path.lstrip("/")
    segs = [safe_segment(x) for x in path_parts(rel)]
    if not segs:
        segs = ["index.html"]
    elif not segs[-1].lower().endswith(HTML_EXTS):
        segs[-1] = segs[-1] + ".html"
    return str(Path(*segs))


def looks_like_login_page(html: str, title: str = "", url: str = "") -> bool:
    if looks_like_docs_page(html, title):
        return False
    url_lower = (url or "").lower()
    if any(part in url_lower for part in LOGIN_URL_PARTS):
        return True
    # Real doc pages under /docs/.../*.html are not login screens unless redirected.
    parsed = urlparse(url_lower)
    if parsed.path.endswith(".html") and "/docs/" in parsed.path:
        if not any(part in url_lower for part in LOGIN_URL_PARTS):
            return False
    blob = f"{title}\n{html[:8000]}".lower()
    if any(h in blob for h in LOGIN_TEXT_HINTS):
        return True
    if ('type="password"' in blob or "type='password'" in blob) and "sidenav" not in blob:
        return True
    return False


def looks_authenticated_page(html: str, title: str = "", url: str = "") -> bool:
    if looks_like_login_page(html, title, url):
        return False
    blob = f"{title}\n{html[:24000]}".lower()
    if any(h in blob for h in AUTH_SUCCESS_HINTS):
        return True
    return looks_like_docs_page(html, title)


def looks_like_docs_page(html: str, title: str = "") -> bool:
    blob = f"{title}\n{html[:48000]}".lower()
    return any(h in blob for h in DOC_PAGE_HINTS)


def guess_title_from_html(html: str, fallback: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    if soup.title and soup.title.get_text(strip=True):
        title = soup.title.get_text(" ", strip=True)
    else:
        h1 = soup.find("h1")
        title = h1.get_text(" ", strip=True) if h1 else fallback
    return re.sub(r"\s+", " ", title).strip()[:200]


def breadcrumb_from_soup(soup: BeautifulSoup) -> List[str]:
    found: List[str] = []
    for sel in (
        ".breadcrumb a",
        ".breadcrumb span",
        "nav.breadcrumb a",
        "nav.breadcrumb span",
        ".breadcrumbs a",
        ".breadcrumbs span",
    ):
        for n in soup.select(sel):
            text = n.get_text(" ", strip=True)
            if text:
                found.append(text)
    cleaned: List[str] = []
    for item in found:
        if not cleaned or cleaned[-1] != item:
            cleaned.append(item)
    return cleaned[:12]


def find_main_container(soup: BeautifulSoup):
    for selector in (
        "main",
        "article",
        ".topic-content",
        ".content",
        "#content",
        ".page-content",
        ".body-content",
        ".main-content",
    ):
        node = soup.select_one(selector)
        if node:
            return node
    return soup.body or soup


def extract_main_html(raw_html: str) -> Tuple[str, BeautifulSoup]:
    soup = BeautifulSoup(raw_html, "html.parser")
    main = find_main_container(soup)
    clone = BeautifulSoup(str(main), "html.parser")

    for bad in clone.find_all(
        [
            "script",
            "style",
            "noscript",
            "nav",
            "aside",
            "header",
            "footer",
            "form",
            "button",
            "input",
            "select",
            "textarea",
            "iframe",
            "svg",
            "canvas",
            "object",
            "embed",
        ]
    ):
        bad.decompose()

    for tag in clone.find_all(True):
        if tag.name in {"div", "span"} and not tag.get_text(strip=True) and not tag.find("img"):
            tag.decompose()

    return str(clone), clone


def page_text(soup: BeautifulSoup) -> str:
    return re.sub(r"\s+", " ", soup.get_text(" ", strip=True)).strip()


def cookies_from_storage_state(storage_state_path: Path) -> List[Dict]:
    if not storage_state_path.exists():
        return []
    try:
        data = json.loads(storage_state_path.read_text(encoding="utf-8"))
        return data.get("cookies", []) or []
    except Exception:
        return []


def requests_session(cookies: List[Dict]) -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if name and value:
            session.cookies.set(name, value, domain=c.get("domain") or "", path=c.get("path") or "/")
    return session


KNOWN_IMAGE_EXTS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico", ".bmp"})

MIME_EXT_MAP = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/bmp": ".bmp",
}

CSS_BG_RE = re.compile(r"background-image\s*:\s*url\(['\"]?(.+?)['\"]?\)", re.IGNORECASE)


def local_image_path(abs_url: str, assets_root: Path) -> Path:
    p = urlparse(abs_url)
    host = safe_segment(p.netloc)
    parts = [safe_segment(x) for x in path_parts(p.path)] or ["image"]
    return assets_root / host / Path(*parts)


def _detect_ext_from_url(url: str) -> str:
    p = urlparse(url)
    ext = Path(unquote(p.path)).suffix.lower()
    return ext if ext in KNOWN_IMAGE_EXTS else ""


def _detect_ext_from_content_type(content_type: str) -> str:
    ct = content_type.split(";")[0].strip().lower()
    return MIME_EXT_MAP.get(ct, "")


def download_image(
    session: requests.Session,
    abs_url: str,
    dest: Path,
    stats: dict,
) -> tuple:
    ext = _detect_ext_from_url(abs_url)
    if ext and dest.suffix != ext:
        dest = dest.with_suffix(ext)

    if dest.exists() and dest.stat().st_size > 0:
        stats["downloaded"] += 1
        return dest, True

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        resp = session.get(abs_url, timeout=60)
        if resp.status_code == 404:
            stats["failed"] += 1
            log(f"  Failed image (404): {abs_url}")
            return None, False
        if resp.status_code in (401, 403):
            stats["failed"] += 1
            log(f"  Failed image ({resp.status_code}): {abs_url}")
            return None, False
        if resp.status_code != 200:
            stats["failed"] += 1
            return None, False

        if not dest.suffix:
            ext = _detect_ext_from_url(str(resp.url))
            if not ext:
                ext = _detect_ext_from_content_type(resp.headers.get("Content-Type", ""))
            if ext:
                dest = dest.with_suffix(ext)

        dest.write_bytes(resp.content)
        stats["downloaded"] += 1
        log(f"  Downloaded image: {abs_url}")
        return dest, True
    except Exception as exc:
        stats["failed"] += 1
        log(f"  Failed image ({exc}): {abs_url}")
        return None, False


def _extract_img_src(img) -> str:
    for attr in ("src", "data-src", "data-original", "data-lazy-src"):
        val = img.get(attr, "")
        if val and not val.startswith("data:"):
            return val.strip()
    srcset = img.get("srcset", "")
    if srcset:
        parts = srcset.strip().split(",")
        first = parts[0].strip().split()[0]
        if first and not first.startswith("data:"):
            return first
    return ""


LOCAL_ASSETS_RE = re.compile(r"^(\.\./)+assets/")


def get_relative_asset_path(page_file: Path, asset_path: Path) -> str:
    return os.path.relpath(str(asset_path), start=str(page_file.parent)).replace("\\", "/")


def rewrite_content_images(
    content_soup: BeautifulSoup,
    page_url: str,
    session: requests.Session,
    assets_root: Path,
    stats: dict,
    page_file: Path = None,
) -> None:
    for img in content_soup.find_all("img"):
        src = _extract_img_src(img)
        if not src:
            continue
        if re.match(r"^(\.\./)+assets/", src):
            for attr in ("data-src", "data-original", "data-lazy-src", "srcset"):
                if attr in img.attrs:
                    del img[attr]
            continue
        abs_url = urljoin(page_url, src)
        p = urlparse(abs_url)
        if p.scheme not in {"http", "https"}:
            continue
        dest = local_image_path(abs_url, assets_root)
        result, ok = download_image(session, abs_url, dest, stats)
        if ok and result:
            if page_file is None:
                rel = Path("..") / "assets" / result.relative_to(assets_root).as_posix()
            else:
                rel = get_relative_asset_path(page_file, result)
            img["src"] = rel
            for attr in ("data-src", "data-original", "data-lazy-src", "srcset"):
                if attr in img.attrs:
                    del img[attr]

    for tag in content_soup.find_all(style=True):
        style_val = tag.get("style", "")
        matches = CSS_BG_RE.findall(style_val)
        if not matches:
            continue
        for url_str in matches:
            url_str = url_str.strip().strip("'\"")
            if url_str.startswith("data:") or url_str.startswith("#"):
                continue
            if re.search(r"(\.\./)+assets/", url_str):
                continue
            abs_url = urljoin(page_url, url_str)
            p = urlparse(abs_url)
            if p.scheme not in {"http", "https"}:
                continue
            dest = local_image_path(abs_url, assets_root)
            result, ok = download_image(session, abs_url, dest, stats)
            if ok and result:
                if page_file is None:
                    rel = Path("..") / "assets" / result.relative_to(assets_root).as_posix()
                else:
                    rel = get_relative_asset_path(page_file, result)
                tag["style"] = tag["style"].replace(url_str, rel)


def _page_has_remote_images(html: str) -> bool:
    soup = BeautifulSoup(html, "html.parser")
    for img in soup.find_all("img"):
        src = img.get("src", "")
        if src and not src.startswith("data:") and not LOCAL_ASSETS_RE.match(src):
            return True
        for attr in ("data-src", "data-original", "data-lazy-src"):
            val = img.get(attr, "")
            if val and not val.startswith("data:") and not LOCAL_ASSETS_RE.match(val):
                return True
    for tag in soup.find_all(style=True):
        for url_str in CSS_BG_RE.findall(tag.get("style", "")):
            url_str = url_str.strip().strip("'\"")
            if url_str and not url_str.startswith("data:") and not LOCAL_ASSETS_RE.match(url_str):
                return True
    return False


def reprocess_existing_pages(
    sidebar_items: List[SidebarItem],
    prefix: str,
    out_root: Path,
    session: requests.Session,
    stats: dict,
) -> None:
    pages_root = out_root / "pages"
    assets_root = out_root / "assets"
    assets_root.mkdir(parents=True, exist_ok=True)
    total = 0
    for item in sidebar_items:
        local_rel = url_to_local_page(item.url, prefix)
        out_file = pages_root / local_rel
        if not out_file.exists() or out_file.stat().st_size <= 50:
            continue
        html = out_file.read_text(encoding="utf-8")
        if not _page_has_remote_images(html):
            continue
        soup = BeautifulSoup(html, "html.parser")
        rewrite_content_images(soup, item.url, session, assets_root, stats, page_file=out_file)
        out_file.write_text(str(soup), encoding="utf-8")
        total += 1
        log(f"  Reprocessed existing page: {out_file.relative_to(out_root)}")
    if total == 0:
        log("  No pages needed image reprocessing.")
    log(f"Images downloaded: {stats['downloaded']}")
    log(f"Images failed: {stats['failed']}")


def rewrite_content_links(
    content_soup: BeautifulSoup,
    page_url: str,
    url_map: Dict[str, str],
    domain: str,
) -> None:
    for a in content_soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        try:
            abs_url = normalize_url(urljoin(page_url, href))
        except ValueError:
            continue
        if abs_url in url_map:
            a["href"] = url_map[abs_url]
        elif urlparse(abs_url).netloc.lower() == domain.lower():
            del a["href"]
            a["class"] = (a.get("class") or []) + ["offline-unavailable"]


def build_page_document(title: str, breadcrumb: List[str], content_html: str) -> str:
    crumb_html = ""
    if breadcrumb:
        crumb_html = (
            '<nav class="breadcrumb">'
            + "".join(f"<span>{html.escape(c)}</span>" for c in breadcrumb)
            + "</nav>"
        )
    safe_title = html.escape(title)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{safe_title}</title>
  <style>{PAGE_STYLES}</style>
</head>
<body>
  {crumb_html}
  <h1 class="doc-title">{safe_title}</h1>
  <article class="doc-content">{content_html}</article>
</body>
</html>
"""


async def capture_login_state(start_url: str, storage_state_path: Path, headless: bool = False) -> None:
    prefix = derive_allowed_prefix(start_url)
    interactive = bool(sys.stdin and sys.stdin.isatty())

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1600, "height": 1200})
        page = await context.new_page()
        log(f"Opening start page: {start_url}")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=120000)

        if interactive:
            print()
            print("=" * 80)
            print("  1. Log in manually in the browser window.")
            print("  2. Navigate until you can see the documentation page.")
            print("  3. Come back here and press ENTER to continue.")
            print("=" * 80)
            print()
            await asyncio.to_thread(input, ">>> Press ENTER when logged in and ready to continue: ")
            print()
            log("Enter received — work is starting now.")
            log("Reloading start page to verify your session...")
            await page.goto(start_url, wait_until="networkidle", timeout=120000)
        else:
            log("Waiting for login (non-interactive mode, up to 15 minutes)...")
            deadline = time.monotonic() + 900
            authenticated_hits = 0
            while time.monotonic() < deadline:
                try:
                    html = await page.content()
                    title = await page.title()
                    current_path = urlparse(page.url or "").path
                    ready = (
                        await context.cookies()
                        and current_path.startswith(prefix)
                        and looks_authenticated_page(html, title)
                    )
                except Exception:
                    ready = False

                if ready:
                    authenticated_hits += 1
                    if authenticated_hits >= 2:
                        log("Login detected automatically.")
                        break
                else:
                    authenticated_hits = 0

                await asyncio.sleep(2)
            else:
                raise RuntimeError(
                    "Timed out waiting for login. Run from an interactive terminal "
                    "so you can press ENTER after logging in."
                )

        html = await page.content()
        title = await page.title()
        cookies = await context.cookies()
        current_url = page.url or start_url

        log(f"  Page title : {title}")
        log(f"  Current URL: {current_url}")
        log(f"  Cookies    : {len(cookies)}")

        if not cookies:
            await browser.close()
            raise RuntimeError("No cookies found. Login may not have completed.")

        if looks_like_login_page(html, title, current_url):
            log("WARNING: Page still looks like a login screen.")
            if interactive:
                log("Saving session anyway because you pressed ENTER.")
            else:
                await browser.close()
                raise RuntimeError("Still on login page. Complete login and retry.")
        elif looks_like_docs_page(html, title):
            log("Documentation page detected — session looks good.")
        else:
            log("Session captured — continuing to sidebar crawl.")

        storage_state_path.parent.mkdir(parents=True, exist_ok=True)
        await context.storage_state(path=str(storage_state_path))
        log(f"Saved session to {storage_state_path}")
        await browser.close()


async def collect_sidebar_urls(
    start_url: str,
    domain: str,
    prefix: str,
    storage_state: Path,
    headless: bool,
) -> List[SidebarItem]:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
            storage_state=str(storage_state),
        )
        page = await context.new_page()
        log(f"Loading start page for sidebar extraction: {start_url}")
        await page.goto(start_url, wait_until="networkidle", timeout=120000)

        html = await page.content()
        title = guess_title_from_html(html, "Welcome")
        if looks_like_login_page(html, title, page.url):
            await browser.close()
            raise AuthenticationRequired("Login page detected. Session is missing or expired.")

        log("Expanding sidebar tree (this may take a minute)...")
        for cycle in range(25):
            clicked = await page.evaluate(EXPAND_SIDEBAR_JS)
            log(f"  expand cycle {cycle + 1}: clicked {clicked}")
            await asyncio.sleep(1.2)
            if clicked == 0:
                break

        await page.evaluate(SCROLL_SIDEBAR_JS)
        await asyncio.sleep(0.8)

        raw_items = await page.evaluate(EXTRACT_SIDEBAR_JS)
        await browser.close()

    items: List[SidebarItem] = []
    seen: Set[str] = set()
    for raw in raw_items:
        url = normalize_url(raw.get("url", ""))
        if not url or url in seen or not is_doc_url(url, domain, prefix):
            continue
        seen.add(url)
        title = re.sub(r"\s+", " ", raw.get("title", "")).strip() or Path(urlparse(url).path).stem
        nav_path = [str(x).strip() for x in (raw.get("navPath") or []) if str(x).strip()]
        items.append(SidebarItem(url=url, title=title, nav_path=nav_path))

    if not items:
        raise RuntimeError("No sidebar links found. Check login state and start URL.")

    log(f"Collected {len(items)} sidebar page URLs")
    return items


async def scrape_pages(
    sidebar_items: List[SidebarItem],
    domain: str,
    prefix: str,
    out_root: Path,
    storage_state: Path,
    cookies: List[Dict],
    headless: bool,
    max_pages: int,
    start_url: str,
    existing_records: Optional[List[PageRecord]] = None,
) -> List[PageRecord]:
    pages_root = out_root / "pages"
    assets_root = out_root / "assets"
    pages_root.mkdir(parents=True, exist_ok=True)
    assets_root.mkdir(parents=True, exist_ok=True)

    items = sidebar_items[:max_pages]
    url_map: Dict[str, str] = {}
    for item in items:
        local_rel = url_to_local_page(item.url, prefix)
        url_map[item.url] = f"pages/{local_rel}"

    session = requests_session(cookies)
    records: List[PageRecord] = list(existing_records or [])
    completed_urls: Set[str] = {r.url for r in records}
    total = len(items)

    if completed_urls:
        log(f"Resuming scrape: {len(completed_urls)}/{total} pages already saved — skipping those")

    img_stats: Dict[str, int] = {"downloaded": 0, "failed": 0}

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={"width": 1600, "height": 1200},
            storage_state=str(storage_state) if storage_state.exists() else None,
        )
        page = await context.new_page()

        for idx, item in enumerate(items, start=1):
            local_rel = url_to_local_page(item.url, prefix)
            out_file = pages_root / local_rel

            if item.url in completed_urls:
                log(f"[{idx}/{total}] SKIP (already scraped) {item.title}")
                continue

            if out_file.exists() and out_file.stat().st_size > 50:
                recovered = recover_record_from_file(item, prefix, out_root)
                if recovered:
                    records.append(recovered)
                    completed_urls.add(item.url)
                    save_checkpoint(out_root, start_url, prefix, sidebar_items, records)
                    log(f"[{idx}/{total}] SKIP (found on disk) {item.title}")
                    continue

            log(f"[{idx}/{total}] {item.title} -> {item.url}")
            try:
                await page.goto(item.url, wait_until="networkidle", timeout=120000)
                raw_html = await page.content()
            except Exception as exc:
                log(f"  failed to load: {exc}")
                continue

            title = guess_title_from_html(raw_html, item.title)
            current_url = page.url or item.url
            if looks_like_login_page(raw_html, title, current_url):
                log(f"  SKIP (login page): {item.title}")
                continue

            pw_cookies = await context.cookies()
            for c in pw_cookies:
                session.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))

            content_html, content_soup = extract_main_html(raw_html)
            local_rel = url_to_local_page(item.url, prefix)
            out_file = pages_root / local_rel
            rewrite_content_images(content_soup, item.url, session, assets_root, img_stats, page_file=out_file)
            rewrite_content_links(content_soup, item.url, url_map, domain)

            breadcrumb = breadcrumb_from_soup(BeautifulSoup(raw_html, "html.parser")) or item.nav_path
            out_file.parent.mkdir(parents=True, exist_ok=True)
            out_file.write_text(
                build_page_document(title, breadcrumb, str(content_soup)),
                encoding="utf-8",
            )

            record = PageRecord(
                url=item.url,
                title=title,
                local_path=f"pages/{local_rel}",
                breadcrumb=breadcrumb,
                nav_path=item.nav_path,
                text=page_text(content_soup)[:12000],
            )
            records.append(record)
            completed_urls.add(item.url)
            save_checkpoint(out_root, start_url, prefix, sidebar_items, records)
            log(f"  saved -> {out_file.relative_to(out_root)}")

        await browser.close()

    log(f"Images downloaded: {img_stats['downloaded']}")
    log(f"Images failed: {img_stats['failed']}")
    return records


def build_menu_tree(records: List[PageRecord]) -> Dict:
    root: Dict = {"name": "Documentation", "children": {}, "pages": []}

    def ensure_folder(parent: Dict, name: str) -> Dict:
        return parent["children"].setdefault(name, {"name": name, "children": {}, "pages": []})

    for rec in records:
        node = root
        for part in rec.nav_path:
            node = ensure_folder(node, part)
        node["pages"].append({"title": rec.title, "url": rec.local_path, "breadcrumb": rec.breadcrumb})

    return root


def menu_to_json(node: Dict) -> Dict:
    return {
        "name": node["name"],
        "children": [menu_to_json(v) for v in sorted(node["children"].values(), key=lambda x: x["name"].lower())],
        "pages": sorted(node["pages"], key=lambda p: p["title"].lower()),
    }


def build_search_index(records: List[PageRecord]) -> List[Dict]:
    return [
        {
            "title": r.title,
            "url": r.local_path,
            "breadcrumb": r.breadcrumb,
            "navPath": r.nav_path,
            "text": r.text,
        }
        for r in records
    ]


def write_js_var(path: Path, var_name: str, data: object) -> None:
    path.write_text(
        f"window.{var_name} = {json.dumps(data, ensure_ascii=False, indent=2)};\n",
        encoding="utf-8",
    )


INDEX_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Documentation Training Portal</title>
  <style>
    :root {
      --bg:#f5f7fb; --panel:#fff; --panel2:#eef3ff; --text:#1e2430; --muted:#6b7484;
      --border:#dbe2f0; --accent:#2f4ea2; --shadow:0 8px 30px rgba(18,32,73,.08);
    }
    * { box-sizing:border-box; }
    html, body { height:100%; margin:0; }
    body { font-family:Segoe UI,Roboto,Arial,sans-serif; background:var(--bg); color:var(--text); }
    .app { display:grid; grid-template-columns:360px 1fr; height:100vh; }
    .sidebar {
      border-right:1px solid var(--border);
      background:linear-gradient(180deg,#fff 0%,#f8faff 100%);
      display:flex; flex-direction:column; min-width:280px;
    }
    .brand { padding:16px 16px 10px; border-bottom:1px solid var(--border); }
    .brand h1 { margin:0 0 4px; font-size:20px; color:var(--accent); }
    .brand p { margin:0; font-size:12px; color:var(--muted); line-height:1.45; }
    .controls { padding:12px 16px; border-bottom:1px solid var(--border); background:#f8faff; }
    .search {
      width:100%; padding:11px 12px; border:1px solid var(--border); border-radius:12px;
      outline:none; background:#fff; font-size:14px;
    }
    .stats { display:flex; gap:10px; margin-top:10px; font-size:12px; color:var(--muted); }
    .tree-wrap { overflow:auto; padding:10px 12px 20px; flex:1; }
    details { background:rgba(255,255,255,.85); border:1px solid var(--border); border-radius:10px; margin-bottom:6px; overflow:hidden; }
    details > summary { list-style:none; cursor:pointer; padding:9px 11px; font-weight:600; color:var(--accent); }
    details > summary::-webkit-details-marker { display:none; }
    .page-link {
      display:block; text-decoration:none; color:var(--text); padding:8px 11px;
      border-top:1px solid var(--border); font-size:13px;
    }
    .page-link:hover, .page-link.active { background:var(--panel2); }
    .main { display:grid; grid-template-rows:auto 1fr; min-width:0; }
    .topbar {
      background:linear-gradient(90deg,#253b86 0%,#334ea0 100%); color:#fff;
      padding:12px 18px; display:flex; align-items:center; justify-content:space-between; gap:12px;
    }
    .doc-title { font-size:18px; font-weight:700; margin-bottom:3px; }
    .doc-meta { font-size:12px; opacity:.92; }
    .btn {
      appearance:none; border:1px solid rgba(255,255,255,.45); color:#fff; background:rgba(255,255,255,.08);
      padding:8px 11px; border-radius:10px; cursor:pointer; text-decoration:none; font-size:13px;
    }
    .content { padding:14px; min-width:0; overflow:hidden; }
    .reader {
      width:100%; height:calc(100vh - 120px); border:1px solid var(--border);
      border-radius:16px; background:#fff; box-shadow:var(--shadow);
    }
    .results { padding:0 16px 8px; max-height:180px; overflow:auto; }
    .result {
      display:block; padding:8px 10px; border:1px solid var(--border); border-radius:10px;
      background:#fff; text-decoration:none; color:var(--text); margin-bottom:6px; font-size:13px;
    }
    .result:hover { background:var(--panel2); }
    .result .r-title { font-weight:700; margin-bottom:2px; }
    .result .r-meta { font-size:11px; color:var(--muted); }
    @media (max-width:1100px) {
      .app { grid-template-columns:1fr; grid-template-rows:auto 1fr; }
      .sidebar { max-height:42vh; border-right:0; border-bottom:1px solid var(--border); }
      .reader { height:58vh; }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside class="sidebar">
      <div class="brand">
        <h1>Documentation Training Portal</h1>
        <p>Offline documentation from the authenticated sidebar crawl.</p>
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
    results.innerHTML = items.slice(0, 30).map(item => `
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


def write_portal(out_root: Path, records: List[PageRecord]) -> None:
    out_root.mkdir(parents=True, exist_ok=True)
    write_js_var(out_root / "menu.js", "PORTAL_MENU", menu_to_json(build_menu_tree(records)))
    write_js_var(out_root / "search.js", "PORTAL_SEARCH", build_search_index(records))
    (out_root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    log(f"Portal written to {out_root.resolve()}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build offline documentation training portal from authenticated sidebar crawl")
    parser.add_argument("--start-url", default=DEFAULT_START_URL)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument("--storage-state", default=DEFAULT_STORAGE)
    parser.add_argument("--allowed-domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--headless", action="store_true", help="Deprecated: crawl is headless by default after login")
    parser.add_argument("--show-browser", action="store_true", help="Show browser during sidebar crawl and page scrape")
    parser.add_argument("--max-pages", type=int, default=DEFAULT_MAX_PAGES)
    parser.add_argument("--login-only", action="store_true", help="Only capture login session, do not crawl")
    parser.add_argument("--fresh", action="store_true", help="Ignore checkpoint and start crawl from scratch")
    parser.add_argument("--refresh-images", action="store_true", help="Reprocess existing pages: download missing images and rewrite paths")
    parser.add_argument("--repair-assets", action="store_true", help="Fix asset paths in existing HTML pages (no crawl or auth needed)")
    return parser.parse_args()


def crawl_headless(args: argparse.Namespace) -> bool:
    if args.show_browser:
        return False
    return True


def run_crawl_with_resume(
    args: argparse.Namespace,
    start_url: str,
    prefix: str,
    output_root: Path,
    storage_state: Path,
    total_steps: int,
) -> List[PageRecord]:
    cookies = cookies_from_storage_state(storage_state)
    headless = crawl_headless(args)
    sidebar_items: Optional[List[SidebarItem]] = None
    existing_records: List[PageRecord] = []

    if not args.fresh:
        sidebar_items, existing_records = load_checkpoint(output_root, start_url, prefix)
        if sidebar_items and existing_records:
            log(
                f"Found checkpoint: {len(existing_records)}/{len(sidebar_items)} pages already scraped — will resume"
            )
        elif sidebar_items:
            log(f"Found checkpoint with sidebar ({len(sidebar_items)} links) — will resume scrape")

    while True:
        try:
            if sidebar_items is None:
                log_phase(2, total_steps, "Collecting sidebar links")
                sidebar_items = asyncio.run(
                    collect_sidebar_urls(
                        start_url=start_url,
                        domain=args.allowed_domain,
                        prefix=prefix,
                        storage_state=storage_state,
                        headless=headless,
                    )
                )
                save_checkpoint(output_root, start_url, prefix, sidebar_items, existing_records)

            remaining = len(sidebar_items) - len(existing_records)
            log_phase(3, total_steps, f"Scraping pages ({len(existing_records)} done, {remaining} remaining)")
            records = asyncio.run(
                scrape_pages(
                    sidebar_items=sidebar_items,
                    domain=args.allowed_domain,
                    prefix=prefix,
                    out_root=output_root,
                    storage_state=storage_state,
                    cookies=cookies,
                    headless=headless,
                    max_pages=args.max_pages,
                    start_url=start_url,
                    existing_records=existing_records,
                )
            )
            return records
        except AuthenticationRequired as exc:
            log(str(exc))
            if sidebar_items is not None:
                _, existing_records = load_checkpoint(output_root, start_url, prefix)
            log_phase(1, total_steps, "Session expired — log in again (browser will open)")
            asyncio.run(capture_login_state(start_url, storage_state, headless=False))
            cookies = cookies_from_storage_state(storage_state)
            headless = crawl_headless(args)
            log("Session refreshed — resuming headless from last saved page...")
            continue


def refresh_images_main(
    args: argparse.Namespace,
    start_url: str,
    prefix: str,
    output_root: Path,
    storage_state: Path,
) -> int:
    log_phase(2, 3, "Loading sidebar links from checkpoint")
    sidebar_items, records = load_checkpoint(output_root, start_url, prefix)
    if sidebar_items is None:
        log("No checkpoint found — collecting sidebar links (browser will open)")
        while True:
            if not storage_state.exists():
                asyncio.run(capture_login_state(start_url, storage_state, headless=False))
            try:
                sidebar_items = asyncio.run(
                    collect_sidebar_urls(
                        start_url=start_url,
                        domain=args.allowed_domain,
                        prefix=prefix,
                        storage_state=storage_state,
                        headless=crawl_headless(args),
                    )
                )
                break
            except AuthenticationRequired:
                log("Session expired — re-login required")
                asyncio.run(capture_login_state(start_url, storage_state, headless=False))
        save_checkpoint(output_root, start_url, prefix, sidebar_items, records)
    log(f"Found {len(sidebar_items)} sidebar links, {len(records)} existing records")

    log_phase(3, 3, "Reprocessing existing pages for images")
    cookies = cookies_from_storage_state(storage_state)
    session = requests_session(cookies)
    img_stats: Dict[str, int] = {"downloaded": 0, "failed": 0}
    reprocess_existing_pages(sidebar_items, prefix, output_root, session, img_stats)

    log_phase(4, 3, "Building offline portal")
    write_portal(output_root, records)
    print()
    log("All done.")
    log(f"Open this file in your browser: {output_root / 'index.html'}")
    return 0


def _strip_dotdot_prefix(path: str) -> str:
    return re.sub(r"^(\.\./)+assets/", "", path)


def build_records_from_pages(pages_root: Path) -> List[PageRecord]:
    records: List[PageRecord] = []
    for html_path in sorted(pages_root.rglob("*.html")):
        try:
            record = _record_from_html_path(html_path, pages_root)
        except Exception:
            continue
        if record is not None:
            records.append(record)
    log(f"Reconstructed {len(records)} page records from disk")
    return records


def _record_from_html_path(html_path: Path, pages_root: Path) -> Optional[PageRecord]:
    try:
        html = html_path.read_text(encoding="utf-8")
    except Exception:
        return None
    if len(html) <= 50:
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = guess_title_from_html(html, html_path.stem)
    breadcrumb = breadcrumb_from_soup(soup) or []
    article = soup.find("article", class_="doc-content")
    text = page_text(article or soup)[:12000]
    local_path = str(html_path.relative_to(pages_root.parent).as_posix())
    return PageRecord(
        url="",
        title=title,
        local_path=local_path,
        breadcrumb=breadcrumb,
        nav_path=breadcrumb,
        text=text,
    )


def repair_assets_main(output_root: Path) -> int:
    pages_root = output_root / "pages"
    assets_root = output_root / "assets"
    if not pages_root.exists():
        log(f"No pages directory found at {pages_root}")
        return 1

    count = 0
    fixed = 0
    for html_path in sorted(pages_root.rglob("*.html")):
        count += 1
        try:
            html = html_path.read_text(encoding="utf-8")
        except Exception:
            continue
        soup = BeautifulSoup(html, "html.parser")
        changed = False

        for img in soup.find_all("img"):
            src = img.get("src", "")
            if not src or src.startswith("data:"):
                continue
            if LOCAL_ASSETS_RE.match(src):
                asset_rel = _strip_dotdot_prefix(src)
                dest = assets_root / asset_rel
                if dest.exists():
                    correct = get_relative_asset_path(html_path, dest)
                    if correct != src:
                        img["src"] = correct
                        changed = True
                continue
            if src.startswith("http"):
                dest = local_image_path(src, assets_root)
                if dest.exists():
                    correct = get_relative_asset_path(html_path, dest)
                    if correct != src:
                        img["src"] = correct
                        changed = True

        for tag in soup.find_all(style=True):
            style_val = tag.get("style", "")
            for url_str in CSS_BG_RE.findall(style_val):
                raw = url_str.strip().strip("'\"")
                if not raw or raw.startswith("data:"):
                    continue
                if LOCAL_ASSETS_RE.match(raw):
                    asset_rel = _strip_dotdot_prefix(raw)
                    dest = assets_root / asset_rel
                    if dest.exists():
                        correct = get_relative_asset_path(html_path, dest)
                        if correct != raw:
                            tag["style"] = tag["style"].replace(raw, correct)
                            changed = True
                    continue
                if raw.startswith("http"):
                    dest = local_image_path(raw, assets_root)
                    if dest.exists():
                        correct = get_relative_asset_path(html_path, dest)
                        if correct != raw:
                            tag["style"] = tag["style"].replace(raw, correct)
                            changed = True

        if changed:
            html_path.write_text(str(soup), encoding="utf-8")
            fixed += 1
            log(f"  Repaired: {html_path.relative_to(output_root)}")
    log(f"Scanned {count} page files")
    log(f"Fixed asset paths in {fixed} files")

    log("Rebuilding portal data from repaired pages...")
    records = build_records_from_pages(pages_root)
    if records:
        write_portal(output_root, records)
    return 0


def main() -> int:
    args = parse_args()
    start_url = args.start_url
    output_root = Path(args.output).resolve()
    storage_state = Path(args.storage_state).resolve()
    prefix = derive_allowed_prefix(start_url)

    log(f"Start URL: {start_url}")
    log(f"Allowed prefix: {prefix}")
    log(f"Output: {output_root}")

    if args.repair_assets:
        return repair_assets_main(output_root)

    total_steps = 3 if args.login_only else 4

    if args.refresh_images:
        if not storage_state.exists():
            log("Storage state file not found. Please login first.")
            return 1
        return refresh_images_main(args, start_url, prefix, output_root, storage_state)

    if not storage_state.exists():
        log_phase(1, total_steps, "Manual login — browser will open")
        asyncio.run(capture_login_state(start_url, storage_state, headless=False))
    else:
        log(f"Using saved session: {storage_state}")

    if args.login_only:
        log("Login-only mode complete.")
        return 0

    records = run_crawl_with_resume(
        args=args,
        start_url=start_url,
        prefix=prefix,
        output_root=output_root,
        storage_state=storage_state,
        total_steps=total_steps,
    )

    log_phase(4, total_steps, "Building offline portal")
    log(f"Scraped {len(records)} pages")
    write_portal(output_root, records)
    clear_checkpoint(output_root)
    print()
    log("All done.")
    log(f"Open this file in your browser: {output_root / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
