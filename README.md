# POL2API

Pollo.ai 协议网关。任务管理、账号池、设置界面和外部 API 形式沿用本地 ak2api，Pollo 上游适配重新实现。积分领取、自动注册和密码登录暂不接入；使用已授权的 Cookie 会话或现有 Chrome CDP 会话。

## 启动

需要 Python 3.12、ffprobe（来自 FFmpeg）。

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
# 修改 .env 中三种密钥后启动，单进程运行。
.venv\Scripts\python -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8798
```

控制台：`http://127.0.0.1:8798/`，使用 `POL_ADMIN_TOKEN` 登录。外部调用使用 `POL_API_KEY`；账号同步使用 `POL_SYNC_TOKEN`。密钥、数据库、Cookie、媒体与日志不要提交仓库。

Docker：配置 `.env` 后运行 `docker compose up -d --build`。默认端口 8798，数据位于 `./data`。一个数据库只运行一个应用进程/副本；不要使用多个 Uvicorn workers。

3.5 pre 使用独立 runner、GHCR 和 Cloudflare Tunnel，推送 `pre` 分支触发部署；服务器端口为 8799。配置及回滚方式见 [部署文档](docs/deployment.md)。

## 账号与任务管理

控制台支持 Cookie Header / Cookie JSON 导入、批量 JSON 导入、编辑、启用/禁用、并发设置、代理池分配、余额检测，以及从已打开的本机 CDP 端口读取会话。读取 CDP 不会启动、导航或关闭用户浏览器，不使用 Playwright。容器内的本机 CDP 只适用于同容器浏览器；跨机器请导入 Cookie。

也可在宿主机导入：

```powershell
.venv\Scripts\python scripts/import_cdp.py --port 9336 --name pollo-local --proxy socks5://127.0.0.1:20001
```

保存账号后点击“检测”确认余额和项目。会话失效时更新 Cookie 或在浏览器登录后再次读取 CDP。积分余额取 `totalCount-useCount`，不重复累计奖励汇总。账号任务运行期间暂不刷新余额，以免重复计算预留额度。

任务支持异步队列、同步等待、上游 ID 恢复查询、结果预览、请求/响应审计、失败重试、已结束任务清理。设置支持并发、排队容量、轮询/超时、代理池、会话维护、素材超限策略和模型别名。修改设置持久化到 SQLite。

提交前按完整素材和元数据向上游询价；足额后才提交。提交成功的 ID 与本地扣减在同一事务保存。提交超时或响应丢失不会自动重发，避免重复扣费；`SUBMISSION_UNKNOWN` 需先核对上游。已取得上游 ID 的查询超时任务，点击重试会继续查询原任务。失败退款以真实余额为准。

## 外部调用

```http
Authorization: Bearer <POL_API_KEY>
```

同时支持 `X-API-Key`。

| 用途 | 路径 |
|---|---|
| 模型及能力 | `GET /v1/models` |
| 创建视频 | `POST /v1/videos`、`POST /v1/videos/generations` |
| 查询视频 | `GET /v1/videos/{task_id}` |
| 结果跳转 | `GET /v1/videos/{task_id}/content` |
| Responses 兼容创建/查询 | `POST /v1/responses`、`GET /v1/responses/{task_id}` |
| 通用任务创建/查询 | `POST /api/v3/contents/generations/tasks`、`GET /api/v3/contents/generations/tasks/{task_id}` |
| 下游账号同步 | `POST /api/accounts/sync`，使用同步密钥 |

创建示例：

```json
{
  "model": "sd-2-0-mini",
  "prompt": "以图片1为主角，参考video1的动作，配合Audio1",
  "duration": 4,
  "resolution": "480p",
  "aspect_ratio": "16:9",
  "image_urls": ["https://example.com/image.png"],
  "video_urls": ["https://example.com/video.mp4"],
  "audio_urls": ["https://example.com/audio.mp3"],
  "generate_audio": true,
  "published": true,
  "protection_mode": false,
  "n": 1,
  "background": true
}
```

示例地址需要换成可访问的真实文件。支持 HTTP(S) URL 或 data URL；不读取本地路径，不向媒体下载/存储端发送账户 Cookie。音视频元数据由 ffprobe 解析，图片由 Pillow 校验。单个 HTTP 请求包含 base64 媒体时应自行控制请求大小。

提示词 `视频1|video2|v3|视4|图片1|图1|img2|Image3|Audio1|音频2` 按各媒体列表的一基编号归一化为 Pollo `[@name]`。既有 `[@name]` 支持传入具名素材，例如 `{"url":"...","name":"storyboard"}`；默认拒绝不存在或越界的引用。

Seedance 无后缀模型默认 **720p**；显式分辨率后缀不允许与请求 resolution 冲突。上游报价受参考视频时长、模型、分辨率和活动影响，不采用固定价格表。控制台“消耗参考”展示实际任务样本，不能替代实时询价。

| 外部名称 | 上游模型 | 默认分辨率 |
|---|---|---|
| `sd-2-0` | `seedance-2-0` | 720p |
| `sd-2-0-1080p` | `seedance-2-0` | 1080p |
| `sd-2-0-4k` | `seedance-2-0` | **4K** |
| `sd-2-0-fast` | `seedance-2-0-fast` | 720p |
| `sd-2-0-mini` | `seedance-2-0-mini` | 720p |
| `sd-2-0-fast-480p` | `seedance-2-0-fast` | 480p |
| `sd-2-5` | `seedance-2-5` | 720p |
| `sd-2-5-480p` | `seedance-2-5` | 480p |
| `sd-2-5-1080p` | `seedance-2-5` | 1080p |

也支持上述四种 Seedance 上游 modelKey 直接调用，以及 Pollo 2/2.5/3、Wan 3/Prime、MiniMax H3/H3 Max/Hailuo 03/H3 Fast 的已确认模式；具体名称和能力见 `/v1/models`。2.0 系列支持 9 图/3 音频/3 视频、4–15 秒；2.5 支持 30 图/10 音频/10 视频、4–30 秒。素材大小、总时长及其他约束在提交前用实时 manifest 再校验。无视觉素材时标准/Fast/2.5 走 `multi2video`；Mini 目前要求至少一个图片或视频参考，因为捕获的文生视频模型列表不含 Mini。

`background=false` 同步等待，超过同步时限仍返回可继续查询的任务 ID。成功时从 `data[].url` 获取结果，`metadata` 使用上游实际尺寸与时长。Responses 接受字符串 input 或消息内容列表；兼容返回形状不代表实现了完整 OpenAI Responses API。

上游实测包括 Mini 480p 多素材、2.5 480p 多素材、标准 480p 文生视频；其他能力来自服务端 manifest，仍由上游当前账户权益和实时规则决定。`published=true` 表示允许上游公开作品，`protection_mode` 与会员权益有关，请按需要设置。

## 验证

```powershell
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m ruff check app tests scripts
node --check app/static/app.js
```

原始抓包保存在工作区忽略目录；脱敏研究资料见 `docs/research/protocol-research.md`。公共代码不包含账户凭据或真实媒体地址。

当前测试与实际上游验证范围见 [实现验证记录](docs/implementation-verification.md)。

## 生成模式与原始下载

`generation_mode` 支持 `auto`（默认）、`reference`、`image`、`text`。`image_urls/video_urls/audio_urls` 默认作为多素材参考，按上游 refsV2 归一化提示词。首尾帧使用 `image_url` / `image_tail_url`，走 `multi2video` 的 `image/imageTail` 字段；提示词保持普通文本。尾帧必须同时提供首帧，不能混用参考图片数组。不同模型可用模式见 `capabilities.modes`。

按模型校验 `mode`、`web_search`、`generate_audio`、`seed` 等可选字段。Pollo 3 的 1080p/4K 需要 `mode=pro`；Pollo 2.5 的时长是离散值且 15 秒要求音频开启；H3 系列使用 768p/2K 等自身规格。报价与提交使用相同素材、元数据和参数。

成功结果优先级：官方 `videoUrlNoWatermark` → 原始 `mediaUrl` → 播放 `videoUrl`。`data[]` 同时返回 `preview_url`、`original_url`、`no_watermark_url`、`watermark_verified` 和 `source`。只有上游明确提供无水印地址时才标记验证成功；下载权限失败不会重发生成任务或改变已成功的生成状态。

`GET /v1/videos/{id}/content?variant=best&index=0` 跳转到首个优质结果；`variant` 还支持 `original`、`no_watermark`、`preview`。无水印版本不可用返回 403，不会用预览冒充；`refresh=true` 使用原任务 ID 更新过期链接。控制台原始视频下载使用同样逻辑。

详细协议与画质证据见 [第二轮记录](docs/research/round2-models-downloads.md)。外部终态错误包含 `error.outcome`：`rejected`/`failed` 为已知拒绝/失败，`unknown` 表示可能已提交，不应自动换渠道再次生成。
