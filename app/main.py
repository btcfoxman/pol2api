from __future__ import annotations

import base64
import json
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from fastapi import (
    Body,
    Cookie,
    Depends,
    FastAPI,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import Database
from app.schemas import (
    AccountPatch,
    AccountSyncRequest,
    AccountUpsert,
    GenerationTaskCreate,
    SettingsPatch,
)
from app.service import PolService


BASE_DIR = Path(__file__).resolve().parent
database = Database(settings.database_path, settings.account_default_concurrency)
service = PolService(database, settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if not settings.api_key or not settings.admin_token:
        raise RuntimeError("请配置 POL_API_KEY 和 POL_ADMIN_TOKEN 后启动")
    service.start()
    try:
        yield
    finally:
        service.stop()


app = FastAPI(
    title="POL2API",
    version=settings.schema_version,
    description="Pollo Seedance protocol gateway",
    lifespan=lifespan,
)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


def _admin_token(pol_admin: str | None = Cookie(default=None)) -> None:
    if not pol_admin or not secrets.compare_digest(pol_admin, settings.admin_token):
        raise HTTPException(status_code=401, detail="admin login required")


def _api_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    token = str(x_api_key or "").strip()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token or not secrets.compare_digest(token, settings.api_key):
        raise HTTPException(status_code=401, detail="invalid API key")


def _sync_token(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> None:
    token = str(x_api_key or "").strip()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    valid = any(
        token and secrets.compare_digest(token, expected)
        for expected in {settings.api_key, settings.sync_token}
        if expected
    )
    if not valid:
        raise HTTPException(status_code=401, detail="invalid account sync token")


def _detail(exc: Exception) -> HTTPException:
    if isinstance(exc, KeyError):
        return HTTPException(status_code=404, detail=str(exc).strip("'"))
    if isinstance(exc, (ValueError, IndexError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=getattr(exc, "status_code", 500), detail=str(exc))


def _task_or_404(task_id: str) -> dict[str, Any]:
    task = database.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    return task


def _create(payload: dict[str, Any], *, synchronous: bool = False) -> dict[str, Any]:
    try:
        validated = GenerationTaskCreate.model_validate(payload).model_dump(
            exclude_unset=True, exclude_none=True
        )
        task = service.create_task(validated, caller_request=payload)
        if synchronous:
            task = service.wait_task(str(task["id"]))
        return task
    except Exception as exc:
        raise _detail(exc) from exc


def _responses_input(payload: dict[str, Any]) -> dict[str, Any]:
    result = dict(payload)
    source = payload.get("input")
    if isinstance(source, str):
        result["prompt"] = source
        return result
    prompt: list[str] = []
    content: list[dict[str, Any]] = list(payload.get("content") or [])
    messages = source if isinstance(source, list) else []
    for message in messages:
        if isinstance(message, str):
            prompt.append(message)
            continue
        if not isinstance(message, dict):
            continue
        value = message.get("content")
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, str):
                prompt.append(item)
            elif isinstance(item, dict):
                kind = str(item.get("type") or "")
                if "text" in kind:
                    prompt.append(str(item.get("text") or item.get("input_text") or ""))
                else:
                    content.append(item)
    result["prompt"] = "\n".join(value for value in prompt if value).strip()
    result["content"] = content
    return result


def _response_object(task: dict[str, Any]) -> dict[str, Any]:
    public = service.public_task(task)
    urls = [item["url"] for item in public.get("data") or []]
    value = {
        "id": task["id"],
        "object": "response",
        "created_at": task.get("created_at"),
        "status": "completed"
        if task.get("status") == "succeeded"
        else task.get("status"),
        "model": task.get("model"),
        "output": [
            {
                "id": f"video_{task['id']}",
                "type": "video_generation_call",
                "status": "completed",
                "video_url": url,
            }
            for url in urls
        ],
    }
    if public.get("error"):
        value["error"] = public["error"]
    return value


@app.exception_handler(json.JSONDecodeError)
def json_error(_: Request, exc: json.JSONDecodeError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": f"invalid JSON: {exc.msg}"})


@app.get("/health")
def health() -> dict[str, Any]:
    return {"status": "ok", **service.status()}


@app.get("/login")
def login_page() -> Response:
    html = """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><link rel=\"icon\" href=\"data:,\"><title>POL2API 登录</title><link rel=\"stylesheet\" href=\"/static/styles.css\"></head><body class=\"login-page\"><form class=\"login-panel\" method=\"post\" action=\"/login\"><div class=\"brand-mark\">POL</div><p class=\"eyebrow\">POLLO PROTOCOL GATEWAY</p><h1>POL2API</h1><input name=\"username\" value=\"admin\" autocomplete=\"username\" hidden><label>管理密钥<input name=\"token\" type=\"password\" autofocus required autocomplete=\"current-password\"></label><button class=\"button primary\" type=\"submit\">登录控制台</button></form></body></html>"""
    return Response(html, media_type="text/html")


@app.post("/login")
def login(token: str = Form(...)) -> Response:
    if not secrets.compare_digest(token, settings.admin_token):
        return RedirectResponse("/login?error=1", status_code=303)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        "pol_admin", token, httponly=True, samesite="lax", max_age=86400 * 14
    )
    return response


@app.post("/logout")
def logout() -> Response:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("pol_admin")
    return response


@app.get("/")
def dashboard(pol_admin: str | None = Cookie(default=None)) -> Response:
    if not pol_admin or not secrets.compare_digest(pol_admin, settings.admin_token):
        return RedirectResponse("/login", status_code=303)
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/api/settings", dependencies=[Depends(_admin_token)])
def admin_settings() -> dict[str, Any]:
    return service.runtime_settings()


@app.patch("/api/settings", dependencies=[Depends(_admin_token)])
def patch_settings(payload: SettingsPatch) -> dict[str, Any]:
    try:
        return service.update_runtime_settings(
            payload.model_dump(exclude_none=True, exclude_unset=True)
        )
    except Exception as exc:
        raise _detail(exc) from exc


@app.get("/api/accounts", dependencies=[Depends(_admin_token)])
def accounts() -> list[dict[str, Any]]:
    return database.list_accounts(include_secrets=False)


@app.post("/api/accounts", dependencies=[Depends(_admin_token)])
def add_account(payload: AccountUpsert) -> dict[str, Any]:
    try:
        value = payload.model_dump()
        return service.upsert_account(value, start_login=bool(value.get("auto_login")))
    except Exception as exc:
        raise _detail(exc) from exc


@app.post("/api/accounts/sync", dependencies=[Depends(_sync_token)])
def sync_account(payload: AccountSyncRequest) -> dict[str, Any]:
    try:
        return service.sync_account(payload.model_dump())
    except Exception as exc:
        raise _detail(exc) from exc


@app.post("/api/accounts/batch-import", dependencies=[Depends(_admin_token)])
def batch_import(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        raw_accounts = payload.get("accounts")
        if isinstance(raw_accounts, list):
            source: str | list[dict[str, Any]] = [
                AccountUpsert.model_validate(
                    {
                        **item,
                        "name": str(item.get("name") or item.get("email") or ""),
                    }
                ).model_dump()
                for item in raw_accounts
                if isinstance(item, dict)
            ]
        else:
            source = str(payload.get("text") or "")
        return service.batch_import(
            source,
            start_login=bool(payload.get("start_login", False)),
            use_proxy_pool=bool(payload.get("use_proxy_pool", True)),
        )
    except Exception as exc:
        raise _detail(exc) from exc


@app.patch("/api/accounts/{account_id}", dependencies=[Depends(_admin_token)])
def patch_account(account_id: int, payload: AccountPatch) -> dict[str, Any]:
    try:
        return service.update_account(
            account_id, payload.model_dump(exclude_none=True, exclude_unset=True)
        )
    except Exception as exc:
        raise _detail(exc) from exc


@app.delete("/api/accounts/{account_id}", dependencies=[Depends(_admin_token)])
def remove_account(account_id: int) -> dict[str, Any]:
    try:
        if not service.delete_account(account_id):
            raise KeyError("account not found")
        return {"deleted": True}
    except Exception as exc:
        raise _detail(exc) from exc


@app.post("/api/accounts/{account_id}/check", dependencies=[Depends(_admin_token)])
def check_account(account_id: int) -> dict[str, Any]:
    try:
        return service.check_account(account_id)
    except Exception as exc:
        raise _detail(exc) from exc


@app.post("/api/accounts/{account_id}/balance", dependencies=[Depends(_admin_token)])
def refresh_balance(account_id: int) -> dict[str, Any]:
    return check_account(account_id)


@app.post(
    "/api/accounts/{account_id}/cdp/reconnect", dependencies=[Depends(_admin_token)]
)
def reconnect_account(account_id: int) -> dict[str, Any]:
    if not database.get_account(account_id):
        raise HTTPException(status_code=404, detail="account not found")
    started = service.schedule_login(account_id)
    return {"accepted": True, "started": started, "account_id": account_id}


@app.get("/api/models", dependencies=[Depends(_admin_token)])
def admin_models() -> list[dict[str, Any]]:
    return service.models()


@app.get("/v1/models", dependencies=[Depends(_api_token)])
def models() -> dict[str, Any]:
    return {"object": "list", "data": service.models()}


@app.get("/api/model-costs", dependencies=[Depends(_admin_token)])
def model_costs(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    return database.list_model_costs(limit=limit)


@app.get("/api/tasks", dependencies=[Depends(_admin_token)])
def tasks(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    return database.list_task_summaries(limit)


@app.post("/api/tasks", dependencies=[Depends(_admin_token)])
def create_admin_task(payload: GenerationTaskCreate) -> dict[str, Any]:
    return _create(payload.model_dump(exclude_none=True, exclude_unset=True))


@app.delete("/api/tasks", dependencies=[Depends(_admin_token)])
def clear_tasks() -> dict[str, Any]:
    return {"deleted": database.clear_finished_tasks()}


@app.get("/api/tasks/{task_id}", dependencies=[Depends(_admin_token)])
def task_detail(task_id: str) -> dict[str, Any]:
    return _task_or_404(task_id)


@app.post("/api/tasks/{task_id}/retry", dependencies=[Depends(_admin_token)])
def retry_task(task_id: str) -> dict[str, Any]:
    try:
        return service.retry_task(task_id)
    except Exception as exc:
        raise _detail(exc) from exc


@app.get("/api/tasks/{task_id}/media/{index}", dependencies=[Depends(_admin_token)])
def task_media(task_id: str, index: int) -> Response:
    try:
        source = service.task_media_source(task_id, index)
    except Exception as exc:
        raise _detail(exc) from exc
    if source.startswith(("http://", "https://")):
        return RedirectResponse(source)
    if source.startswith("data:"):
        header, _, data = source.partition(",")
        mime = header[5:].partition(";")[0] or "application/octet-stream"
        raw = base64.b64decode(data) if ";base64" in header else unquote(data).encode()
        return Response(raw, media_type=mime)
    raise HTTPException(status_code=404, detail="media not found")


@app.get("/api/integration-docs", dependencies=[Depends(_admin_token)])
def integration_docs() -> Response:
    return Response(
        (BASE_DIR.parent / "README.md").read_text(encoding="utf-8"),
        media_type="text/plain",
    )


@app.post("/v1/videos", dependencies=[Depends(_api_token)])
@app.post("/v1/videos/generations", dependencies=[Depends(_api_token)])
@app.post("/api/videos/generate", dependencies=[Depends(_api_token)])
def create_video(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    task = _create(payload, synchronous=not bool(payload.get("background", True)))
    return service.public_task(task)


@app.get("/v1/videos/{task_id}", dependencies=[Depends(_api_token)])
@app.get("/api/videos/{task_id}", dependencies=[Depends(_api_token)])
def get_video(task_id: str) -> dict[str, Any]:
    return service.public_task(_task_or_404(task_id))


@app.get("/v1/videos/{task_id}/content", dependencies=[Depends(_api_token)])
def video_content(task_id: str) -> Response:
    task = _task_or_404(task_id)
    urls = task.get("result_urls") or []
    if task.get("status") != "succeeded" or not urls:
        raise HTTPException(status_code=409, detail="video is not ready")
    return RedirectResponse(str(urls[0]), status_code=307)


@app.post("/v1/responses", dependencies=[Depends(_api_token)])
def responses(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    request_payload = _responses_input(payload)
    task = _create(
        request_payload, synchronous=not bool(payload.get("background", True))
    )
    return _response_object(task)


@app.get("/v1/responses/{task_id}", dependencies=[Depends(_api_token)])
def get_response(task_id: str) -> dict[str, Any]:
    return _response_object(_task_or_404(task_id))


@app.post("/api/v3/contents/generations/tasks", dependencies=[Depends(_api_token)])
def generic_task(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    task = _create(payload)
    return {"code": 100, "data": service.public_task(task)}


@app.get(
    "/api/v3/contents/generations/tasks/{task_id}", dependencies=[Depends(_api_token)]
)
def generic_task_status(task_id: str) -> dict[str, Any]:
    return {"code": 100, "data": service.public_task(_task_or_404(task_id))}
