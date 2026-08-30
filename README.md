# 飞牛 Docker 部署器

一个面向 fnOS 的中文 Docker 部署工具。安装为飞牛 `.fpk` 应用后，可以在浏览器里分步部署任意镜像，或从公开 GitHub 仓库拉取 Compose 项目。

## 功能

- 镜像部署：Docker Hub、GHCR 和其他标准镜像仓库。
- GitHub 部署：拉取公开仓库，自动寻找 `compose.yaml`、`compose.yml`、`docker-compose.yaml` 或 `docker-compose.yml`。
- 中文分步表单：端口、目录/数据卷、环境变量、命令、设备、网络、重启策略。
- 权限选项：容器内 root 用户和 Docker privileged 特权模式均为单项目可选。
- 后台任务：拉取和构建不阻塞飞牛页面，可随时查看日志。
- 可选 AI 辅助：连接 OpenAI 兼容中转，生成部署草稿；草稿必须人工确认，不会自动执行。
- 项目管理：查看状态，启动、停止、重启、拉取更新。
- 安全默认值：两段式端口映射自动绑定 NAS 局域网 IP，不默认监听全部接口。
- 可恢复：每个项目都保存在 `/vol1/Docker/项目名`，卸载 FPK 时不删除项目、容器或数据卷。

## 安装

1. 从 GitHub Releases 下载最新版 `.fpk`。
2. 打开 fnOS 应用中心，选择“手动安装”。
3. 上传 `.fpk`，确认 root Docker 管理权限。
4. 从飞牛桌面打开“Docker 部署器”。

## 两种部署方式

### 任意镜像

填写镜像地址，例如：

```text
nginx:1.29-alpine
ghcr.io/owner/image:1.0.0
```

端口每行一个，通常写成：

```text
8080:80
```

部署器会自动绑定为 `NAS局域网IP:8080:80`。目录映射示例：

```text
/vol1/data:/data
my_config:/config
```

### GitHub Compose

填写公开仓库 URL，例如：

```text
https://github.com/owner/repository
```

部署器会克隆到 `/vol1/Docker/项目名/source`，然后寻找仓库根目录、`docker/`、`deploy/` 或 `deployment/` 下的标准 Compose 文件。其他位置可手工填写相对路径。

## 安全边界

- 本应用本身以 root 运行，因为它需要管理 Docker；只有 fnOS 管理员应能打开入口。
- root 或 privileged 容器几乎可以完全控制 NAS。只对可信镜像启用。
- GitHub 仓库自带的 Compose 文件可以挂载宿主机目录、开放端口或启用特权模式，部署前应先审阅。
- 当前版本只支持公开 GitHub 仓库，不接收也不保存 GitHub Token。
- AI API Key 仅保存在 NAS 的 `/vol1/Docker/.fnos-docker-deployer/ai.json`，权限为 `0600`，不进入部署任务。
- AI 输出是不可信草稿，不会直接转成 root 操作；必须核对上游说明后再部署。
- 环境变量保存在 NAS 项目目录的 `.env`，权限为 `0600`。请勿将项目目录公开同步。
- 本项目仓库和 Release 不包含用户密码、数据库、照片、Docker 数据卷或镜像归档。

## 在 fnOS 上构建

fnOS 已安装官方 `fnpack` 时：

```bash
fnpack build --directory ./fnos-docker-deployer
```

打包前确保 `cmd/*`、`app/ui/index.cgi` 和 `app/scripts/worker.py` 具有可执行权限。

仓库也提供一键构建脚本：

```bash
bash scripts/build-on-fnos.sh
```

生成的 FPK 和 `SHA256SUMS` 位于 `dist/`。

## 许可

MIT
