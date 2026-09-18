# 3.5 pre 部署

`pre` 分支 push 或手动触发 `.github/workflows/deploy-test.yml`，执行测试、构建并推送 GHCR 镜像，再由专属 runner 部署。

| 配置 | 值 |
| --- | --- |
| 仓库 | `btcfoxman/pol2api`，public，默认分支 `pre` |
| 镜像 | `ghcr.io/btcfoxman/pol2api:<commit SHA>` 和 `latest` |
| 主机 | `192.168.3.5` |
| DEPLOY_PATH | `/home/btcfoxman/docker/pol2api` |
| DEPLOY_SERVICE | `pol2api` |
| runner | `repo-pol2api`，标签 `pol2api-pre` |
| runner 目录 | `/home/btcfoxman/actions-runners/repo-pol2api` |
| 宿主机 / 容器端口 | `8799` / `8798` |
| 网络 | `my-shared-net` |
| 公网入口 | `https://pol2api.aiid.edu.kg` |
| tunnel ingress | `http://127.0.0.1:8799` |

8798 已由服务器上的 dra2api 使用，因此 pol2api 的服务器端口为 8799；本地开发端口仍为 8798。

## 首次准备

将 `deploy/docker-compose.pre.yml` 放入部署目录，命名为 `docker-compose.yml`。在服务器配置独立 `.env` 和持久化 `data/`，凭据仅留在私有环境。应用使用 `POL_API_KEY`、`POL_ADMIN_TOKEN`、`POL_SYNC_TOKEN`，不要将 `IMAGE_*` 写入 `.env`。

在仓库注册独立 runner 并安装为 systemd 服务；可复用 ak2api 的 runner 程序，但不能复制其注册凭据、工作目录或注册状态。runner 用户需具备 Docker 与现有 Cloudflared 管理命令权限。

GitHub `GITHUB_TOKEN` 的 `contents:read` 和 `packages:write` 权限用于 checkout 和 GHCR。部署不依赖 SSH Secrets，依靠主机上的独立 runner。仅 `pre` 推送和手动运行触发，不执行外部 PR 的服务器任务。

服务器直连 GitHub 受限时，仓库变量 `RUNNER_HTTPS_PROXY` 可指定 HTTP 代理。3.5 使用 `http://127.0.0.1:10809`，仅作用于该 runner 的 checkout 步骤，不修改主机或其他仓库的全局 Git 配置。

服务使用授权 Cookie，会话更新通过管理页面或账号同步 API。容器不启动浏览器；迁移数据库时应将持久化设置 `proxy_host_override` 改为 `host.docker.internal`，并关闭 `browser_recovery_enabled`，避免本地设置覆盖容器默认值。

## 更新与回滚

工作流只选择镜像并重建本服务，保留服务器 `.env`、Compose 和数据库。Tunnel 配置只增改本服务的 ingress，先校验、再备份和安装，仅发生配置变化时重启 Cloudflared，最后验证公网健康检查。

回滚时在部署目录执行：

```bash
IMAGE_TAG=<previous-successful-sha> docker compose pull pol2api
IMAGE_TAG=<previous-successful-sha> docker compose up -d --no-build pol2api
curl -fsS http://127.0.0.1:8799/health
```

数据库需使用 SQLite backup API 或在停机时备份，保留已提交任务 ID，防止升级后重复提交。每个数据库只运行一个应用实例。
