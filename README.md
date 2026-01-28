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
docker-compose up -d
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
```


## 🔗 高级：对接 Sonarr/Radarr

在 Sonarr/Radarr 的 Settings -> Download Clients 中：

添加一个新的 Torrent Blackhole 客户端。

Torrent Folder: 设置为本项目 watch 目录在宿主机上的路径（例如 /data/downloads）。

Watch Folder: 设置为任意空文件夹（本项目只负责处理种子，下载进度的监控通常依赖云端挂载的回扫）。

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