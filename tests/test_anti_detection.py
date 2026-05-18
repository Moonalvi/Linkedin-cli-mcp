from scanner.linkedin import (
    CHROME_UA,
    STEALTH_ARGS,
    STEALTH_INIT_SCRIPT,
    COMMON_VIEWPORTS,
    WAF_CHALLENGE_PATTERNS,
    AdaptiveLimiter,
    _detect_waf_challenge,
)


def test_chrome_ua_format():
    """UA must impersonate Chrome 131+ on Windows."""
    assert "Chrome/" in CHROME_UA
    assert "Windows NT" in CHROME_UA
    assert "AppleWebKit" in CHROME_UA
    assert len(CHROME_UA) > 80


def test_stealth_args_critical_flags():
    """Critical anti-detection flags must be present."""
    assert "--disable-blink-features=AutomationControlled" in STEALTH_ARGS


def test_init_script_fakes_webdriver():
    """The injected JS must remove navigator.webdriver."""
    assert "navigator, 'webdriver'" in STEALTH_INIT_SCRIPT
    assert "get: () => false" in STEALTH_INIT_SCRIPT


def test_init_script_fakes_chrome():
    """The injected JS must define window.chrome.runtime."""
    assert "window.chrome" in STEALTH_INIT_SCRIPT
    assert "runtime" in STEALTH_INIT_SCRIPT


def test_viewport_pool():
    """Viewports must be realistic common desktop resolutions."""
    assert len(COMMON_VIEWPORTS) >= 3
    for vp in COMMON_VIEWPORTS:
        assert vp["width"] >= 1024
        assert vp["height"] >= 720


def test_waf_challenge_detection():
    """Should detect known WAF/challenge sentinels in page text."""
    assert _detect_waf_challenge("Just a moment... verifying") == "just a moment"
    assert _detect_waf_challenge("Please verify you are human") == "verify you are human"
    assert _detect_waf_challenge("Security check required") == "security check"
    assert _detect_waf_challenge("Normal LinkedIn page") is None
    assert _detect_waf_challenge("") is None


def test_adaptive_limiter_starts_at_min():
    limiter = AdaptiveLimiter(delay_min=2.0)
    assert limiter.delay == 2.0


def test_adaptive_limiter_decreases_on_success():
    limiter = AdaptiveLimiter(delay_min=1.0)
    limiter.delay = 3.0  # start above min
    for _ in range(5):
        limiter.on_success()
    assert limiter.delay < 3.0  # decreased from 3.0 after 5-streak


def test_adaptive_limiter_backs_off():
    limiter = AdaptiveLimiter(delay_min=1.0)
    initial = limiter.delay
    limiter.on_rate_limit()
    assert limiter.delay == initial * 2


def test_adaptive_limiter_respects_max():
    limiter = AdaptiveLimiter(delay_min=1.0, delay_max=5.0)
    for _ in range(10):
        limiter.on_rate_limit()
    assert limiter.delay <= 5.0


def test_waf_patterns_are_lowercase():
    """All patterns must be lowercase for case-insensitive matching."""
    for pattern in WAF_CHALLENGE_PATTERNS:
        assert pattern == pattern.lower()