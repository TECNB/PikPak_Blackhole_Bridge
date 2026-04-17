🌉 PikPak Blackhole Bridge

连接 Sonarr/Radarr 种子黑洞与 Alist/PikPak 的自动化桥梁。

专为 非 NAS / 本地空间受限 用户设计。利用 Sonarr/Radarr 的 "Torrent Blackhole" 机制，拦截种子文件并自动推送至云端离线下载，实现“只存云端”的自动化影视库构建方案。

## 💡 项目背景

传统的自动化追剧方案（Sonarr/Radarr）通常依赖本地下载器（如 qBittorrent）和大量的本地存储空间（NAS）。

本项目解决了什么痛点？
如果你希望利用 PikPak 等网盘强大的离线下载能力，而不消耗本地硬盘空间，本工具充当了完美的“中间人”：

Sonarr/Radarr 负责搜刮资源，将种子扔进“黑洞”。

本工具 监听黑洞，截获种子，通过 Alist API 发送离线下载指令。

云端 秒速完成下载，配合 Rclone/Alist 挂载即可直接播放。

## ✨ 核心功能

🕳️ 对接种子黑洞：完美适配 Sonarr/Radarr 的 Torrent Blackhole 下载客户端模式。

☁️ 云端离线桥接：自动解析 .torrent 或 .magnet，调用 Alist 接口将任务无缝推送到 PikPak。

🐳 Docker 开箱即用：环境隔离，配置简单，一键启动。

🧹 智能路径解析：

- 自动识别剧集名称 (S01, S02...)。

- Radarr 电影 Grab 可通过 Webhook 提供 `movie.title`、`movie.year`、`release.releaseTitle`，黑洞文件只负责提供真正的磁力信息，电影目录固定生成为 `{电影名} ({年份})`。


- 清洗文件名（去除分辨率、制作组等冗余信息）。


- 精准归类：将文件推送至云端对应的剧集目录，保持云端库整洁。

🛡️ 隐私安全：所有敏感配置通过环境变量隔离。

## 🛠️ 前置要求

安装 Docker 和 Docker Compose。

部署并运行 Alist，且已挂载 PikPak 网盘。

Sonarr/Radarr 配置：在 Download Client 中添加 "Torrent Blackhole"，并指定一个文件夹作为黑洞路径。

## 🚀 快速开始

1. 克隆仓库

```bash
git clone [https://github.com/TECNB/docker_pikpak.git](https://github.com/TECNB/docker_pikpak.git)
cd docker_pikpak
```


2. 配置环境变量

复制并修改配置文件：

```bash
cp .env.example .env
```

编辑 .env 文件，填入 Alist 账号密码及路径信息


3. 启动服务

```bash
docker compose up -d
```


## 📂 工作流示意

假设你配置了 Sonarr 的黑洞路径为本项目的监听目录：

Sonarr 抓取到《曼达洛人》的种子，将其放入 ./data/watch/ (即黑洞目录)。

本工具 探测到新文件：

解析磁力链接。

识别剧名：The Mandalorian，季度：Season 03。

本工具 请求 Alist，将任务离线下载到云端路径：/pikpak/TV/The Mandalorian/Season 03/。

本工具 将本地种子移动到 ./data/processed/ 归档，防止重复处理。

## ⚙️ 配置文件说明 (.env)

```markdown
# Alist 服务地址
ALIST_HOST=[http://127.0.0.1:5244](http://127.0.0.1:5244)

# 认证信息
ALIST_USERNAME=admin
ALIST_PASSWORD=your_password

# 云端存储根目录 (PikPak 挂载路径)
ALIST_BASE_PATH=/pikpak/TV

# 容器内路径映射 (通常保持默认，需与 docker-compose volumes 对应)
WATCH_DIR=/data/watch
PROCESSED_DIR=/data/processed

# 扫描频率 (秒)
CHECK_INTERVAL=10

# Radarr Grab Webhook 服务
# Radarr -> Settings -> Connect -> Webhook
# URL: http://<本机IP>:8787/webhook/radarr
WEBHOOK_HOST=0.0.0.0
WEBHOOK_PORT=8787
PENDING_TASK_FILE=/data/processed/radarr_pending_tasks.json

# 电影下载完成后的保守型单层包装目录拍平
# 仅 Movie 生效；TV/剧集不会参与整理
MOVIE_FLATTEN_ENABLED=true
MOVIE_FLATTEN_TASK_FILE=/data/processed/movie_flatten_tasks.json
MOVIE_CONTENT_POLL_INTERVAL=6
MOVIE_CONTENT_MAX_ATTEMPTS=10
MOVIE_FLATTEN_STABLE_CHECKS=2
MOVIE_FLATTEN_MAX_TASKS_PER_LOOP=3

# 云端目录确认短轮询
PATH_READY_POLL_INTERVAL=2
PATH_READY_MAX_ATTEMPTS=10

# 可选: 电影拍平完成后，额外刷新并确认 CD2 已看到最终电影目录
CD2_REFRESH_ENABLED=false
CD2_HOST=http://your-cd2-host:19798
CD2_TOKEN=your_cd2_token_here
CD2_MOVIE_BASE_PATH=/WebDAV/Media/Movie
CD2_REFRESH_POLL_INTERVAL=10
CD2_REFRESH_MAX_ATTEMPTS=5
```


## 🔗 高级：对接 Sonarr/Radarr

在 Sonarr/Radarr 的 Settings -> Download Clients 中：

添加一个新的 Torrent Blackhole 客户端。

Torrent Folder: 设置为本项目 watch 目录在宿主机上的路径（例如 /data/downloads）。

Watch Folder: 设置为任意空文件夹（本项目只负责处理种子，下载进度的监控通常依赖云端挂载的回扫）。

### Radarr 电影目录匹配

Radarr 的 Torrent Blackhole Grab Webhook 不会提供磁力本体，但会提供结构化的影片与发布信息。本工具会先记录 Grab 事件，再等待 Movie 黑洞目录出现同名发布文件：

- Webhook 地址：`http://<本机IP>:8787/webhook/radarr`
- 触发事件：选择 Grab
- 匹配字段：`release.releaseTitle` 标准化后匹配黑洞文件名主干
- 磁力来源：`.torrent` 计算 BTIH，或从 `.magnet` / `.txt` 内提取 `magnet:?xt=urn:btih:...`
- 电影保存目录：`ALIST_PATH_MOVIE/movie.title (movie.year)`

电影离线任务提交成功后，工具会登记一个后处理任务。整理分为两阶段：先用 `refresh=true` 短轮询等待目标目录出现内容；首次出现内容后，再等待连续 2 次目录快照一致，然后做保守型单层包装目录检查：

- 根目录存在唯一包装子目录：将该子目录内容移动到电影根目录，再删除空子目录，兼容 4K/1080p 等多版本共存
- 根目录已有视频但同时存在唯一包装子目录：仍会拍平该包装子目录
- 若启用 `CD2_REFRESH_ENABLED=true`：仅会在“已拍平完成”或“确认根目录本就没有包装目录”之后，再对拍平后的最终 `movie_path` 执行一次 CD2 父目录刷新，并按 10 秒间隔最多轮询 5 次，用 CloudDrive2 的 `GetSubFiles + FindFileByPath` 确认 CD2 已看到该目录
- 若 CD2 的实际电影根目录与 OpenList 不同，可设置 `CD2_MOVIE_BASE_PATH`；例如 OpenList 为 `/pikpak/Media/Movie`，CD2 为 `/WebDAV/Media/Movie` 时，程序会自动把最终电影路径映射后再去 CD2 确认
- CD2 侧基于 CloudDrive2 官方 gRPC API 查询；默认直接按 `CD2_MOVIE_BASE_PATH` 映射后的路径访问，因此请让 token 的 `RootDirectoryRequired` 与该路径保持一致
- 内容出现等待 10 次后仍为空：停止该整理任务，并按离线 task id 或 BTIH 查询未完成离线/转存任务；若仍在进行则直接取消该任务，同时删除最初创建的空电影目录
- 其它情况：保守跳过，避免误动复杂目录
- 剧集目录不参与该整理流程

## 📅 路线图

计划开发轻量级 Web 可视化管理面板 (Dashboard)，以降低配置门槛并提供直观的运行状态监控：

### [ ] 可视化配置

支持在网页端直接修改 .env 环境变量及运行参数。

### [ ] 任务监控中心

[ ] 进行中任务 (Processing)：查看当前正在解析或推送的种子任务。

[ ] 已完成任务 (Completed)：查看历史处理记录及归档状态。

[ ] 清除记录：一键清理历史日志或重置归档目录。

### [ ] 系统与运维

[ ] Docker 管理：在页面上直接查看容器运行日志、执行容器重启。

[ ] 进程状态 (Process)：实时查看后台 Python 脚本的运行心跳与资源占用。

## 📝 开发与贡献

欢迎提交 Issue 或 PR 改进代码。

## 📄 许可证

MIT License
