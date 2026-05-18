from __future__ import annotations

import os as _os
import random as _random
import re
import shutil as _shutil
import tempfile as _tempfile
import time as _time
import warnings as _warnings
from datetime import datetime, timedelta, timezone
from typing import Any

_warnings.filterwarnings("ignore", category=UserWarning, module="openpyxl")

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover - runtime scanner installs Playwright.
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None

from .config import BROWSER_PROFILE_DIR

# ── browser fingerprint hardening ──────────────────────────────────────

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

COMMON_VIEWPORTS = [
    {"width": 1920, "height": 1080},
    {"width": 1536, "height": 864},
    {"width": 1440, "height": 900},
    {"width": 1366, "height": 768},
]

# Chromium flags that disable automation indicators
STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
    "--no-sandbox",
    "--disable-infobars",
    "--disable-dev-shm-usage",
    "--disable-setuid-sandbox",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--disable-sync",
    "--disable-background-networking",
]

# JS evasion script injected before every page load
# Kept minimal — only undetectable overrides that don't break site JS.
STEALTH_INIT_SCRIPT = """
// Remove webdriver detection — the #1 bot flag
Object.defineProperty(navigator, 'webdriver', { get: () => false });

// Fake chrome.runtime (Chrome-only, non-invasive)
window.chrome = { runtime: {} };

// Fake plugins length (headless normally has 0)
Object.defineProperty(navigator, 'plugins', {
    get: () => [1, 2, 3, 4, 5],
});

// Fake languages
Object.defineProperty(navigator, 'languages', {
    get: () => ['en-US', 'en'],
});
"""

# Patterns to detect WAF/challenge pages (LinkedIn, Cloudflare, etc.)
WAF_CHALLENGE_PATTERNS = [
    "verify you are human",
    "checking your browser",
    "one more step",
    "please stand by",
    "are you a real person",
    "security check",
    "captcha",
    "cf-challenge",
    "cf-turnstile",
    "just a moment",
    "access denied",
    "unusual activity",
    "please verify",
]

def _detect_waf_challenge(text: str) -> str | None:
    """Return the matched challenge pattern if the page is a WAF/security gate."""
    text_lower = text.lower()
    for pattern in WAF_CHALLENGE_PATTERNS:
        if pattern in text_lower:
            return pattern
    return None

# ── adaptive rate limiter ──────────────────────────────────────────────

class AdaptiveLimiter:
    """Adaptive pacing: starts at delay_min, decreases on success, backs off on rate limit."""

    def __init__(self, delay_min: float = 1.0, delay_max: float = 120.0):
        self.delay = delay_min
        self.delay_min = delay_min
        self.delay_max = delay_max
        self._success_streak = 0

    def wait(self) -> None:
        import time as _time
        _time.sleep(self.delay)

    def on_success(self) -> None:
        self._success_streak += 1
        if self._success_streak >= 5:
            self.delay = max(self.delay_min, self.delay * 0.8)
            self._success_streak = 0

    def on_rate_limit(self) -> None:
        self._success_streak = 0
        self.delay = min(self.delay_max, self.delay * 2)


URN_RE = re.compile(r"urn:li:activity:\d+")
RELATIVE_TIME_RE = re.compile(r"\b(\d+)\s*(m|min|minute|minutes|h|hr|hour|hours|d|day|days|w|wk|week|weeks|mo|mon|month|months|y|yr|year|years)\b", re.I)

REPOST_PATTERNS = [
    r"\breposted this\b",
    r"\bshared this\b",
    r"\bshared a post\b",
    r"\breshared\b",
]


def _is_repost(card_text: str) -> bool:
    lowered = card_text.lower()
    for pattern in REPOST_PATTERNS:
        if re.search(pattern, lowered):
            return True
    return False


def _compact_number(raw: str | None) -> int | None:
    if not raw:
        return None
    value = raw.replace(",", "").strip().lower()
    match = re.search(r"(\d+(?:\.\d+)?)\s*([kmb])?", value)
    if not match:
        return None
    number = float(match.group(1))
    suffix = match.group(2) or ""
    multiplier = {"k": 1_000, "m": 1_000_000, "b": 1_000_000_000}.get(suffix, 1)
    parsed = int(round(number * multiplier))
    return parsed if parsed >= 0 else None


def _metric(text: str, labels: list[str]) -> int | None:
    for label in labels:
        after = re.search(rf"(\d[\d,.]*\s*[kmb]?)\s+{label}", text, re.I)
        if after:
            return _compact_number(after.group(1))
        before = re.search(rf"{label}\s+(\d[\d,.]*\s*[kmb]?)", text, re.I)
        if before:
            return _compact_number(before.group(1))
    return None


def _post_impressions_metric(text: str) -> int | None:
    impressions = _metric(text, ["impressions?"])
    if impressions is not None:
        return impressions
    normalized = re.sub(r"\s+", " ", str(text or ""))
    for match in re.finditer(r"(\d[\d,.]*\s*[kmb]?)\s+(?:post\s+)?views?\b", normalized, re.I):
        prefix = normalized[max(0, match.start() - 24):match.start()].lower()
        if "profile" not in prefix and "page" not in prefix:
            return _compact_number(match.group(1))
    for match in re.finditer(r"\b(?:post\s+)?views?\s+(\d[\d,.]*\s*[kmb]?)", normalized, re.I):
        prefix = normalized[max(0, match.start() - 24):match.start()].lower()
        if "profile" not in prefix and "page" not in prefix:
            return _compact_number(match.group(1))
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_relative_time(raw: str | None) -> tuple[str | None, str]:
    text = str(raw or "").strip()
    if not text:
        return None, ""
    match = RELATIVE_TIME_RE.search(text)
    if not match:
        return None, ""
    amount = int(match.group(1))
    unit = match.group(2).lower()
    if unit.startswith("m") and unit not in {"mo", "mon", "month", "months"}:
        delta = timedelta(minutes=amount)
    elif unit.startswith("h"):
        delta = timedelta(hours=amount)
    elif unit.startswith("d"):
        delta = timedelta(days=amount)
    elif unit.startswith("w"):
        delta = timedelta(weeks=amount)
    elif unit in {"mo", "mon", "month", "months"}:
        delta = timedelta(days=amount * 30)
    else:
        delta = timedelta(days=amount * 365)
    return _iso(datetime.now(timezone.utc) - delta), match.group(0)


def _parse_publish_time(item: dict[str, Any], post_text: str) -> tuple[str | None, str]:
    datetime_value = str(item.get("datetime") or "").strip()
    if datetime_value:
        try:
            return _iso(datetime.fromisoformat(datetime_value.replace("Z", "+00:00"))), datetime_value
        except ValueError:
            pass

    time_text = str(item.get("timeText") or "").strip()
    if time_text:
        parsed, raw = _parse_relative_time(time_text)
        if parsed:
            return parsed, raw

    card_text = str(item.get("cardText") or "")
    first_post_line = next((line.strip() for line in post_text.splitlines() if line.strip()), "")
    metadata_text = card_text
    if first_post_line and first_post_line in card_text:
        metadata_text = card_text.split(first_post_line, 1)[0]
    parsed, raw = _parse_relative_time(metadata_text[:500])
    return parsed, raw


POST_BODY_START_KEYS = {"feed post"}
POST_BODY_STOP_KEYS = {
    "be the first to comment",
    "about",
    "accessibility",
    "help center",
    "privacy terms",
    "ad choices",
    "advertising",
    "business services",
    "get the linkedin app",
    "more",
    "linkedin corporation 2026",
}
POST_BODY_NOISE_KEYS = {
    "premium",
    "you",
    "tahir nawab",
    "tahir nawab tahir nawab",
    "like",
    "comment",
    "repost",
    "send",
    "share",
    "view analytics",
    "home",
    "my network",
    "jobs",
    "messaging",
    "notifications",
    "me",
    "for business",
    "learning",
    "karachi division sindh",
    "experience",
    "connections",
    "grow your network",
    "your premium features",
    "skip to main content",
    "more",
    "...more",
}


def _noise_key(line: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", line.lower()).strip()


def _raw_lines(text: str) -> list[str]:
    return [re.sub(r"\s+", " ", raw_line).strip(" Ã¢â‚¬Â¢") for raw_line in str(text or "").splitlines()]


def _post_body_window(lines: list[str]) -> list[str]:
    start_index = 0
    for index, line in enumerate(lines):
        if _noise_key(line) in POST_BODY_START_KEYS:
            start_index = index + 1
            break

    window: list[str] = []
    for line in lines[start_index:]:
        if _noise_key(line) in POST_BODY_STOP_KEYS:
            break
        window.append(line)
    return window


def _clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in _post_body_window(_raw_lines(text)):
        lowered = line.lower()
        noise_key = _noise_key(line)
        if not line:
            continue
        if lowered.startswith("feed post number"):
            content_match = re.search(r"\b(the|if|most|a|an|why|how|when|what)\b.+", line, re.I)
            if content_match:
                lines.append(content_match.group(0)[:1000])
            continue
        if noise_key in POST_BODY_NOISE_KEYS:
            continue
        if "there was a problem processing your payment" in lowered:
            continue
        if re.fullmatch(r"[-â€“â€”â€¢Â·.\s?]+", line):
            continue
        if re.fullmatch(r"\d+", line):
            continue
        if "visible to anyone on or off linkedin" in lowered:
            continue
        if lowered.startswith("premium ") or lowered.endswith(" premium"):
            continue
        if lowered.startswith("activate to view"):
            continue
        if re.search(r"\b\d[\d,.]*\s*(k|m)?\s+impressions?\b", lowered):
            continue
        if re.fullmatch(r"(you\s*)?[-â€“]?\s*(\d+\s*)?(m|h|d|w|mo|yr|y)\b.*", lowered):
            continue
        lines.append(line)
    return lines


def _first_sentence(text: str) -> str:
    cleaned = " ".join(_clean_lines(text))
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return (parts[0] if parts else cleaned)[:1000]


def _media_type(text: str) -> str:
    lowered = text.lower()
    if "poll closed" in lowered or "vote" in lowered:
        return "unknown"
    if "document" in lowered or "pdf" in lowered:
        return "document"
    if "carousel" in lowered:
        return "carousel"
    if "video" in lowered:
        return "video"
    if "image" in lowered or "photo" in lowered:
        return "image"
    return "text"


def enrich_post_fields(post: dict[str, Any]) -> dict[str, Any]:
    """Normalize raw visible post fields without doing strategic analysis.

    Hook/CTA/topic/body-structure labels belong to the backend Insight_tracker
    pass. The scanner is intentionally only a camera: it keeps the visible body,
    a display title, timing, URL/URN, and media facts.
    """
    text = str(post.get("post_text") or "")
    lines = _clean_lines(text)
    media_type = str(post.get("media_type") or _media_type(text))
    body_text = "\n".join(lines) if lines else text
    title = str(post.get("post_title") or (lines[0] if lines else _first_sentence(text)) or "Needs post body rescan")
    enriched = dict(post)
    enriched.update({
        "post_title": title[:300],
        "post_text": body_text[:12000],
        "body_text": str(post.get("body_text") or body_text)[:12000],
        "hook_text": "",
        "hook_type": "",
        "cta_text": "",
        "cta_type": "",
        "topic": "",
        "format": "",
        "body_structure": "",
        "media_type": media_type,
    })
    return enriched


def analytics_url(canonical_urn: str) -> str:
    return f"https://www.linkedin.com/analytics/post-summary/{canonical_urn}/"


def _short_urn(value: Any) -> str:
    text = str(value or "")
    return text[-12:] if len(text) > 12 else text


def _scanner_log(stage: str, **fields: Any) -> None:
    parts = [f"[LinkedinCLI] {stage}"]
    for key, value in fields.items():
        if value is None or value == "":
            continue
        safe_value = str(value).replace("\n", " ")[:180]
        parts.append(f"{key}={safe_value}")
    print(" ".join(parts), flush=True)


# ── LinkedinScanner with persistent browser session ───────────────────

class LinkedinScanner:
    """Scanner with persistent browser context.

    The browser launches ONCE via _ensure_context() and stays alive until
    shutdown() is called.  The first browser tab becomes the activity page
    (home base).  Analytics tabs are opened as needed and closed after
    processing so the activity tab remains the anchor.

    Scroll-based discovery walks the activity feed in batches of up to 7
    posts.  Each post URN is extracted from the activity DOM, then its
    analytics page is opened in a fresh tab, XLSX-exported, and parsed.
    """

    def __init__(self, headless: bool = False):
        self.headless = headless
        self.limiter = AdaptiveLimiter(delay_min=3.0)
        self._playwright = None
        self._context = None
        self._activity_page = None

    # ── browser lifecycle ───────────────────────────────────────────

    def _ensure_context(self):
        if self._playwright is not None and self._context is not None:
            try:
                self._context.pages
                return self._playwright, self._context
            except Exception:
                self._playwright = None
                self._context = None
                self._activity_page = None

        if sync_playwright is None:
            raise RuntimeError("Playwright is required for LinkedIn scanner browser commands.")

        self._playwright = sync_playwright().start()
        viewport = _random.choice(COMMON_VIEWPORTS)
        self._context = self._playwright.chromium.launch_persistent_context(
            str(BROWSER_PROFILE_DIR),
            headless=self.headless,
            viewport=viewport,
            args=STEALTH_ARGS,
            bypass_csp=True,
        )
        self._context.add_init_script(STEALTH_INIT_SCRIPT)

        pages = self._context.pages
        if pages:
            self._activity_page = pages[0]
        else:
            self._activity_page = self._context.new_page()

        _scanner_log("browser_started", headless=self.headless,
                      viewport=f"{viewport['width']}x{viewport['height']}")
        return self._playwright, self._context

    def _get_activity_page(self):
        self._ensure_context()
        if self._activity_page is None or self._activity_page.is_closed():
            self._activity_page = self._context.new_page()
        return self._activity_page

    def shutdown(self):
        if self._activity_page and not self._activity_page.is_closed():
            try:
                self._activity_page.close()
            except Exception:
                pass
        self._activity_page = None

        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
        self._context = None

        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
        self._playwright = None
        _scanner_log("browser_shutdown")

    def _random_delay(self, min_s: float = 0.3, max_s: float = 1.5) -> None:
        _time.sleep(_random.uniform(min_s, max_s))

    # ── login / session check ───────────────────────────────────────

    def login_check(self) -> bool:
        self._ensure_context()
        page = self._get_activity_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60_000)
        text = page.locator("body").inner_text(timeout=10_000)
        return "Sign in" not in text and "Join LinkedIn" not in text

    def open_login(self) -> str | None:
        self._ensure_context()
        page = self._get_activity_page()
        page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=60_000)
        print("LinkedIn login browser is open. Sign in, then come back here.")
        input("Press Enter here after you have logged into LinkedIn...")

        try:
            self._context.pages
        except Exception:
            _scanner_log("login_browser_closed_by_user", action="recreate_context")
            self._activity_page = None
            self._ensure_context()
            page = self._get_activity_page()

        profile_url = None
        try:
            page.goto("https://www.linkedin.com/in/me/", wait_until="domcontentloaded", timeout=30_000)
            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                pass
        except PlaywrightTimeoutError:
            pass
        except Exception:
            pass

        try:
            current_url = page.url
            match = re.match(r"(https?://[^/]+/in/[^/?]+)", current_url)
            if match:
                profile_url = match.group(1)
        except Exception:
            profile_url = None

        return profile_url

    # ── helpers for tab management ──────────────────────────────────

    def _close_extra_tabs(self, keep_open) -> None:
        for p in self._context.pages:
            if p is not keep_open and not p.is_closed():
                try:
                    p.close()
                except Exception:
                    pass

    # ── XLSX export download ────────────────────────────────────────

    def _download_export_xlsx(self, page, timeout_ms: int = 30_000) -> str | None:
        export_selectors = [
            'button:has-text("Export")',
            '[aria-label*="Export"]',
            'button:has-text("Export as XLSX")',
        ]
        btn = None
        for selector in export_selectors:
            try:
                candidate = page.locator(selector).first
                if candidate.is_visible(timeout=2000):
                    btn = candidate
                    break
            except Exception:
                continue

        if btn is None:
            try:
                more_btn = page.locator('[aria-label="More actions"], button:has-text("...")').first
                if more_btn.is_visible(timeout=2000):
                    more_btn.click()
                    page.wait_for_timeout(1000)
            except Exception:
                pass
            for selector in export_selectors:
                try:
                    candidate = page.locator(selector).first
                    if candidate.is_visible(timeout=2000):
                        btn = candidate
                        break
                except Exception:
                    continue

        if btn is None:
            return None

        tmp_dir = _tempfile.mkdtemp(prefix="linkedin_cli_xlsx_")
        tmp_path = _os.path.join(tmp_dir, "analytics_export.xlsx")

        try:
            with page.expect_download(timeout=timeout_ms) as download_info:
                btn.click()
            download = download_info.value
            download.save_as(tmp_path)
            return tmp_path
        except PlaywrightTimeoutError:
            return None

    # ── analytics tab processing ────────────────────────────────────

    ANALYTICS_ERROR_PATTERNS = [
        "analytics failed to load",
        "analytics not available",
        "refresh the page to load analytics",
        "try reloading the page",
    ]

    def _process_analytics_tab(self, tab, urn: str) -> dict | None:
        from .xlsx_parser import parse_linkedin_export_xlsx

        try:
            tab.wait_for_load_state("networkidle", timeout=30_000)
        except PlaywrightTimeoutError:
            pass

        text = tab.locator("body").inner_text(timeout=10_000)
        content_lower = tab.content().lower()
        text_lower = text.lower()

        if any(p in text_lower or p in content_lower for p in self.ANALYTICS_ERROR_PATTERNS):
            raise RuntimeError("SKIP_REPOST: LinkedIn has no analytics for reposted/shared content.")

        tmp_path = self._download_export_xlsx(tab)
        if tmp_path is None:
            raise RuntimeError("Could not download LinkedIn analytics export.")

        try:
            parsed = parse_linkedin_export_xlsx(tmp_path)
            if not parsed or parsed.get("impressions") is None:
                raise RuntimeError("Failed to parse LinkedIn analytics XLSX export.")

            impressions = parsed.get("impressions")
            reactions = parsed.get("reactions")
            comments = parsed.get("comments")
            reposts = parsed.get("reposts")
            members_reached = parsed.get("members_reached")
            profile_viewers = parsed.get("profile_viewers")
            followers_gained = parsed.get("followers_gained")
            saves = parsed.get("saves")
            sends = parsed.get("sends")
            demographics = parsed.get("demographics", [])

            known_engagements = sum(v or 0 for v in (reactions, comments, reposts))
            engagement_rate = (
                known_engagements / impressions
                if impressions > 0 and all(v is not None for v in (reactions, comments, reposts))
                else None
            )

            optional_metrics = {
                "members_reached": members_reached,
                "profile_viewers": profile_viewers,
                "followers_gained": followers_gained,
                "saves": saves,
                "sends": sends,
            }
            missing_optional = [k for k, v in optional_metrics.items() if v is None]
            warnings = []
            if missing_optional:
                warnings.append("Export missing optional fields: " + ", ".join(missing_optional))

            return {
                "urn": urn,
                "schema_version": "linkedin_cli_snapshot_v2",
                "capture": {
                    "capture_mode": "export",
                    "capture_timestamp": _now_iso(),
                    "extractor_version": "local-scanner-0.2.0",
                    "source": "local_scanner",
                    "confidence": 0.98 if not warnings else 0.90,
                    "required_fields": ["impressions", "reactions", "comments", "reposts"],
                    "missing_fields": [],
                    "warnings": warnings,
                    "valid": True,
                },
                "metrics": {
                    "impressions": impressions,
                    "reactions": reactions,
                    "comments": comments,
                    "reposts": reposts,
                    "profile_clicks": profile_viewers,
                    "follower_count_at_capture": followers_gained,
                    "engagements": known_engagements,
                    "engagement_rate": engagement_rate,
                    "members_reached": members_reached,
                    "saves": saves,
                    "sends": sends,
                },
                "demographics": demographics,
                "post_meta": {
                    "top_job_title": parsed.get("top_job_title") or "",
                    "top_location": parsed.get("top_location") or "",
                    "top_industry": parsed.get("top_industry") or "",
                },
            }
        finally:
            try:
                _shutil.rmtree(_os.path.dirname(tmp_path), ignore_errors=True)
            except Exception:
                pass

    # ── scroll-based discovery ──────────────────────────────────────

    def _extract_activity_cards(self, page) -> list[dict]:
        return page.locator("[data-urn*='urn:li:activity']").evaluate_all(
            """nodes => nodes.map(n => {
                const textEl = n.querySelector('.update-components-text, .feed-shared-inline-show-more-text');
                const timeEl = n.querySelector('time');
                const analyticsLink = n.querySelector('a[href*="analytics/post-summary"]');
                return {
                    urn: n.getAttribute('data-urn') || '',
                    text: textEl ? textEl.innerText : '',
                    cardText: n.innerText || '',
                    timeText: timeEl ? timeEl.innerText : '',
                    datetime: timeEl ? (timeEl.getAttribute('datetime') || '') : '',
                    analyticsHref: analyticsLink ? analyticsLink.href : ''
                };
            })"""
        )

    def _goto_activity(self, profile_url: str) -> None:
        activity = self._get_activity_page()
        url = profile_url.rstrip("/")
        if "/recent-activity" not in url:
            url = f"{url}/recent-activity/all/"
        activity.goto(url, wait_until="domcontentloaded", timeout=60_000)
        try:
            activity.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            pass

        text = activity.locator("body").inner_text(timeout=10_000)
        waf_hit = _detect_waf_challenge(text)
        if waf_hit:
            raise RuntimeError(
                f"WAF_CHALLENGE: LinkedIn served a security challenge page "
                f"(detected '{waf_hit}'). Page snippet: {text[:200].replace(chr(10), ' ')}"
            )
        if re.search(r"sign in|join linkedin", text, re.I):
            raise RuntimeError("LinkedIn login is required in the scanner browser profile.")

    # ── post discovery (backward-compatible API) ────────────────────

    def discover_posts(self, profile_url: str, limit: int = 25,
                       exact_dates: bool = False) -> list[dict]:
        self._ensure_context()
        self._goto_activity(profile_url)

        posts: list[dict] = []
        seen: set[str] = set()
        scrolls_without_new = 0

        while len(posts) < limit and scrolls_without_new < 5:
            cards = self._extract_activity_cards(self._activity_page)

            new_in_view = 0
            for card in cards:
                urn = card.get("urn", "")
                if not urn or urn in seen:
                    continue
                seen.add(urn)
                card_text = str(card.get("cardText") or "")
                if _is_repost(card_text):
                    continue
                new_in_view += 1

                text = str(card.get("text") or "").strip()
                published_at, published_at_raw = _parse_publish_time(card, text)
                post = enrich_post_fields({
                    "canonical_urn": urn,
                    "canonical_url": f"https://www.linkedin.com/feed/update/{urn}/",
                    "post_text": text[:12000],
                    "published_at": published_at or _now_iso(),
                    "published_at_raw": published_at_raw,
                    "discovered_at": _now_iso(),
                })
                posts.append(post)
                if len(posts) >= limit:
                    break

            if new_in_view == 0:
                scrolls_without_new += 1
            else:
                scrolls_without_new = 0

            if len(posts) < limit and scrolls_without_new < 5:
                delta = _random.randint(600, 1400)
                self._activity_page.mouse.wheel(0, delta)
                self._random_delay(1.0, 2.5)

        _scanner_log("discover_done", found=len(posts), limit=limit)
        return posts

    # ── single-post analytics capture (backward-compatible) ─────────

    def capture_analytics(self, post: dict, snapshot_window: str) -> dict:
        canonical_urn = str(post["canonical_urn"])
        self._ensure_context()

        tab = self._context.new_page()
        tmp_path = None
        try:
            tab.goto(analytics_url(canonical_urn), wait_until="domcontentloaded", timeout=60_000)
            try:
                tab.wait_for_load_state("networkidle", timeout=30_000)
            except PlaywrightTimeoutError:
                pass

            text = tab.locator("body").inner_text(timeout=10_000)
            if re.search(r"sign in|join linkedin", text, re.I):
                raise RuntimeError("LinkedIn login is required in the scanner browser profile.")

            waf_hit = _detect_waf_challenge(text)
            if waf_hit:
                raise RuntimeError(
                    f"WAF_CHALLENGE: LinkedIn served a security challenge page "
                    f"(detected '{waf_hit}'). Page snippet: {text[:200].replace(chr(10), ' ')}"
                )

            result = self._process_analytics_tab(tab, canonical_urn)
            if result is None:
                raise RuntimeError(f"Failed to capture analytics for {canonical_urn}")

            result["capture"]["snapshot_window"] = snapshot_window
            result["post"] = {
                "canonical_url": post.get("canonical_url") or f"https://www.linkedin.com/feed/update/{canonical_urn}/",
                "canonical_urn": canonical_urn,
                "author": post.get("author") or "",
                "post_title": post.get("post_title") or "",
                "post_text": post.get("post_text") or "",
                "body_text": post.get("body_text") or post.get("post_text") or "",
                "hook_text": "",
                "hook_type": "",
                "cta_text": "",
                "cta_type": "",
                "topic": "",
                "format": "",
                "body_structure": "",
                "published_at_raw": post.get("published_at_raw") or "",
                "published_at": post.get("published_at"),
                "media_type": post.get("media_type") or "",
                "post_type": post.get("media_type") or "unknown",
                "top_job_title": result["post_meta"].get("top_job_title") or "",
                "top_location": result["post_meta"].get("top_location") or "",
                "top_industry": result["post_meta"].get("top_industry") or "",
            }
            result["device"] = {"runtime": "playwright", "platform": "windows"}
            del result["urn"]
            del result["post_meta"]
            return result
        finally:
            if tmp_path:
                try:
                    _shutil.rmtree(_os.path.dirname(tmp_path), ignore_errors=True)
                except Exception:
                    pass
            if tab and not tab.is_closed():
                tab.close()

    # ── batch capture (the new workhorse) ───────────────────────────

    def capture_batch(self, posts: list[dict],
                      snapshot_window: str = "") -> list[dict]:
        self._ensure_context()
        batch_size = 7
        results: list[dict] = []

        for i in range(0, len(posts), batch_size):
            batch = posts[i:i + batch_size]

            tabs = []
            for post in batch:
                urn = str(post["canonical_urn"])
                self.limiter.wait()
                tab = self._context.new_page()
                try:
                    tab.goto(analytics_url(urn), wait_until="domcontentloaded", timeout=60_000)
                    self._random_delay(0.5, 1.5)
                    tabs.append((post, tab))
                except Exception as e:
                    _scanner_log("tab_open_failed", urn=_short_urn(urn), error=str(e)[:120])
                    if tab and not tab.is_closed():
                        tab.close()

            for post, tab in tabs:
                urn = str(post["canonical_urn"])
                try:
                    result = self._process_analytics_tab(tab, urn)
                    if result is None:
                        raise RuntimeError(f"No analytics result for {urn}")

                    result["capture"]["snapshot_window"] = snapshot_window or _now_iso()
                    result["post"] = {
                        "canonical_url": post.get("canonical_url") or f"https://www.linkedin.com/feed/update/{urn}/",
                        "canonical_urn": urn,
                        "author": post.get("author") or "",
                        "post_title": post.get("post_title") or "",
                        "post_text": post.get("post_text") or "",
                        "body_text": post.get("body_text") or post.get("post_text") or "",
                        "hook_text": "",
                        "hook_type": "",
                        "cta_text": "",
                        "cta_type": "",
                        "topic": "",
                        "format": "",
                        "body_structure": "",
                        "published_at_raw": post.get("published_at_raw") or "",
                        "published_at": post.get("published_at"),
                        "media_type": post.get("media_type") or "",
                        "post_type": post.get("media_type") or "unknown",
                        "top_job_title": result["post_meta"].get("top_job_title") or "",
                        "top_location": result["post_meta"].get("top_location") or "",
                        "top_industry": result["post_meta"].get("top_industry") or "",
                    }
                    result["device"] = {"runtime": "playwright", "platform": "windows"}
                    del result["urn"]
                    del result["post_meta"]
                    results.append(result)
                    self.limiter.on_success()
                except Exception as e:
                    err_msg = str(e)
                    if "SKIP_REPOST" in err_msg:
                        _scanner_log("capture_skipped", urn=_short_urn(urn), reason="repost_no_analytics")
                    elif "WAF_CHALLENGE" in err_msg or "rate limit" in err_msg.lower():
                        self.limiter.on_rate_limit()
                        _scanner_log("rate_limit_backoff", delay=round(self.limiter.delay, 1),
                                      urn=_short_urn(urn))
                    else:
                        _scanner_log("capture_failed", urn=_short_urn(urn), error=err_msg[:200])
                finally:
                    if tab and not tab.is_closed():
                        tab.close()

            _scanner_log("batch_complete", batch_start=i, batch_size=len(batch),
                          results=len(results))

        return results

    # ── integrated discover + capture ───────────────────────────────

    def discover_and_capture(self, profile_url: str, limit: int = 25) -> list[dict]:
        self._ensure_context()
        self._goto_activity(profile_url)

        all_results: list[dict] = []
        seen: set[str] = set()
        scrolls_without_new = 0
        pending: list[dict] = []

        while len(all_results) < limit and scrolls_without_new < 5:
            cards = self._extract_activity_cards(self._activity_page)

            new_in_view = 0
            for card in cards:
                urn = card.get("urn", "")
                if not urn or urn in seen:
                    continue
                seen.add(urn)
                card_text = str(card.get("cardText") or "")
                if _is_repost(card_text):
                    continue
                new_in_view += 1

                text = str(card.get("text") or "").strip()
                published_at, published_at_raw = _parse_publish_time(card, text)
                pending.append(enrich_post_fields({
                    "canonical_urn": urn,
                    "canonical_url": f"https://www.linkedin.com/feed/update/{urn}/",
                    "post_text": text[:12000],
                    "published_at": published_at or _now_iso(),
                    "published_at_raw": published_at_raw,
                    "discovered_at": _now_iso(),
                }))

            if new_in_view == 0:
                scrolls_without_new += 1
            else:
                scrolls_without_new = 0

            while len(pending) >= 7 and len(all_results) < limit:
                batch = pending[:7]
                pending = pending[7:]
                captured = self.capture_batch(batch)
                all_results.extend(captured)

            if len(all_results) < limit and scrolls_without_new < 5:
                delta = _random.randint(600, 1400)
                self._activity_page.mouse.wheel(0, delta)
                self._random_delay(1.0, 2.5)

        if pending and len(all_results) < limit:
            remaining = pending[:limit - len(all_results)]
            captured = self.capture_batch(remaining)
            all_results.extend(captured)

        self._close_extra_tabs(self._activity_page)
        _scanner_log("discover_and_capture_done", captured=len(all_results), limit=limit)
        return all_results
