from dataclasses import dataclass
import json
import os
from urllib.parse import urlsplit, urlunsplit

from app.model_catalog import DEFAULT_MODEL_MAP


def normalize_proxy_url(value: str) -> str:
    value = str(value or "").strip()
    return "socks5://" + value if value and "://" not in value else value


def rewrite_loopback_proxy(value: str, host: str) -> str:
    parsed = urlsplit(normalize_proxy_url(value))
    if host and parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        auth = parsed.netloc.rpartition("@")[0] + "@" if "@" in parsed.netloc else ""
        return urlunsplit(parsed._replace(netloc=f"{auth}{host}:{parsed.port}"))
    return parsed.geturl()


@dataclass
class Settings:
    api_key: str = ""
    admin_token: str = ""
    sync_token: str = ""
    database_path: str = "data/pol2api.db"
    task_workers: int = 5
    task_queue_capacity: int = 100
    poll_interval_seconds: int = 5
    task_timeout_seconds: int = 1800
    synchronous_timeout_seconds: int = 120
    request_timeout_seconds: int = 60
    request_retries: int = 2
    account_maintenance_interval_seconds: int = 300
    account_maintenance_workers: int = 3
    account_default_concurrency: int = 1
    media_timeout_seconds: int = 180
    media_max_bytes: int = 209715200
    browser_recovery_enabled: bool = True
    proxy_host_override: str = ""
    proxy_pool_enabled: bool = False
    proxy_pool: str = ""
    low_balance_disable_threshold: float = 1.0
    excess_media_policy: str = "strict"
    allow_video_reference_inputs: bool = True
    prompt_media_reference_cleanup_enabled: bool = False
    model_map: str = ""
    schema_version: str = "1.0.0"
    upstream_base_url: str = "https://pollo.ai"
    ffprobe_executable: str = "ffprobe"


def load_settings() -> Settings:
    value = Settings()
    for key in value.__dataclass_fields__:
        raw = os.getenv("POL_" + key.upper())
        if raw is None:
            continue
        default = getattr(value, key)
        parsed = (
            raw.lower() in ("1", "true", "yes", "on")
            if isinstance(default, bool)
            else type(default)(raw)
        )
        setattr(value, key, parsed)
    value.model_map = value.model_map or json.dumps(DEFAULT_MODEL_MAP)
    value.admin_token = value.admin_token or value.api_key
    value.sync_token = value.sync_token or value.api_key
    return value


settings = load_settings()
