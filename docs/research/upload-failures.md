# 上传及素材失败分类（2026-09-18）

本轮通过原生 CDP 监听授权浏览器，并结合 3.5 pre 服务日志排查。原始捕获、素材、账号及完整任务详情只保存于工作区忽略目录。

## 视频 Unsupported 样本

`POST /api/upload/sign` 与 `POST /api/trpc/uploadAsset.complete` 均返回 200。样本媒体为 MP4、H.264/AAC、1440×2560、24 fps、15.084 秒、29,764,950 字节。上传确认返回的宽高/时长字段为空，因此不能仅依赖该响应判定素材符合模型限制。

ffprobe 实测像素总数 3,686,400，大于已捕获的 Seedance 2.0 Mini/2.5 参考视频 `maxTotalPixels=2086876`。时长分别在 15.2 / 30.2 秒范围内。该样本可在生成前独立识别为 `MEDIA_LIMIT_EXCEEDED`，对外提示“素材超限，请修改后再试~”。这不是 HTTP 上传拒绝；未捕获到浏览器 Unsupported 提示的完整文字，不能断言前端只检查了这一项。1080×1920 的像素总数符合该项约束，其他素材参数仍需正常校验。

## 服务器图片下载失败

两个指定失败任务均没有上游生成 ID，也尚无提交/报价审计。服务堆栈定位到 `prepare → upload_media → download → requests.get`，异常为 `SSLError / SSLEOFError: UNEXPECTED_EOF_WHILE_READING`。同一账号代理下对原链接再次只读探测均返回 200 image/png，符合临时素材下载断连的表现。

修复为只对素材 GET 进行有限重试，仍失败返回 `MEDIA_DOWNLOAD_FAILED` 和“素材下载失败，请检查素材链接后重试~”。不把调用方素材服务器的 SSL/403 错误视为 Pollo 登录失效，不自动重发生成任务，不声明未发生的退款。

## 错误合同

保留诊断 `code`，新增 `category`、`refunded`；提交确定性仍由 `outcome` 独立表示。HTTP 403/tRPC 业务拒绝先解析内容审核信息；无内容审核信息的上传拒绝归入维护分类。Lingya 依据本地消息白名单展示，与原始错误透传开关独立，提交结果未知时仍停止自动重提。
