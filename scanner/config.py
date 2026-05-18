from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


APP_DIR = Path(os.environ.get("LINKEDIN_CLI_HOME") or Path(os.environ["LOCALAPPDATA"]) / "LinkedInCLI")
CONFIG_PATH = APP_DIR / "config.json"
DB_PATH = APP_DIR / "scanner.sqlite"
BROWSER_PROFILE_DIR = APP_DIR / "browser-profile"


@dataclass
class ScannerConfig:
    linkedin_profile_url: str = ""
    paused: bool = False


def ensure_app_dirs() -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> ScannerConfig:
    ensure_app_dirs()
    if not CONFIG_PATH.exists():
        return ScannerConfig()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ScannerConfig()
    return ScannerConfig(
        linkedin_profile_url=str(data.get("linkedin_profile_url") or ""),
        paused=bool(data.get("paused") or False),
    )


def save_config(config: ScannerConfig) -> None:
    ensure_app_dirs()
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "linkedin_profile_url": config.linkedin_profile_url,
                "paused": config.paused,
            },
            indent=2,
        ),
        encoding="utf-8",
    )