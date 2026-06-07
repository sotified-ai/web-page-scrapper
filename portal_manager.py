#!/usr/bin/env python3
"""
Docs Portal Manager

A single entry point for the existing documentation portal tools.

What it does:
- crawls a new authenticated portal URL using the existing build_training_portal.py
- repairs image paths using --repair-assets
- rebuilds the sidebar/search/index from the saved pages folder
- repairs dropdown / accordion sections in saved pages

This keeps the existing crawler untouched, while giving you one reusable command
for new URLs and for fixing existing portal output.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import List


SCRIPT_DIR = Path(__file__).resolve().parent

BUILD_SCRIPT = SCRIPT_DIR / "build_training_portal.py"
SIDEBAR_REPAIR_SCRIPT = SCRIPT_DIR / "repair_training_portal_sidebar.py"
DROPDOWN_REPAIR_SCRIPT = SCRIPT_DIR / "repair_dropdown_sections.py"

DEFAULT_START_URL = "defaulturl"
DEFAULT_OUTPUT = r"folder output"
DEFAULT_STORAGE = str(SCRIPT_DIR / "storage_state.json")
DEFAULT_DOMAIN = "docs.example.com"


def log(msg: str) -> None:
    print(msg, flush=True)


def require_script(path: Path) -> None:
    if not path.exists():
        raise FileNotFoundError(f"Missing required script: {path}")


def run_cmd(cmd: List[str]) -> int:
    log("")
    log("Running:")
    log("  " + " ".join(cmd))
    log("")
    result = subprocess.run(cmd)
    return result.returncode


def build_args_from_common(args: argparse.Namespace) -> List[str]:
    cmd = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--start-url",
        args.start_url,
        "--output",
        args.output,
        "--storage-state",
        args.storage_state,
        "--allowed-domain",
        args.allowed_domain,
        "--max-pages",
        str(args.max_pages),
    ]

    if args.show_browser:
        cmd.append("--show-browser")
    if args.headless:
        cmd.append("--headless")
    if args.login_only:
        cmd.append("--login-only")
    if args.fresh:
        cmd.append("--fresh")
    if args.refresh_images:
        cmd.append("--refresh-images")
    if args.repair_assets:
        cmd.append("--repair-assets")
    return cmd


def run_crawl(args: argparse.Namespace) -> int:
    require_script(BUILD_SCRIPT)
    for attempt in range(2):
        code = run_cmd(build_args_from_common(args))
        if code == 0:
            return 0
        if attempt == 0:
            log("Crawl failed — will re-login and resume from checkpoint...")
            storage_path = Path(args.storage_state)
            if storage_path.exists():
                storage_path.unlink()
            login_cmd = [
                sys.executable,
                str(BUILD_SCRIPT),
                "--start-url", args.start_url,
                "--output", args.output,
                "--storage-state", args.storage_state,
                "--login-only",
            ]
            login_code = run_cmd(login_cmd)
            if login_code != 0:
                log("Login attempt failed.")
                return login_code
    return code


def run_sidebar_repair(args: argparse.Namespace) -> int:
    require_script(SIDEBAR_REPAIR_SCRIPT)
    cmd = [
        sys.executable,
        str(SIDEBAR_REPAIR_SCRIPT),
        "--root",
        args.output,
    ]
    if args.repair_output and args.repair_output != args.output:
        cmd.extend(["--output", args.repair_output])
    return run_cmd(cmd)


def run_dropdown_repair(args: argparse.Namespace) -> int:
    require_script(DROPDOWN_REPAIR_SCRIPT)
    cmd = [
        sys.executable,
        str(DROPDOWN_REPAIR_SCRIPT),
        "--root",
        args.output,
    ]
    return run_cmd(cmd)


def run_repair_assets(args: argparse.Namespace) -> int:
    require_script(BUILD_SCRIPT)
    cmd = [
        sys.executable,
        str(BUILD_SCRIPT),
        "--output",
        args.output,
        "--repair-assets",
    ]
    # The repair-assets mode in the existing crawler rebuilds menu/search/index
    # from the repaired pages, so it is safe to run independently.
    return run_cmd(cmd)


def run_repair_all(args: argparse.Namespace) -> int:
    # Order matters:
    # 1) fix dropdown visibility inside saved pages
    # 2) repair asset paths inside saved pages
    # 3) rebuild sidebar/search/index from the cleaned pages
    for step in (
        run_dropdown_repair,
        run_repair_assets,
        run_sidebar_repair,
    ):
        code = step(args)
        if code != 0:
            return code
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="One reusable entry point for portal crawl + repair workflows"
    )
    p.add_argument(
        "mode",
        nargs="?",
        default="crawl",
        choices=("crawl", "repair-assets", "repair-sidebar", "repair-dropdowns", "repair-all"),
        help="What to do",
    )
    p.add_argument("--start-url", default=DEFAULT_START_URL)
    p.add_argument("--output", default=DEFAULT_OUTPUT)
    p.add_argument("--storage-state", default=DEFAULT_STORAGE)
    p.add_argument("--allowed-domain", default=DEFAULT_DOMAIN)
    p.add_argument("--max-pages", type=int, default=5000)
    p.add_argument("--show-browser", action="store_true")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--login-only", action="store_true")
    p.add_argument("--fresh", action="store_true")
    p.add_argument("--refresh-images", action="store_true")
    p.add_argument("--repair-assets", action="store_true", help=argparse.SUPPRESS)
    p.add_argument(
        "--repair-output",
        default="",
        help="Optional alternate output path for repair-sidebar mode",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    # Normalize the optional repair output path.
    if not args.repair_output:
        args.repair_output = args.output

    log(f"Mode: {args.mode}")
    log(f"Start URL: {args.start_url}")
    log(f"Output: {args.output}")
    log(f"Storage: {args.storage_state}")

    if args.mode == "crawl":
        return run_crawl(args)
    if args.mode == "repair-assets":
        return run_repair_assets(args)
    if args.mode == "repair-sidebar":
        return run_sidebar_repair(args)
    if args.mode == "repair-dropdowns":
        return run_dropdown_repair(args)
    if args.mode == "repair-all":
        return run_repair_all(args)

    log(f"Unknown mode: {args.mode}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
