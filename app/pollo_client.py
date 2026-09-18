from __future__ import annotations

import base64
import io
import ipaddress
import json
import math
import mimetypes
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, unquote_to_bytes

from curl_cffi import requests as curl_requests
from PIL import Image
import requests

from app.config import rewrite_loopback_proxy
from app.model_catalog import MODELS, enum_values


class UpstreamError(RuntimeError):
    def __init__(self, message, code="UPSTREAM_ERROR", status_code=502):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class SubmissionUnknown(UpstreamError):
    def __init__(self):
        super().__init__(
            "提交结果未知；请先核对上游记录，系统不会自动重复提交", "SUBMISSION_UNKNOWN"
        )


def public_media_url(url):
    parsed = urlsplit(url)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ValueError("素材 URL 无效")
    addresses = socket.getaddrinfo(
        parsed.hostname,
        parsed.port or (443 if parsed.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    if any(not ipaddress.ip_address(row[4][0]).is_global for row in addresses):
        raise ValueError("素材 URL 必须指向公共网络地址")


class PolloClient:
    def __init__(self, account, settings):
        self.account = dict(account)
        self.settings = settings
        self.base = settings.upstream_base_url.rstrip("/")
        self.proxy = rewrite_loopback_proxy(
            account.get("proxy_url", ""), settings.proxy_host_override
        )
        self.session = curl_requests.Session(impersonate="chrome")
        domain = urlsplit(self.base).hostname
        records = account.get("cookie_records") or []
        if isinstance(records, str):
            records = json.loads(records)
        cookies = {
            x["name"]: x["value"]
            for x in records
            if isinstance(x, dict) and x.get("name")
        }
        for part in (
            account.get("cookie_header") or account.get("cookies") or ""
        ).split(";"):
            key, sep, value = part.strip().partition("=")
            if sep:
                cookies[key] = value
        for name, value in cookies.items():
            self.session.cookies.set(name, value, domain=domain, path="/")

    def close(self):
        self.session.close()

    def session_context(self):
        records = [
            dict(name=c.name, value=c.value, domain=c.domain, path=c.path)
            for c in self.session.cookies.jar
        ]
        return dict(
            cookie_records=records,
            cookie_header="; ".join(f"{x['name']}={x['value']}" for x in records),
        )

    def _request(self, method, path, *, payload=None, params=None):
        headers = {
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": self.base + "/create",
            "x-recipe-protocol": "1",
            "user-language": "en",
        }
        if self.account.get("user_agent"):
            headers["User-Agent"] = self.account["user_agent"]
        attempts = self.settings.request_retries + 1 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                response = self.session.request(
                    method,
                    self.base + path,
                    json=payload,
                    params=params,
                    headers=headers,
                    proxy=self.proxy or None,
                    timeout=self.settings.request_timeout_seconds,
                    allow_redirects=False,
                )
            except Exception as exc:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise UpstreamError("Pollo 网络请求失败", "NETWORK_ERROR") from exc
            if response.status_code in (401, 403):
                raise UpstreamError(
                    "Pollo 会话失效或需要浏览器验证", "AUTH_REQUIRED", 401
                )
            if response.status_code == 429 or response.status_code >= 500:
                if attempt + 1 < attempts:
                    time.sleep(min(2**attempt, 5))
                    continue
                raise UpstreamError(
                    f"Pollo HTTP {response.status_code}",
                    "RATE_LIMITED"
                    if response.status_code == 429
                    else "UPSTREAM_HTTP_ERROR",
                    response.status_code,
                )
            if response.status_code >= 300:
                raise UpstreamError(
                    f"Pollo HTTP {response.status_code}",
                    "UPSTREAM_HTTP_ERROR",
                    response.status_code,
                )
            try:
                return response.json()
            except Exception as exc:
                raise UpstreamError(
                    "Pollo 返回非 JSON 响应", "INVALID_RESPONSE"
                ) from exc
        raise UpstreamError("Pollo 请求失败")

    def rpc(self, name, value=None, *, mutation=False):
        data = {"0": {"json": value}}
        if mutation:
            body = self._request(
                "POST", "/api/trpc/" + name, payload=data, params={"batch": "1"}
            )
        else:
            body = self._request(
                "GET",
                "/api/trpc/" + name,
                params={
                    "batch": "1",
                    "input": json.dumps(
                        data, ensure_ascii=False, separators=(",", ":")
                    ),
                },
            )
        item = body[0] if isinstance(body, list) and body else body
        if not isinstance(item, dict):
            raise UpstreamError("无效 tRPC 响应", "INVALID_RESPONSE")
        if "error" in item:
            err = item["error"].get("json", item["error"])
            details = err.get("data") or {}
            code = str(details.get("code") or err.get("code") or "TRPC_ERROR")
            message = str(err.get("message") or "Pollo 请求被拒绝")[:500]
            if code in ("UNAUTHORIZED", "FORBIDDEN"):
                raise UpstreamError("Pollo 会话失效或需要验证", "AUTH_REQUIRED", 401)
            raise UpstreamError(message, code, int(details.get("httpStatus") or 422))
        if "result" not in item or "data" not in item["result"]:
            raise UpstreamError("无效 tRPC 数据", "INVALID_RESPONSE")
        result = item["result"]["data"]
        return (
            result.get("json")
            if isinstance(result, dict) and "json" in result
            else result
        )

    def account_state(self):
        session = self._request("GET", "/api/auth/session")
        user = session.get("user") or {}
        if not user.get("id"):
            raise UpstreamError("请导入已登录的 Pollo 会话", "AUTH_REQUIRED", 401)
        usage = self.rpc("subUsage.getSubUsage", {"appName": "Pollo"})
        rows = [
            r for r in usage.get("usageList", []) if r.get("usageType") == "credits"
        ]
        balance = sum(
            max(float(r.get("totalCount") or 0) - float(r.get("useCount") or 0), 0)
            for r in rows
        )
        project = self.account.get("team_id") or self.account.get("cognito_sub")
        if not project:
            projects = self.rpc("userProject.getListV2", {"pageSize": 20})
            items = projects.get("items") or []
            if items:
                project = items[0].get("id") or items[0].get("projectId")
        return dict(
            available_balance=balance,
            buckets=usage,
            plan="paid" if usage.get("isActiveSub") else "free",
            user_id=str(user["id"]),
            email=user.get("email") or "",
            team_id=project or "",
            **self.session_context(),
        )

    def manifest(self, payload):
        return self.rpc(
            "recipe.manifest",
            {"recipeCode": payload["_recipe"], "model": payload["upstream_model"]},
        )

    def download(self, value, limit):
        if value.startswith("data:"):
            head, _, raw = value.partition(",")
            if len(raw) > limit * 4 // 3 + 1024:
                raise ValueError("素材过大")
            data = (
                base64.b64decode(raw, validate=True)
                if ";base64" in head
                else unquote_to_bytes(raw)
            )
            mime = head[5:].split(";")[0]
        else:
            url = value
            session = requests.Session()
            session.trust_env = False
            try:
                for _ in range(6):
                    public_media_url(url)
                    with session.get(
                        url,
                        stream=True,
                        allow_redirects=False,
                        timeout=self.settings.media_timeout_seconds,
                        proxies={"http": self.proxy, "https": self.proxy}
                        if self.proxy
                        else {},
                    ) as response:
                        if response.is_redirect:
                            from urllib.parse import urljoin

                            url = urljoin(url, response.headers["Location"])
                            continue
                        response.raise_for_status()
                        chunks = []
                        size = 0
                        for chunk in response.iter_content(65536):
                            size += len(chunk)
                            if size > limit:
                                raise ValueError("素材超过模型允许大小")
                            chunks.append(chunk)
                        data = b"".join(chunks)
                        mime = response.headers.get("Content-Type", "").split(";")[0]
                        break
                else:
                    raise ValueError("素材重定向次数过多")
            finally:
                session.close()
        if not data or len(data) > limit:
            raise ValueError("素材为空或过大")
        return data, mime

    def upload_media(self, source, kind, rule):
        data, mime = self.download(
            source["value"],
            min(
                self.settings.media_max_bytes,
                rule.get("maxFilesize", self.settings.media_max_bytes),
            ),
        )
        metadata = {"size": len(data)}
        if kind == "image":
            with Image.open(io.BytesIO(data)) as im:
                im.verify()
            with Image.open(io.BytesIO(data)) as im:
                metadata.update(width=im.width, height=im.height)
                mime = Image.MIME.get(im.format, mime)
        else:
            with tempfile.TemporaryDirectory(prefix="pol-media-") as folder:
                path = Path(folder) / "media"
                path.write_bytes(data)
                try:
                    probe = subprocess.run(
                        [
                            self.settings.ffprobe_executable,
                            "-v",
                            "error",
                            "-show_streams",
                            "-show_format",
                            "-of",
                            "json",
                            str(path),
                        ],
                        capture_output=True,
                        timeout=30,
                        check=True,
                    )
                    info = json.loads(probe.stdout)
                except Exception as exc:
                    raise ValueError(
                        "无法解析音视频；请检查媒体格式及 ffprobe 安装"
                    ) from exc
            stream = next(
                (s for s in info.get("streams", []) if s.get("codec_type") == kind),
                None,
            )
            if not stream:
                raise ValueError(f"文件中没有 {kind} 流")
            duration = float(
                info.get("format", {}).get("duration") or stream.get("duration") or 0
            )
            if not math.isfinite(duration) or not rule.get(
                "minDuration", 0
            ) <= duration <= rule.get("maxDuration", float("inf")):
                raise ValueError("参考素材时长超出模型范围")
            metadata["duration"] = duration
            if kind == "video":
                metadata.update(
                    width=int(stream["width"]), height=int(stream["height"])
                )
                num, _, den = str(stream.get("avg_frame_rate", "0/1")).partition("/")
                fps = float(num) / float(den or 1) if float(den or 1) else 0
                if fps and not rule.get("minFps", 0) <= fps <= rule.get(
                    "maxFps", float("inf")
                ):
                    raise ValueError("参考视频帧率超出模型范围")
        if "width" in metadata:
            width, height = metadata["width"], metadata["height"]
            for name, sign in (("minSize", -1), ("maxSize", 1)):
                if name in rule:
                    w, h = map(int, rule[name].split("*"))
                    if (
                        (width < w or height < h)
                        if sign < 0
                        else (width > w or height > h)
                    ):
                        raise ValueError("参考素材尺寸超出模型范围")
            ratio = width / height
            if (
                not rule.get("minRatio", 0)
                <= ratio
                <= rule.get("maxRatio", float("inf"))
            ):
                raise ValueError("参考素材画幅超出模型范围")
            if (
                not rule.get("minTotalPixels", 0)
                <= width * height
                <= rule.get("maxTotalPixels", float("inf"))
            ):
                raise ValueError("参考视频像素总数超出模型范围")
        ext = mimetypes.guess_extension(mime) or (
            ".mp4" if kind == "video" else ".mp3" if kind == "audio" else ".png"
        )
        if ext == ".jpe":
            ext = ".jpg"
        filename = source["label"] + ext
        signed = self._request(
            "POST", "/api/upload/sign", payload={"filename": filename, "type": kind}
        )
        destination = urlsplit(signed.get("sign", ""))
        if destination.scheme != "https" or not (destination.hostname or "").endswith(
            ".r2.cloudflarestorage.com"
        ):
            raise UpstreamError("上传签名目标无效")
        # Separate session: never forward account cookies to storage or caller media hosts.
        with requests.Session() as upload_session:
            upload_session.trust_env = False
            response = upload_session.put(
                signed["sign"],
                data=data,
                headers={"Content-Type": mime or "application/octet-stream"},
                timeout=self.settings.media_timeout_seconds,
                allow_redirects=False,
                proxies={"http": self.proxy, "https": self.proxy} if self.proxy else {},
            )
            if not 200 <= response.status_code < 300:
                raise UpstreamError("素材上传失败", "UPLOAD_FAILED")
        completed = self.rpc(
            "uploadAsset.complete",
            {"accessURL": signed["accessURL"], "fileName": filename},
            mutation=True,
        )
        return {
            "type": kind,
            "name": source["label"],
            kind: completed.get("url") or signed["accessURL"],
            "metadata": metadata,
        }

    def prepare(self, payload):
        manifest = self.manifest(payload)
        props = manifest["schema"]["properties"]
        for name, value in (
            ("resolution", payload["resolution"]),
            ("aspectRatio", payload["aspect_ratio"]),
        ):
            allowed = enum_values(props.get(name, {}))
            if allowed and value not in allowed:
                raise ValueError(f"实时模型配置不支持 {name}={value}")
        rule = props.get("duration", {})
        durations = enum_values(rule)
        if durations and payload["duration"] not in durations:
            raise ValueError("实时配置不支持该时长")
        if (
            not rule.get("minimum", 0)
            <= payload["duration"]
            <= rule.get("maximum", float("inf"))
        ):
            raise ValueError("实时配置不支持该时长")
        rules = (
            props.get("refs", {}).get("x-ui-config")
            or MODELS[payload["upstream_model"]]["reference_config"]
        )
        total_refs = sum(
            len(payload.get(field, [])) for field in ("_images", "_videos", "_audio")
        )
        max_refs = props.get("refs", {}).get(
            "maxItems", rules.get("maxItems", float("inf"))
        )
        if total_refs > max_refs:
            raise ValueError("实时素材总数量限制已变化")
        refs = []
        for kind, field in (
            ("image", "_images"),
            ("video", "_videos"),
            ("audio", "_audio"),
        ):
            limits = rules["upload"].get(kind, {})
            items = payload.get(field, [])
            if len(items) > limits.get("maxNum", 0):
                raise ValueError("实时素材数量限制已变化")
            for source in items:
                ref = self.upload_media(source, kind, limits)
                ref["order"] = len(refs) + 1
                refs.append(ref)
            subset = [r for r in refs if r["type"] == kind]
            if sum(r["metadata"].get("duration", 0) for r in subset) > limits.get(
                "maxTotalDuration", float("inf")
            ):
                raise ValueError("参考素材总时长超出限制")
            if sum(r["metadata"].get("size", 0) for r in subset) > limits.get(
                "maxTotalFilesize", float("inf")
            ):
                raise ValueError("参考素材总大小超出限制")
        user_input = {
            **manifest.get("initialValues", {}),
            "model": payload["upstream_model"],
            "prompt": payload["prompt"],
            "duration": payload["duration"],
            "resolution": payload["resolution"],
            "aspectRatio": payload["aspect_ratio"],
            "numOutputs": payload["n"],
            "generateAudio": payload.get("generate_audio", True),
            "published": payload.get("published", True),
            "protectionMode": payload.get("protection_mode", False),
        }
        if payload["_recipe"] == "ref2video":
            user_input["refs"] = refs
        if payload.get("seed") is not None:
            user_input["seed"] = int(payload["seed"])
        body = {
            "recipeCode": payload["_recipe"],
            "modelKey": payload["upstream_model"],
            "userInput": user_input,
            "numOutputs": payload["n"],
            "entitlement": {"unlimited": False},
        }
        quote = self.rpc("recipe.estimateTask", body, mutation=True)
        cost = quote.get("discountCost", quote.get("cost"))
        if cost is None or not math.isfinite(float(cost)) or float(cost) < 0:
            raise UpstreamError("上游未返回有效报价")
        project = self.account.get("team_id") or self.account.get("cognito_sub")
        if not project:
            raise UpstreamError(
                "账户没有可用项目，请在 Pollo 创建项目后刷新账户",
                "PROJECT_REQUIRED",
                422,
            )
        body["projectId"] = project
        return body, quote, float(cost)

    def generate(self, body):
        try:
            result = self.rpc("recipe.submit", body, mutation=True)
        except UpstreamError as exc:
            if exc.status_code >= 500 or exc.code in (
                "NETWORK_ERROR",
                "INVALID_RESPONSE",
            ):
                raise SubmissionUnknown() from exc
            raise
        except Exception as exc:
            # A malformed response can follow an accepted POST. Never replay it.
            raise SubmissionUnknown() from exc
        if not isinstance(result, dict) or not result.get("id"):
            raise SubmissionUnknown()
        return result

    def status(self, record_id):
        records = self.rpc(
            "generationPolling.fetchRecordsStatus", {"recordIds": [int(record_id)]}
        )
        if not isinstance(records, list) or not records:
            raise UpstreamError("上游任务暂未找到", "TASK_NOT_FOUND")
        state = next((r for r in records if str(r.get("id")) == str(record_id)), None)
        if state is None:
            raise UpstreamError("上游任务未匹配", "TASK_NOT_FOUND")
        return state

    def detail(self, record_id):
        return self.rpc("generation.queryRecordDetail", {"id": int(record_id)})


def result_urls(detail):
    outputs = detail.get("generations") or [detail]
    return list(
        dict.fromkeys(
            url
            for item in outputs
            if (url := item.get("videoUrl") or item.get("mediaUrl"))
            and url.startswith("https://")
        )
    )
