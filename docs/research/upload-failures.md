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

## 输出版权审核（后续样本）

两条 Seedance 2.5 任务在生成后失败，`failCode=3008`，说明为输出涉及潜在版权问题并明确声明 `Credits refunded.`。重新查询同一上游 ID 后，父记录和唯一输出仍返回相同终态及说明，`refundCreditDecimal` 均为空。

此类错误归入 `OUTPUT_MODERATION_FAILED`，提示“生成的视频内容违规，请修改描述后重试，积分已返还~”。保留原始诊断码和失败说明，退款来源注明 `upstream_failure_message`。这说明上游声明已退积分，不是独立的余额流水核销；不把无关调用方文本、尚未失败、多输出或有冲突的数值退款字段当作足额退款证据。计费净消耗按照已接受的上游退款声明修正，余额继续以上游余额接口为准。

## 输入敏感审核（后续样本）

Seedance 2.0 Fast 参考模式任务与 Wan 3.0 浏览器参考模式任务均返回 `failCode=3000`，说明为 `Sensitive input flagged by the third-party model. Please modify your input. Credits refunded.`。上游未明确区分是哪种输入触发审核，因此归入 `CONTENT_MODERATION_FAILED`，返回“检测到内容有敏感或违规情况，请修改后重试，积分已返还～”，不根据任务含图片就推断为图片或真人问题。

退款证据沿用上述严格单输出核验规则，将这一组完整错误码/消息加入已观察回执；数值退款字段的冲突、未知提交状态或多输出仍不能依据文字推断足额退款。

## 账号生成权限受限

后续连续提交在 `recipe.submit` 返回 `TOO_MANY_REQUESTS`，完整提示为 `Generation is currently restricted for your account. Please contact support if you believe this is a mistake.`。此提示明确限制账号生成，不应仅根据 429 错误码解释为普通速率限制。失败任务未获得上游记录 ID，保留原始拒绝原因，不声明退款。

此情况归为 `UPSTREAM_MAINTENANCE`，返回“上游维护中，请稍后再试~”。自动暂停该账号新任务分配，管理端显示“生成受限”；已经准备素材的任务在提交前再次检查限制状态。已取得上游 ID 的任务仍可查询。刷新余额/有效登录会话不代表生成限制解除，因此不能自动恢复账号生成权限。平台限制本身需由账号所有者联系上游处理，不自动换账号重提受限任务。
