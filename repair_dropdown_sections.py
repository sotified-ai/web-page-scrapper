#!/usr/bin/env python3
"""
Repair dropdown / accordion sections in saved portal pages.

What it does:
- scans training-portal/pages/**/*.html
- finds MadCap / accordion style blocks like MCDropDown / dropDown
- forces the hidden bodies to render inline so the content is visible offline
- optionally rewrites only pages that need it

This does NOT change authentication, crawling, sidebar generation, or search.
It only repairs page content visibility.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Iterable

from bs4 import BeautifulSoup

DROPDOWN_MARKERS = (
    "MCDropDown",
    "MCDropDown_Closed",
    "MCDropDown_Open",
    "dropDown",
    "dropDown_Closed",
    "dropDown_Open",
    "MCDropDownBody",
    "dropDownBody",
    "MCDropDownHotSpot",
    "dropDownHotSpot",
)

STYLE_ID = "portal-dropdown-repair-style"
SCRIPT_ID = "portal-dropdown-repair-script"

REPAIR_CSS = """
/* Portal dropdown repair */
.MCDropDown_Closed,
.dropDown_Closed {
  display: block !important;
}

.MCDropDownBody,
.dropDownBody,
.MCDropDown > .MCDropDownBody,
.dropDown > .dropDownBody,
.MCDropDown_Closed > .MCDropDownBody,
.dropDown_Closed > .dropDownBody {
  display: block !important;
  visibility: visible !important;
  opacity: 1 !important;
  height: auto !important;
  max-height: none !important;
  overflow: visible !important;
}

.MCDropDownHotSpot,
.dropDownHotSpot,
.MCDropDownHotSpot_Closed,
.dropDownHotSpot_Closed {
  cursor: default !important;
}

/* Keep the clickable header visually intact, only make the body visible. */
"""

REPAIR_SCRIPT = r"""
(function () {
  function expand(node) {
    if (!node) return;
    node.classList.remove('MCDropDown_Closed', 'dropDown_Closed');
    node.classList.add('MCDropDown_Open', 'dropDown_Open');
    node.style.display = 'block';
  }

  function showBody(node) {
    if (!node) return;
    node.style.display = 'block';
    node.style.visibility = 'visible';
    node.style.opacity = '1';
    node.style.height = 'auto';
    node.style.maxHeight = 'none';
    node.style.overflow = 'visible';
    node.hidden = false;
  }

  function repairDropdown(drop) {
    expand(drop);

    const bodies = drop.querySelectorAll('.MCDropDownBody, .dropDownBody');
    bodies.forEach(showBody);

    const hidden = drop.querySelectorAll('[style*="display:none"], [style*="display: none"]');
    hidden.forEach(showBody);
  }

  function run() {
    document.querySelectorAll(
      '.MCDropDown, .dropDown, .MCDropDown_Closed, .dropDown_Closed'
    ).forEach(repairDropdown);

    document.querySelectorAll('.MCDropDownBody, .dropDownBody').forEach(showBody);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', run);
  } else {
    run();
  }
})();
"""


def has_dropdown(html: str) -> bool:
    return any(marker in html for marker in DROPDOWN_MARKERS)


def ensure_head_assets(soup: BeautifulSoup) -> None:
    head = soup.head
    if head is None:
        head = soup.new_tag("head")
        if soup.html:
            soup.html.insert(0, head)
        else:
            html_tag = soup.new_tag("html")
            html_tag.insert(0, head)
            html_tag.extend(list(soup.contents))
            soup.clear()
            soup.append(html_tag)

    if not head.find("style", id=STYLE_ID):
        style = soup.new_tag("style", id=STYLE_ID)
        style.string = REPAIR_CSS
        head.append(style)

    if not head.find("script", id=SCRIPT_ID):
        script = soup.new_tag("script", id=SCRIPT_ID)
        script.string = REPAIR_SCRIPT
        head.append(script)


def repair_html_file(path: Path, dry_run: bool = False) -> bool:
    raw = path.read_text(encoding="utf-8", errors="ignore")
    if not has_dropdown(raw):
        return False

    soup = BeautifulSoup(raw, "html.parser")
    changed = False

    # Convert closed dropdown wrappers to open, visible wrappers.
    for node in soup.select(".MCDropDown_Closed, .dropDown_Closed"):
        classes = node.get("class", [])
        if classes:
            new_classes = []
            for c in classes:
                if c in {"MCDropDown_Closed", "dropDown_Closed"}:
                    new_classes.append(c.replace("_Closed", "_Open"))
                    changed = True
                else:
                    new_classes.append(c)
            if new_classes != classes:
                node["class"] = new_classes

        style = node.get("style", "")
        if "display:none" in style.replace(" ", "") or "display: none" in style:
            changed = True
        node["style"] = _merge_visible_style(style)

    # Force bodies to be visible.
    for node in soup.select(".MCDropDownBody, .dropDownBody"):
        new_style = _merge_visible_style(node.get("style", ""))
        if node.get("style", "") != new_style:
            node["style"] = new_style
            changed = True
        if node.has_attr("hidden"):
            del node["hidden"]
            changed = True

    # Catch any nested hidden sections inside dropdowns.
    for node in soup.select('.MCDropDown [style*="display:none"], .MCDropDown [style*="display: none"], .dropDown [style*="display:none"], .dropDown [style*="display: none"]'):
        new_style = _merge_visible_style(node.get("style", ""))
        if node.get("style", "") != new_style:
            node["style"] = new_style
            changed = True
        if node.has_attr("hidden"):
            del node["hidden"]
            changed = True

    if changed:
        ensure_head_assets(soup)
        if not dry_run:
            path.write_text(str(soup), encoding="utf-8")
    return changed


def _merge_visible_style(style: str) -> str:
    # Minimal, safe override, preserves existing styling as much as possible.
    style = style or ""
    additions = [
        "display:block !important",
        "visibility:visible !important",
        "opacity:1 !important",
        "height:auto !important",
        "max-height:none !important",
        "overflow:visible !important",
    ]

    style_clean = style.strip()
    # If already has explicit visible overrides, leave as-is.
    if "display:block !important" in style_clean.replace(" ", ""):
        return style_clean

    # Remove the most common hiding declarations, then append visible ones.
    parts = [p.strip() for p in style_clean.split(";") if p.strip()]
    kept = []
    for p in parts:
        key = p.split(":", 1)[0].strip().lower() if ":" in p else ""
        if key in {"display", "visibility", "opacity", "height", "max-height", "overflow"}:
            continue
        kept.append(p)
    kept.extend(additions)
    return "; ".join(kept) + ";"


def iter_html_files(root: Path) -> Iterable[Path]:
    for p in sorted(root.rglob("*.html")):
        if p.is_file():
            yield p


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair dropdown sections in saved pages.")
    parser.add_argument(
        "--root",
        required=True,
        help="Path to the training-portal folder, e.g. D:\\Code-Projects\\scrape_portal\\training-portal",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write files, just report what would change.")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    pages_root = root / "pages"
    if not pages_root.exists():
        print(f"pages folder not found: {pages_root}")
        return 1

    total = 0
    changed = 0
    for html_file in iter_html_files(pages_root):
        total += 1
        try:
            if repair_html_file(html_file, dry_run=args.dry_run):
                changed += 1
                print(f"repaired: {html_file.relative_to(root)}")
        except Exception as exc:
            print(f"skipped (error): {html_file.relative_to(root)} -> {exc}")

    print()
    print(f"scanned: {total}")
    print(f"repaired: {changed}")
    if args.dry_run:
        print("dry run only, no files were written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
