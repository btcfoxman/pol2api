from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field


class BrowserCookie(BaseModel):
    model_config = ConfigDict(extra="allow")
    name: str
    value: str = ""
    domain: str = ".pollo.ai"
    path: str = "/"


class AccountUpsert(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=120)
    email: str = ""
    cookie_header: str = ""
    cookies: str = ""
    cookie_records: list[BrowserCookie] = Field(default_factory=list)
    project_id: str = ""
    user_agent: str = ""
    proxy_url: str = ""
    profile_dir: str = ""
    cdp_port: int | None = Field(default=None, ge=1024, le=65535)
    enabled: bool = True
    auto_login: bool = False
    max_concurrency: int = Field(default=1, ge=1, le=100)
    use_proxy_pool: bool = True


class AccountSyncRequest(AccountUpsert):
    name: str = ""


class AccountPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = None
    email: str | None = None
    cookie_header: str | None = None
    cookie_records: list[BrowserCookie] | None = None
    project_id: str | None = None
    proxy_url: str | None = None
    profile_dir: str | None = None
    cdp_port: int | None = Field(default=None, ge=1024, le=65535)
    enabled: bool | None = None
    auto_login: bool | None = None
    max_concurrency: int | None = Field(default=None, ge=1, le=100)


class SettingsPatch(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    task_workers: int | None = Field(default=None, ge=1, le=50)
    task_queue_capacity: int | None = Field(default=None, ge=0, le=5000)
    poll_interval_seconds: int | None = Field(default=None, ge=2, le=120)
    task_timeout_seconds: int | None = Field(default=None, ge=60, le=7200)
    synchronous_timeout_seconds: int | None = Field(default=None, ge=1, le=3600)
    request_timeout_seconds: int | None = Field(default=None, ge=10, le=600)
    request_retries: int | None = Field(default=None, ge=0, le=5)
    account_maintenance_interval_seconds: int | None = Field(
        default=None, ge=30, le=86400
    )
    account_maintenance_workers: int | None = Field(default=None, ge=1, le=20)
    low_balance_disable_threshold: float | None = Field(default=None, ge=0)
    proxy_host_override: str | None = None
    proxy_pool: str | None = None
    proxy_pool_enabled: bool | None = None
    browser_recovery_enabled: bool | None = None
    excess_media_policy: Literal["strict", "ignore"] | None = None
    allow_video_reference_inputs: bool | None = None
    prompt_media_reference_cleanup_enabled: bool | None = None
    model_map: str | None = None


class GenerationTaskCreate(BaseModel):
    model_config = ConfigDict(extra="allow", allow_inf_nan=False, strict=True)
    model: str = "sd-2-0-mini"
    prompt: str = ""
    content: list[dict[str, Any]] = Field(default_factory=list)
    image_urls: list[Any] = Field(default_factory=list)
    video_urls: list[Any] = Field(default_factory=list)
    audio_urls: list[Any] = Field(default_factory=list)
    generation_mode: Literal["auto", "reference", "image", "text"] = "auto"
    image_url: str | None = None
    image_tail_url: str | None = None
    mode: str | None = None
    web_search: bool | None = None
    duration: int = Field(default=5, ge=2, le=30)
    resolution: str | None = None
    aspect_ratio: str = "16:9"
    n: int = Field(default=1, ge=1, le=4)
    generate_audio: bool = True
    published: bool = True
    protection_mode: bool = False
    seed: int | None = Field(default=None, ge=0, le=2147483647)
    account_id: int | None = None
