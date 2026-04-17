import logging
import os
import sys


# 1. Alist 认证配置
ALIST_HOST = os.getenv("ALIST_HOST")
ALIST_USERNAME = os.getenv("ALIST_USERNAME")
ALIST_PASSWORD = os.getenv("ALIST_PASSWORD")

# 2. 归档根目录
PROCESSED_DIR = os.getenv("PROCESSED_DIR")

# 3. 监控配置 (路径映射: 本地监控路径 -> 云端基础路径)
WATCH_CONFIG = {
    "TV": {
        "local": os.getenv("WATCH_DIR_TV", "/data/downloads/incoming/TV"),
        "cloud": os.getenv("ALIST_PATH_TV", "/pikpak/Media/TV"),
    },
    "Movie": {
        "local": os.getenv("WATCH_DIR_MOVIE", "/data/downloads/incoming/Movie"),
        "cloud": os.getenv("ALIST_PATH_MOVIE", "/pikpak/Media/Movie"),
    },
}

# 4. 脚本设置
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "10"))

# 5. Webhook 设置
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8787"))
WEBHOOK_PATHS = {"/webhook", "/webhook/radarr", "/radarr/webhook"}
WEBHOOK_MAX_BODY_BYTES = int(os.getenv("WEBHOOK_MAX_BODY_BYTES", "1048576"))
PENDING_TASK_FILE = os.getenv("PENDING_TASK_FILE")

# 6. 电影下载完成后的保守型单层包装目录拍平
MOVIE_FLATTEN_ENABLED = os.getenv("MOVIE_FLATTEN_ENABLED", "true").lower() in ("1", "true", "yes", "on")
MOVIE_FLATTEN_TASK_FILE = os.getenv("MOVIE_FLATTEN_TASK_FILE")
MOVIE_FLATTEN_STABLE_CHECKS = int(os.getenv("MOVIE_FLATTEN_STABLE_CHECKS", "2"))
MOVIE_FLATTEN_MAX_TASKS_PER_LOOP = int(os.getenv("MOVIE_FLATTEN_MAX_TASKS_PER_LOOP", "3"))
MOVIE_FLATTEN_LIST_PER_PAGE = int(os.getenv("MOVIE_FLATTEN_LIST_PER_PAGE", "1000"))
MOVIE_CONTENT_POLL_INTERVAL = int(os.getenv("MOVIE_CONTENT_POLL_INTERVAL", "6"))
MOVIE_CONTENT_MAX_ATTEMPTS = int(os.getenv("MOVIE_CONTENT_MAX_ATTEMPTS", "10"))
PATH_READY_POLL_INTERVAL = int(os.getenv("PATH_READY_POLL_INTERVAL", "2"))
PATH_READY_MAX_ATTEMPTS = int(os.getenv("PATH_READY_MAX_ATTEMPTS", "10"))

VIDEO_EXTENSIONS = {
    ".3gp",
    ".avi",
    ".divx",
    ".flv",
    ".iso",
    ".m2ts",
    ".m4v",
    ".mkv",
    ".mov",
    ".mp4",
    ".mpeg",
    ".mpg",
    ".mts",
    ".ogm",
    ".ogv",
    ".rmvb",
    ".ts",
    ".vob",
    ".webm",
    ".wmv",
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

required_vars = {
    "PROCESSED_DIR": PROCESSED_DIR,
    "ALIST_HOST": ALIST_HOST,
    "ALIST_USERNAME": ALIST_USERNAME,
    "ALIST_PASSWORD": ALIST_PASSWORD,
}

missing_vars = [key for key, value in required_vars.items() if value is None]
if missing_vars:
    logger.error(f"缺少必需的环境变量: {', '.join(missing_vars)}")
    logger.error("请参考 .env 文件配置环境变量")
    sys.exit(1)

if not PENDING_TASK_FILE:
    PENDING_TASK_FILE = os.path.join(PROCESSED_DIR, "radarr_pending_tasks.json")

if not MOVIE_FLATTEN_TASK_FILE:
    MOVIE_FLATTEN_TASK_FILE = os.path.join(PROCESSED_DIR, "movie_flatten_tasks.json")
