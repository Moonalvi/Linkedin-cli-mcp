from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from . import __version__
from .config import ScannerConfig, load_config, save_config
from .db import (
    due_scans,
    init_db,
    log,
    mark_due_done,
    mark_due_failed,
    record_snapshot,
    status_summary,
    upsert_post,
)


PRINT_WIDTH = 100


def _short_urn(value: Any) -> str:
    text = str(value or "")
    return text[-12:] if len(text) > 12 else text


def _scanner_log(stage: str, **fields: Any) -> None:
    parts = [f"[SocioScanner] {stage}"]
    for key, value in fields.items():
        safe_value = str(value or "").replace("\n", " ")[:180]
        parts.append(f"{key}={safe_value}")
    print(" ".join(parts), flush=True)


# -- terminal output helpers ------------------------------------------

def _print_section(title: str) -> None:
    print(f"\n{'=' * PRINT_WIDTH}")
    print(f"  {title}")
    print(f"{'=' * PRINT_WIDTH}")


def _print_payload(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _print_summary_row(label: str, value: Any) -> None:
    print(f"  {label:.<40} {value}")


# -- CLI commands -----------------------------------------------------

def cmd_init(_: argparse.Namespace) -> int:
    _scanner_log("init")
    init_db()
    print("Scanner state initialized.")
    print(f"  Data dir : {__import__('scanner.config', fromlist=['config']).APP_DIR}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    from .linkedin import LinkedinScanner

    scanner = LinkedinScanner(headless=False)
    try:
        if args.check:
            try:
                logged_in = scanner.login_check()
                _scanner_log("login_check", logged_in=str(logged_in).lower())
                print("logged_in" if logged_in else "login_required")
                return 0 if logged_in else 1
            except Exception as exc:
                _scanner_log("login_check_error", error=str(exc))
                print(f"Error checking login: {exc}", file=sys.stderr)
                return 1
        _scanner_log("login_open")
        try:
            profile_url = scanner.open_login()
            if profile_url:
                init_db()
                config = load_config()
                config.linkedin_profile_url = profile_url
                save_config(config)
                print(f"Profile URL detected and saved: {profile_url}")
            else:
                print("Warning: Could not auto-detect profile URL. Set it with --linkedin-profile-url on import/discover.")
        except Exception as exc:
            _scanner_log("login_error", error=str(exc))
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0
    finally:
        scanner.shutdown()


def cmd_discover(args: argparse.Namespace) -> int:
    from .linkedin import LinkedinScanner

    init_db()
    config = load_config()
    if args.linkedin_profile_url:
        config.linkedin_profile_url = args.linkedin_profile_url
        save_config(config)

    profile_url = config.linkedin_profile_url
    if not profile_url:
        print("Error: No LinkedIn profile URL configured.", file=sys.stderr)
        print("Run 'login' first to auto-detect it, or pass --linkedin-profile-url.", file=sys.stderr)
        return 1

    _scanner_log("discover", limit=args.limit, headless=args.headless, profile_url=profile_url)
    scanner = LinkedinScanner(headless=args.headless)
    try:
        posts = scanner.discover_posts(profile_url, limit=args.limit, exact_dates=args.exact_dates)
        for post in posts:
            upsert_post(post)

        _print_section("Discovery Results")
        print(f"  Discovered: {len(posts)} posts")
        if posts:
            _print_payload({"discovered": len(posts), "posts": posts})
        return 0
    finally:
        scanner.shutdown()


def cmd_scan(args: argparse.Namespace) -> int:
    init_db()
    config = load_config()
    if config.paused and not args.force:
        _scanner_log("scan_skipped", reason="paused")
        print("Scanner is paused. Use 'resume' to unpause.")
        return 0

    _scanner_log("scan", limit=args.limit, force=args.force, headless=args.headless)
    result = _run_scans(config, limit=args.limit, headless=args.headless, force=args.force)
    summary = status_summary()

    _print_section("Scan Results")
    _print_summary_row("Completed scans", result["completed"])
    _print_summary_row("Failed scans", result["failed"])
    _print_summary_row("Pending scans remaining", summary.get("pending_scans"))
    _print_summary_row("Next due at", summary.get("next_due_at"))
    _print_summary_row("Last scan at", summary.get("last_scan_at"))
    if result.get("captures"):
        print(f"\n  -- Capture payloads ({len(result['captures'])} total) --")
        for cap in result["captures"]:
            _print_payload(cap)
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    from .linkedin import LinkedinScanner

    init_db()
    config = load_config()
    if args.linkedin_profile_url:
        config.linkedin_profile_url = args.linkedin_profile_url
        save_config(config)

    profile_url = config.linkedin_profile_url
    if not profile_url:
        print("Error: No LinkedIn profile URL configured.", file=sys.stderr)
        print("Run 'login' first to auto-detect it, or pass --linkedin-profile-url.", file=sys.stderr)
        return 1

    limit = max(1, min(int(args.limit or 50), 50))
    _scanner_log("import", limit=limit, scan_now=args.scan_now, headless=args.headless, profile_url=profile_url)

    scanner = LinkedinScanner(headless=args.headless)
    try:
        posts = scanner.discover_posts(profile_url, limit=limit, exact_dates=True)
        for post in posts:
            upsert_post(post)
        _scanner_log("import_posts_stored", imported=len(posts))

        result: dict[str, Any] = {"completed": 0, "failed": 0, "captures": []}
        if args.scan_now:
            result = _run_scans_with_scanner(scanner, config, limit=limit, force=True)

        summary = status_summary()

        _print_section("Import Results")
        _print_summary_row("Posts imported", len(posts))
        _print_summary_row("Scans completed", result.get("completed", 0))
        _print_summary_row("Scans failed", result.get("failed", 0))
        _print_summary_row("Pending scans", summary.get("pending_scans"))
        _print_summary_row("Next due at", summary.get("next_due_at"))
        _print_summary_row("Last scan at", summary.get("last_scan_at"))

        if result.get("captures"):
            print(f"\n  -- Capture payloads ({len(result['captures'])} total) --")
            for cap in result["captures"]:
                _print_payload(cap)
        return 0
    finally:
        scanner.shutdown()


def cmd_status(args: argparse.Namespace) -> int:
    init_db()
    config = load_config()

    if args.url_only:
        print(config.linkedin_profile_url or "")
        return 0 if config.linkedin_profile_url else 1

    summary = status_summary()

    _print_section("Scanner Status")
    _print_summary_row("Profile URL", config.linkedin_profile_url or "(not set)")
    _print_summary_row("Paused", str(config.paused).lower())
    _print_summary_row("Pending scans", summary.get("pending_scans"))
    _print_summary_row("Queued snapshots", summary.get("queued_snapshots"))
    _print_summary_row("Next due at", summary.get("next_due_at") or "(none)")
    _print_summary_row("Last scan at", summary.get("last_scan_at") or "(never)")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    """Wipe all local scanner data -- database, config, and browser profile."""
    from .config import APP_DIR, CONFIG_PATH, DB_PATH, BROWSER_PROFILE_DIR
    import shutil as _shutil

    if not args.force:
        print("This will permanently delete all local scanner data:")
        print(f"  Database:     {DB_PATH}")
        print(f"  Config:       {CONFIG_PATH}")
        print(f"  Browser data: {BROWSER_PROFILE_DIR}")
        print()
        print("Run with --force to confirm.")
        return 1

    wiped = []
    for path, label in [
        (DB_PATH, "Database"),
        (CONFIG_PATH, "Config"),
        (BROWSER_PROFILE_DIR, "Browser profile"),
    ]:
        try:
            if path.is_file():
                path.unlink()
                wiped.append(label)
            elif path.is_dir():
                _shutil.rmtree(str(path), ignore_errors=True)
                wiped.append(label)
        except Exception as exc:
            print(f"Warning: Could not remove {label}: {exc}")

    _scanner_log("reset", wiped=", ".join(wiped))

    if args.reinit:
        init_db()
        print("Fresh database created.")

    if wiped:
        print("Wiped: " + ", ".join(wiped))
    else:
        print("Nothing to wipe -- already clean.")
    return 0

def cmd_pause(args: argparse.Namespace) -> int:
    config = load_config()
    config.paused = bool(args.paused)
    save_config(config)
    _scanner_log("pause", paused=config.paused)
    print("Scanner paused." if config.paused else "Scanner resumed.")
    return 0


# -- scan engine (no backend) -----------------------------------------

def _run_scans(config: ScannerConfig, *, limit: int, headless: bool, force: bool, delay: bool = False) -> dict[str, Any]:
    from .linkedin import LinkedinScanner

    scanner = LinkedinScanner(headless=headless)
    try:
        scans = due_scans(limit=limit, include_not_due=force)
        _scanner_log("scan_queue", count=len(scans), limit=limit, force=force, headless=headless)

        if not scans:
            return {"completed": 0, "failed": 0, "captures": []}

        posts = [dict(scan) for scan in scans]
        batch_results = scanner.capture_batch(
            posts,
            snapshot_window=scans[0]["snapshot_window"] if scans else "",
        )

        captures: list[dict[str, Any]] = []
        completed = 0
        failed = 0

        # Map results back to scan records by URN
        result_by_urn: dict[str, dict[str, Any]] = {}
        for r in batch_results:
            post_data = r.get("post") if isinstance(r.get("post"), dict) else {}
            urn = str(post_data.get("canonical_urn") or "")
            if urn:
                result_by_urn[urn] = r

        for scan in scans:
            urn = str(scan["canonical_urn"])
            if urn in result_by_urn:
                payload = result_by_urn[urn]
                snapshot_id = record_snapshot(urn, scan["snapshot_window"], payload)
                mark_due_done(int(scan["id"]))
                completed += 1
                captures.append(payload)

                metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
                _scanner_log(
                    "capture_done",
                    snapshot_id=snapshot_id,
                    urn=_short_urn(urn),
                    impressions=metrics.get("impressions"),
                    reactions=metrics.get("reactions"),
                    comments=metrics.get("comments"),
                    reposts=metrics.get("reposts"),
                )
            else:
                # Check if it was skipped (repost) or failed
                fail_reason = f"No result for {urn}"
                mark_due_failed(int(scan["id"]), fail_reason)
                log("error", f"Scan failed for {urn}: {fail_reason}")
                failed += 1
                _scanner_log("capture_failed", urn=_short_urn(urn), error=fail_reason[:200])

        return {"completed": completed, "failed": failed, "captures": captures}
    finally:
        scanner.shutdown()


def _run_scans_with_scanner(scanner, config: ScannerConfig, *, limit: int, force: bool) -> dict[str, Any]:
    """Same as _run_scans but reuses an existing scanner (does NOT create or shutdown)."""
    scans = due_scans(limit=limit, include_not_due=force)
    _scanner_log("scan_queue", count=len(scans), limit=limit, force=force)

    if not scans:
        return {"completed": 0, "failed": 0, "captures": []}

    posts = [dict(scan) for scan in scans]
    batch_results = scanner.capture_batch(
        posts,
        snapshot_window=scans[0]["snapshot_window"] if scans else "",
    )

    captures: list[dict[str, Any]] = []
    completed = 0
    failed = 0

    result_by_urn: dict[str, dict[str, Any]] = {}
    for r in batch_results:
        post_data = r.get("post") if isinstance(r.get("post"), dict) else {}
        urn = str(post_data.get("canonical_urn") or "")
        if urn:
            result_by_urn[urn] = r

    for scan in scans:
        urn = str(scan["canonical_urn"])
        if urn in result_by_urn:
            payload = result_by_urn[urn]
            snapshot_id = record_snapshot(urn, scan["snapshot_window"], payload)
            mark_due_done(int(scan["id"]))
            completed += 1
            captures.append(payload)

            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            _scanner_log(
                "capture_done",
                snapshot_id=snapshot_id,
                urn=_short_urn(urn),
                impressions=metrics.get("impressions"),
                reactions=metrics.get("reactions"),
                comments=metrics.get("comments"),
                reposts=metrics.get("reposts"),
            )
        else:
            fail_reason = f"No result for {urn}"
            mark_due_failed(int(scan["id"]), fail_reason)
            log("error", f"Scan failed for {urn}: {fail_reason}")
            failed += 1
            _scanner_log("capture_failed", urn=_short_urn(urn), error=fail_reason[:200])

    return {"completed": completed, "failed": failed, "captures": captures}


# -- CLI --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="socio-scanner",
        description="Standalone LinkedIn post analytics scanner. "
                    "Discovers posts, captures analytics via XLSX export, "
                    "and outputs structured JSON — all locally, no backend required.",
    )
    parser.add_argument("--version", action="version", version=f"socio-scanner {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init", help="Initialize local scanner database").set_defaults(func=cmd_init)

    login = sub.add_parser("login", help="Open LinkedIn login browser or check session")
    login.add_argument("--check", action="store_true", help="Only check if already logged in")
    login.set_defaults(func=cmd_login)

    discover = sub.add_parser("discover", help="Discover posts from a LinkedIn profile")
    discover.add_argument("--linkedin-profile-url", default="", help="LinkedIn profile URL to scan")
    discover.add_argument("--limit", type=int, default=25, help="Max posts to discover (1-50)")
    discover.add_argument("--headless", action="store_true", help="Run browser headless")
    discover.add_argument("--exact-dates", action="store_true", help="Use exact date selectors")
    discover.set_defaults(func=cmd_discover)

    importer = sub.add_parser("import", help="Discover posts and optionally scan them")
    importer.add_argument("--linkedin-profile-url", default="", help="LinkedIn profile URL to scan")
    importer.add_argument("--limit", type=int, default=50, help="Max posts to import (1-50)")
    importer.add_argument("--headless", action="store_true", help="Run browser headless")
    importer.add_argument("--scan-now", action="store_true", help="Scan analytics immediately after import")
    importer.set_defaults(func=cmd_import)

    scan = sub.add_parser("scan", help="Process due analytics scans")
    scan.add_argument("--limit", type=int, default=5, help="Max scans to run (1-20)")
    scan.add_argument("--headless", action="store_true", help="Run browser headless")
    scan.add_argument("--force", action="store_true", help="Process all scans regardless of due time")
    scan.set_defaults(func=cmd_scan)

    status_p = sub.add_parser("status", help="Show local scanner status")
    status_p.add_argument("--url-only", action="store_true", help="Print only the stored profile URL")
    status_p.set_defaults(func=cmd_status)

    pause = sub.add_parser("pause", help="Pause scanning")
    pause.add_argument("--paused", action="store_true")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="Resume scanning")
    resume.set_defaults(func=lambda _: cmd_pause(argparse.Namespace(paused=False)))


    reset = sub.add_parser("reset", help="Wipe all local scanner data")
    reset.add_argument("--force", action="store_true", help="Skip confirmation prompt")
    reset.add_argument("--reinit", action="store_true", help="Re-initialize fresh database after wipe")
    reset.set_defaults(func=cmd_reset)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
