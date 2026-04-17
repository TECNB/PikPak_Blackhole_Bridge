import os
import time
import requests
import hashlib
import shutil
import bencodepy
import re
import logging
import traceback
import sys
import json
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from threading import Lock, Thread
from urllib.parse import urlparse, unquote, quote

# ================= Configuration =================

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
        "cloud": os.getenv("ALIST_PATH_TV", "/pikpak/Media/TV")
    },
    "Movie": {
        "local": os.getenv("WATCH_DIR_MOVIE", "/data/downloads/incoming/Movie"),
        "cloud": os.getenv("ALIST_PATH_MOVIE", "/pikpak/Media/Movie")
    }
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

# =================================================

# 全局变量存储 Token
CURRENT_TOKEN = ""
PENDING_TASKS = {}
PENDING_TASKS_LOCK = Lock()
MOVIE_FLATTEN_TASKS = {}
MOVIE_FLATTEN_TASKS_LOCK = Lock()

VIDEO_EXTENSIONS = {
    ".3gp", ".avi", ".divx", ".flv", ".iso", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ogm", ".ogv", ".rmvb", ".ts", ".vob",
    ".webm", ".wmv"
}

# 配置日志格式 (输出到控制台)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

# 验证必需的环境变量
required_vars = {
    "PROCESSED_DIR": PROCESSED_DIR,
    "ALIST_HOST": ALIST_HOST,
    "ALIST_USERNAME": ALIST_USERNAME,
    "ALIST_PASSWORD": ALIST_PASSWORD
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

def login_and_update_token():
    """
    登录 Alist 并更新全局 Token
    """
    global CURRENT_TOKEN
    api_url = f"{ALIST_HOST}/api/auth/login"
    payload = {
        "username": ALIST_USERNAME,
        "password": ALIST_PASSWORD
    }
    
    try:
        logger.info("[身份验证] 正在尝试登录 Alist...")
        response = requests.post(api_url, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 200:
                token = data['data']['token']
                CURRENT_TOKEN = token
                logger.info(f"[身份验证] ✅ 登录成功，Token 已更新")
                return True
            else:
                logger.error(f"[身份验证] ❌ 登录失败: {data.get('message')}\n完整响应: {response.text}")
        else:
            logger.error(f"[身份验证] HTTP 错误: {response.status_code}\n完整响应: {response.text}")
    except Exception as e:
        logger.error(f"[身份验证] 连接异常: {e}")
    
    return False

def get_auth_header():
    """获取带 Token 的 Header，如果无 Token 则尝试登录"""
    if not CURRENT_TOKEN:
        login_and_update_token()
    return {"Authorization": CURRENT_TOKEN, "Content-Type": "application/json"}

def alist_post_request(url, payload, retry=True):
    """
    [新增] 通用请求封装函数
    负责发送 POST 请求，并自动拦截 Token 过期异常进行刷新重试。
    
    :param url: 请求地址
    :param payload: 请求体 JSON 数据
    :param retry: 是否允许重试（防止无限递归，仅允许重试 1 次）
    :return: requests.Response 对象
    """
    headers = get_auth_header()
    
    # 发送原始请求
    try:
        response = requests.post(url, json=payload, headers=headers)
    except requests.exceptions.RequestException as e:
        # 网络层面的连接错误直接抛出，由业务函数处理
        raise e

    # 检查 Token 是否失效
    # 情况 A: HTTP 401
    # 情况 B: HTTP 200 但业务 Code 提示 Token 过期
    token_expired = False
    
    if response.status_code == 401:
        token_expired = True
    elif response.status_code == 200:
        try:
            data = response.json()
            code = data.get('code')
            msg = data.get('message', '').lower()
            # Alist 有时返回 200 但 message 包含错误信息
            if code != 200 and ("token is expired" in msg or "token 无效" in msg):
                token_expired = True
        except ValueError:
            # 解析 JSON 失败，说明不是预期的业务错误，忽略
            pass

    # 如果 Token 失效且允许重试
    if token_expired and retry:
        logger.warning(f"[自动恢复] 检测到 Token 失效 (HTTP {response.status_code})，正在尝试刷新...")
        
        # 尝试刷新 Token
        if login_and_update_token():
            logger.info("[自动恢复] Token 刷新成功，正在重发请求...")
            # 递归调用自己，但在重试时关闭 retry 标志，防止死循环
            return alist_post_request(url, payload, retry=False)
        else:
            logger.error("[自动恢复] Token 刷新失败，无法重试，返回原始错误响应。")
            return response

    return response

def alist_get_request(url, retry=True):
    """通用 GET 请求封装，复用 Token 自动刷新逻辑。"""
    headers = get_auth_header()

    try:
        response = requests.get(url, headers=headers)
    except requests.exceptions.RequestException as e:
        raise e

    token_expired = False
    if response.status_code == 401:
        token_expired = True
    elif response.status_code == 200:
        try:
            data = response.json()
            code = data.get('code')
            msg = data.get('message', '').lower()
            if code != 200 and ("token is expired" in msg or "token 无效" in msg):
                token_expired = True
        except ValueError:
            pass

    if token_expired and retry:
        logger.warning(f"[自动恢复] 检测到 Token 失效 (HTTP {response.status_code})，正在尝试刷新...")
        if login_and_update_token():
            logger.info("[自动恢复] Token 刷新成功，正在重发请求...")
            return alist_get_request(url, retry=False)
        logger.error("[自动恢复] Token 刷新失败，无法重试，返回原始错误响应。")

    return response

def normalize_release_title(value):
    """
    将 Radarr releaseTitle 和黑洞文件名归一成同一个可比对 key。
    只做格式标准化，不尝试从名称里推断影片信息。
    """
    if not value:
        return ""

    text = unquote(str(value))
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r'[\W_]+', ' ', text, flags=re.UNICODE)
    text = re.sub(r'\s+', ' ', text).strip()
    return text

def get_file_release_key(filename):
    """取黑洞文件名主干并生成 releaseTitle 匹配 key。"""
    base_name = os.path.splitext(os.path.basename(filename))[0]
    return normalize_release_title(base_name)

def sanitize_path_component(value):
    """清理路径分段，避免电影标题中的路径分隔符产生意外子目录。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r'[\\/]+', ' - ', text)
    text = re.sub(r'[\x00-\x1f\x7f]+', '', text)
    text = re.sub(r'\s+', ' ', text).strip(" .")
    return text

def save_pending_tasks_locked():
    """持久化 pending webhook 任务。调用方需持有 PENDING_TASKS_LOCK。"""
    pending_dir = os.path.dirname(PENDING_TASK_FILE)
    if pending_dir:
        os.makedirs(pending_dir, exist_ok=True)

    tmp_path = f"{PENDING_TASK_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(PENDING_TASKS, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, PENDING_TASK_FILE)

def load_pending_tasks():
    """启动时加载未匹配的 Radarr Grab webhook 任务。"""
    global PENDING_TASKS

    if not os.path.exists(PENDING_TASK_FILE):
        logger.info(f"[Webhook] Pending 任务文件不存在，将在首次写入时创建: {PENDING_TASK_FILE}")
        return

    try:
        with open(PENDING_TASK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.warning(f"[Webhook] Pending 任务文件格式异常，已忽略: {PENDING_TASK_FILE}")
            return

        with PENDING_TASKS_LOCK:
            PENDING_TASKS = {
                key: task for key, task in data.items()
                if isinstance(key, str) and isinstance(task, dict)
            }

        logger.info(f"[Webhook] 已加载 Radarr pending 任务: {len(PENDING_TASKS)} 个")
    except Exception as e:
        logger.error(f"[Webhook] 加载 pending 任务失败: {e}")

def register_radarr_grab(payload):
    """记录 Radarr Grab webhook，等待黑洞文件按 releaseTitle 命中。"""
    event_type = payload.get("eventType")
    if event_type != "Grab":
        return {"ignored": True, "reason": f"eventType={event_type}"}

    movie = payload.get("movie") or {}
    remote_movie = payload.get("remoteMovie") or {}
    release = payload.get("release") or {}

    release_title = release.get("releaseTitle")
    movie_title = movie.get("title") or remote_movie.get("title")
    movie_year = movie.get("year") or remote_movie.get("year")

    if not release_title:
        raise ValueError("缺少 release.releaseTitle")
    if not movie_title:
        raise ValueError("缺少 movie.title")
    if not movie_year:
        raise ValueError("缺少 movie.year")

    release_key = normalize_release_title(release_title)
    if not release_key:
        raise ValueError("release.releaseTitle 标准化后为空")

    task = {
        "category": "Movie",
        "release_key": release_key,
        "release_title": release_title,
        "received_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "movie": {
            "id": movie.get("id"),
            "title": movie_title,
            "year": movie_year,
            "tmdbId": movie.get("tmdbId") or remote_movie.get("tmdbId"),
            "imdbId": movie.get("imdbId") or remote_movie.get("imdbId"),
            "folderPath": movie.get("folderPath")
        },
        "release": {
            "quality": release.get("quality"),
            "releaseGroup": release.get("releaseGroup"),
            "indexer": release.get("indexer"),
            "size": release.get("size")
        }
    }

    with PENDING_TASKS_LOCK:
        PENDING_TASKS[release_key] = task
        save_pending_tasks_locked()

    logger.info(f"[Webhook] 已记录 Radarr Grab: {movie_title} ({movie_year}) | releaseTitle: {release_title}")
    return {"ignored": False, "release_key": release_key, "movie": f"{movie_title} ({movie_year})"}

def find_pending_movie_task(filename, category_tag):
    """按标准化后的 releaseTitle 查找黑洞文件对应的 Radarr Grab 任务。"""
    release_key = get_file_release_key(filename)
    if not release_key:
        return None, None

    with PENDING_TASKS_LOCK:
        task = PENDING_TASKS.get(release_key)

    if task:
        movie = task.get("movie") or {}
        logger.info(
            f"{category_tag} [Webhook匹配] 命中: {movie.get('title')} ({movie.get('year')}) | 文件: {filename}"
        )
        return release_key, task

    logger.warning(f"{category_tag} [Webhook匹配] 未找到匹配的 Grab 任务，暂不处理: {filename}")
    return release_key, None

def remove_pending_movie_task(release_key, category_tag):
    """离线任务提交成功后移除 pending webhook 任务。"""
    if not release_key:
        return

    with PENDING_TASKS_LOCK:
        if release_key not in PENDING_TASKS:
            return
        task = PENDING_TASKS.pop(release_key)
        save_pending_tasks_locked()

    movie = task.get("movie") or {}
    logger.info(f"{category_tag} [Webhook匹配] 已清理 pending 任务: {movie.get('title')} ({movie.get('year')})")

def save_movie_flatten_tasks_locked():
    """持久化电影目录拍平任务。调用方需持有 MOVIE_FLATTEN_TASKS_LOCK。"""
    task_dir = os.path.dirname(MOVIE_FLATTEN_TASK_FILE)
    if task_dir:
        os.makedirs(task_dir, exist_ok=True)

    tmp_path = f"{MOVIE_FLATTEN_TASK_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(MOVIE_FLATTEN_TASKS, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MOVIE_FLATTEN_TASK_FILE)

def load_movie_flatten_tasks():
    """启动时加载未完成的电影目录拍平任务。"""
    global MOVIE_FLATTEN_TASKS

    if not MOVIE_FLATTEN_ENABLED:
        logger.info("[电影整理] 单层包装目录拍平已关闭")
        return

    if not os.path.exists(MOVIE_FLATTEN_TASK_FILE):
        logger.info(f"[电影整理] 任务文件不存在，将在首次写入时创建: {MOVIE_FLATTEN_TASK_FILE}")
        return

    try:
        with open(MOVIE_FLATTEN_TASK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.warning(f"[电影整理] 任务文件格式异常，已忽略: {MOVIE_FLATTEN_TASK_FILE}")
            return

        with MOVIE_FLATTEN_TASKS_LOCK:
            MOVIE_FLATTEN_TASKS = {
                key: task for key, task in data.items()
                if isinstance(key, str) and isinstance(task, dict)
            }

        logger.info(f"[电影整理] 已加载待处理任务: {len(MOVIE_FLATTEN_TASKS)} 个")
    except Exception as e:
        logger.error(f"[电影整理] 加载任务失败: {e}")

def register_movie_flatten_task(movie_path, movie_info, category_tag, offline_info=None):
    """登记电影目录拍平任务，后续主循环等待目录稳定后再整理。"""
    if not MOVIE_FLATTEN_ENABLED:
        return

    normalized_path = movie_path.rstrip("/")
    offline_info = offline_info or {}
    task = {
        "path": normalized_path,
        "movie": movie_info or {},
        "created_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "last_checked_at": None,
        "last_snapshot": None,
        "stable_count": 0,
        "content_wait_count": 0,
        "content_seen_at": None,
        "cleanup_wrapper_name": None,
        "offline_task_ids": offline_info.get("task_ids") or [],
        "btih": offline_info.get("btih")
    }

    with MOVIE_FLATTEN_TASKS_LOCK:
        if normalized_path in MOVIE_FLATTEN_TASKS:
            existing = MOVIE_FLATTEN_TASKS[normalized_path]
            existing["movie"] = movie_info or existing.get("movie") or {}
            existing["offline_task_ids"] = offline_info.get("task_ids") or existing.get("offline_task_ids") or []
            existing["btih"] = offline_info.get("btih") or existing.get("btih")
            existing["content_wait_count"] = 0
            existing["content_seen_at"] = None
            existing["last_snapshot"] = None
            existing["stable_count"] = 0
            MOVIE_FLATTEN_TASKS[normalized_path] = existing
        else:
            MOVIE_FLATTEN_TASKS[normalized_path] = task
        save_movie_flatten_tasks_locked()

    logger.info(f"{category_tag} [电影整理] 已登记下载完成后拍平检查: {normalized_path}")

def remove_movie_flatten_task(movie_path, reason):
    """移除已完成或已判定无需处理的电影目录拍平任务。"""
    normalized_path = movie_path.rstrip("/")

    with MOVIE_FLATTEN_TASKS_LOCK:
        if normalized_path not in MOVIE_FLATTEN_TASKS:
            return
        MOVIE_FLATTEN_TASKS.pop(normalized_path)
        save_movie_flatten_tasks_locked()

    logger.info(f"[电影整理] 任务结束: {normalized_path} | {reason}")

def update_movie_flatten_task(movie_path, updates):
    """更新电影目录拍平任务状态。"""
    normalized_path = movie_path.rstrip("/")

    with MOVIE_FLATTEN_TASKS_LOCK:
        task = MOVIE_FLATTEN_TASKS.get(normalized_path)
        if not task:
            return
        task.update(updates)
        MOVIE_FLATTEN_TASKS[normalized_path] = task
        save_movie_flatten_tasks_locked()

def get_movie_save_path_from_task(task, cloud_base_path, category_tag):
    """用 Radarr webhook 的 movie.title/year 直接生成电影保存目录。"""
    movie = task.get("movie") or {}
    title = sanitize_path_component(movie.get("title"))
    year = str(movie.get("year") or "").strip()

    if not title or not year:
        logger.warning(f"{category_tag} [路径解析] Webhook 电影信息不完整，使用基础路径: {cloud_base_path}")
        return cloud_base_path

    movie_folder = f"{title} ({year})"
    base = cloud_base_path.rstrip("/")
    final_path = f"{base}/{movie_folder}"
    logger.info(f"{category_tag} [路径解析] Webhook 电影目录: [{movie_folder}]")
    return final_path

def write_json_response(handler, status_code, payload):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)

class WebhookRequestHandler(BaseHTTPRequestHandler):
    server_version = "PikPakBlackholeBridgeWebhook/1.0"

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path == "/health":
            write_json_response(self, 200, {"ok": True, "service": "pikpak-blackhole-bridge"})
            return
        write_json_response(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        if path not in WEBHOOK_PATHS:
            write_json_response(self, 404, {"ok": False, "error": "not found"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            write_json_response(self, 400, {"ok": False, "error": "invalid Content-Length"})
            return

        if length <= 0:
            write_json_response(self, 400, {"ok": False, "error": "empty body"})
            return
        if length > WEBHOOK_MAX_BODY_BYTES:
            write_json_response(self, 413, {"ok": False, "error": "body too large"})
            return

        try:
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")

            result = register_radarr_grab(payload)
            write_json_response(self, 200, {"ok": True, **result})
        except json.JSONDecodeError as e:
            write_json_response(self, 400, {"ok": False, "error": f"invalid json: {e}"})
        except ValueError as e:
            write_json_response(self, 400, {"ok": False, "error": str(e)})
        except Exception as e:
            logger.error(f"[Webhook] 处理请求失败: {e}")
            logger.error(traceback.format_exc())
            write_json_response(self, 500, {"ok": False, "error": "internal server error"})

    def log_message(self, fmt, *args):
        logger.debug(f"[Webhook] {self.address_string()} - {fmt % args}")

def start_webhook_server():
    """启动 Radarr Grab webhook HTTP 服务。"""
    try:
        server = ThreadingHTTPServer((WEBHOOK_HOST, WEBHOOK_PORT), WebhookRequestHandler)
    except OSError as e:
        logger.error(f"[Webhook] 启动失败 {WEBHOOK_HOST}:{WEBHOOK_PORT}: {e}")
        return None

    thread = Thread(target=server.serve_forever, name="radarr-webhook-server", daemon=True)
    thread.start()
    paths = ", ".join(sorted(WEBHOOK_PATHS))
    logger.info(f"[Webhook] 已启动: http://{WEBHOOK_HOST}:{WEBHOOK_PORT} ({paths})")
    return server

def get_magnet_from_torrent(torrent_path, category_tag):
    """读取 .torrent 并计算磁力"""
    try:
        metadata = bencodepy.decode_from_file(torrent_path)
        subj = metadata[b'info']
        hashcontents = bencodepy.encode(subj)
        digest = hashlib.sha1(hashcontents).digest()
        b32hash = digest.hex()
        magnet = f"magnet:?xt=urn:btih:{b32hash}"
        logger.info(f"{category_tag} [解析种子] 成功: {os.path.basename(torrent_path)}")
        return magnet
    except Exception as e:
        logger.error(f"{category_tag} [解析种子] 失败 {torrent_path}: {e}")
        return None

def get_save_path(filename, cloud_base_path, category_tag):
    """
    解析文件名并生成保存路径
    """
    base_name = os.path.splitext(filename)[0]
    category_name = category_tag.strip("[]").lower()

    # 电影专属规则: {Movie Title} ({Release Year})
    if category_name == "movie":
        normalized_name = base_name.replace(".", " ").replace("_", " ")
        normalized_name = re.sub(r'\s+', ' ', normalized_name).strip()
        # 模式1: Title (Year) / Title [Year]
        movie_match = re.match(r'^(.*?)\s*[\(\[]((?:19|20)\d{2})[\)\]](?:\s|$)', normalized_name)
        # 模式2: Title Year 其他发布信息
        if not movie_match:
            movie_match = re.match(r'^(.*?)\s((?:19|20)\d{2})(?:\s|$)', normalized_name)

        if movie_match:
            raw_title = movie_match.group(1).strip(" -._")
            release_year = movie_match.group(2)
            clean_title = re.sub(r'\[.*?\]|【.*?】', '', raw_title).strip()
            clean_title = re.sub(r'\s+', ' ', clean_title).strip()

            # 多语标题干扰处理:
            # 例如 "Выживший + The Revenant" 优先保留英文标题段
            if clean_title:
                title_parts = [p.strip(" -._") for p in re.split(r'\s*[+/|]+\s*', clean_title) if p.strip(" -._")]
                if title_parts:
                    ascii_parts = [p for p in title_parts if re.search(r'[A-Za-z]', p)]
                    if ascii_parts:
                        clean_title = max(ascii_parts, key=lambda p: len(re.findall(r'[A-Za-z]', p)))
                    else:
                        clean_title = title_parts[-1]
                    clean_title = re.sub(r'\s+', ' ', clean_title).strip()

            if clean_title:
                movie_folder = f"{clean_title} ({release_year})"
                base = cloud_base_path.rstrip('/')
                final_path = f"{base}/{movie_folder}"
                logger.info(f"{category_tag} [路径解析] 电影提取: [{movie_folder}]")
                return final_path

        logger.warning(f"{category_tag} [路径解析] 未匹配到电影格式，使用基础路径: {cloud_base_path}")
        return cloud_base_path
    
    # 1. 去除所有括号内容
    base_name = re.sub(r'\[.*?\]', '', base_name)
    base_name = re.sub(r'【.*?】', '', base_name)
    base_name = re.sub(r'\(.*?\)', '', base_name)
    base_name = re.sub(r'（.*?）', '', base_name)

    # 2. 核心匹配 Sxx
    match = re.search(r'^(.*?)[\._\s]+S(\d+)', base_name, re.IGNORECASE)
    
    if match:
        raw_name = match.group(1)
        season_num = match.group(2)
        
        # 3. 强制去除中文 (非ASCII字符)
        clean_name = re.sub(r'[^\x00-\x7F]+', '', raw_name)
        
        # 4. 格式化
        clean_name = clean_name.replace(".", " ").replace("_", " ").strip()
        
        # 5. 合并多余空格
        clean_name = re.sub(r'\s+', ' ', clean_name).strip()

        try:
            season_folder = f"Season {int(season_num):02d}"
        except:
            season_folder = f"Season {season_num}"
        
        if clean_name:
            # 确保路径不以 / 结尾再拼接
            base = cloud_base_path.rstrip('/')
            final_path = f"{base}/{clean_name}/{season_folder}"
            logger.info(f"{category_tag} [路径解析] 提取: [{clean_name}] | 季度: [{season_folder}]")
            return final_path

    # 匹配失败或非剧集格式
    logger.warning(f"{category_tag} [路径解析] 未匹配到剧集格式，使用基础路径: {cloud_base_path}")
    return cloud_base_path

def alist_get_path_info(path, refresh=True):
    """调用 OpenList/Alist fs/get 获取路径信息，默认强制刷新缓存。"""
    api_url = f"{ALIST_HOST}/api/fs/get"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 0,
        "refresh": refresh
    }
    
    try:
        response = alist_post_request(api_url, payload)
        
        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 200:
                return data.get("data") or {}
            logger.info(f"[API Get] 路径暂不可用: {path} | Resp: {data}")
            return None

        logger.warning(f"[API Get] HTTP异常 {response.status_code} | Path: {path} | Resp: {response.text}")
        return None
    except Exception as e:
        logger.warning(f"[API Get] 请求异常: {e}")
    return None

def check_alist_path_exists(path, refresh=True):
    """
    调用 OpenList/Alist fs/get 查询路径是否存在。
    """
    return alist_get_path_info(path, refresh=refresh) is not None

def wait_for_alist_path_exists(path, parent_path=None, category_tag="[Path]"):
    """
    使用 fs/get(refresh=true) 短轮询确认路径存在。
    必要时用 fs/list(refresh=true) 刷新父目录，超时后保守返回失败。
    """
    for attempt in range(1, PATH_READY_MAX_ATTEMPTS + 1):
        if check_alist_path_exists(path, refresh=True):
            logger.info(f"{category_tag} [路径确认] 路径已就绪: {path} ({attempt}/{PATH_READY_MAX_ATTEMPTS})")
            return True

        if parent_path:
            alist_fs_list(parent_path, refresh=True)

        if attempt < PATH_READY_MAX_ATTEMPTS:
            logger.info(
                f"{category_tag} [路径确认] 等待路径刷新: {path} "
                f"({attempt}/{PATH_READY_MAX_ATTEMPTS})"
            )
            time.sleep(PATH_READY_POLL_INTERVAL)

    total_wait = PATH_READY_POLL_INTERVAL * max(PATH_READY_MAX_ATTEMPTS - 1, 0)
    logger.warning(
        f"{category_tag} [路径确认] 超时未确认路径: {path} "
        f"({PATH_READY_MAX_ATTEMPTS} 次，约 {total_wait} 秒)，本轮保守失败"
    )
    return False

def alist_fs_list(path, refresh=True):
    """
    强制刷新 Alist 缓存 (使用通用请求函数)
    """
    api_url = f"{ALIST_HOST}/api/fs/list"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 1,
        "refresh": refresh 
    }
    try:
        logger.info(f"[API List] 正在刷新目录缓存: {path}")
        # 使用封装的 alist_post_request 替代 requests.post
        resp = alist_post_request(api_url, payload)
        
        if resp.status_code != 200:
            logger.error(f"[API List] HTTP 错误: {resp.status_code} | Body: {resp.text}")
        else:
            data = resp.json()
            if data.get('code') != 200:
                logger.warning(f"[API List] 刷新返回非200: {data}")
    except Exception as e:
        logger.warning(f"[API List] 刷新请求失败 {path}: {e}")

def alist_list_dir(path, refresh=True):
    """
    列出目录内容，供电影整理逻辑判断下载结果是否稳定。
    """
    api_url = f"{ALIST_HOST}/api/fs/list"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": MOVIE_FLATTEN_LIST_PER_PAGE,
        "refresh": refresh
    }

    try:
        logger.info(f"[API List] 正在读取目录内容: {path}")
        resp = alist_post_request(api_url, payload)

        if resp.status_code != 200:
            logger.warning(f"[API List] HTTP 错误: {resp.status_code} | Path: {path} | Body: {resp.text}")
            return None

        data = resp.json()
        if data.get("code") != 200:
            logger.info(f"[API List] 目录暂不可读: {path} | Resp: {data}")
            return None

        result = data.get("data") or {}
        content = result.get("content")
        if content is None:
            return []
        if not isinstance(content, list):
            logger.warning(f"[API List] 返回 content 格式异常: {path} | Content: {content}")
            return None
        return content
    except Exception as e:
        logger.warning(f"[API List] 读取目录失败 {path}: {e}")
        return None

def alist_fs_move(src_dir, dst_dir, names, category_tag):
    """调用 OpenList/Alist move API 移动一组直接子项。"""
    if not names:
        return False

    api_url = f"{ALIST_HOST}/api/fs/move"
    payload = {
        "src_dir": src_dir,
        "dst_dir": dst_dir,
        "names": names
    }

    try:
        logger.info(f"{category_tag} [电影整理] 正在移动 {len(names)} 个子项: {src_dir} -> {dst_dir}")
        resp = alist_post_request(api_url, payload)
        logger.info(f"{category_tag} [Move API] HTTP: {resp.status_code} | Body: {resp.text}")

        if resp.status_code == 200:
            data = resp.json()
            return data.get("code") == 200
    except Exception as e:
        logger.warning(f"{category_tag} [电影整理] 移动失败: {e}")

    return False

def alist_fs_remove(dir_path, names, category_tag):
    """调用 OpenList/Alist remove API 删除一组直接子项。"""
    if not names:
        return False

    api_url = f"{ALIST_HOST}/api/fs/remove"
    payload = {
        "dir": dir_path,
        "names": names
    }

    try:
        logger.info(f"{category_tag} [电影整理] 正在删除空目录: {dir_path}/{names[0]}")
        resp = alist_post_request(api_url, payload)
        logger.info(f"{category_tag} [Remove API] HTTP: {resp.status_code} | Body: {resp.text}")

        if resp.status_code == 200:
            data = resp.json()
            return data.get("code") == 200
    except Exception as e:
        logger.warning(f"{category_tag} [电影整理] 删除失败: {e}")

    return False

def collect_task_ids_from_value(value):
    """从 add_offline_download 响应中尽量提取任务 id。"""
    task_ids = []

    def add_task_id(candidate):
        if candidate is None:
            return
        text = str(candidate).strip()
        if text and text not in task_ids:
            task_ids.append(text)

    def walk(node, parent_key=""):
        if isinstance(node, dict):
            for key, item in node.items():
                key_text = str(key).lower()
                if key_text in ("id", "tid", "task_id", "taskid"):
                    add_task_id(item)
                else:
                    walk(item, key_text)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)
        elif parent_key in ("id", "tid", "task_id", "taskid", "task", "tasks", "ids"):
            add_task_id(node)

    walk(value)
    if isinstance(value, str):
        add_task_id(value)
    elif isinstance(value, list) and all(not isinstance(item, (dict, list)) for item in value):
        for item in value:
            add_task_id(item)

    return task_ids

def extract_btih_from_magnet(magnet):
    """从 magnet 链接提取 BTIH/infohash。"""
    if not magnet:
        return None

    match = re.search(r'xt=urn:btih:([A-Za-z0-9]+)', magnet, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).lower()

def alist_get_undone_tasks(task_type):
    """读取 OpenList/Alist 未完成任务列表。"""
    api_url = f"{ALIST_HOST}/api/task/{task_type}/undone"

    try:
        resp = alist_get_request(api_url)
        if resp.status_code != 200:
            logger.warning(f"[Task API] HTTP 错误: {resp.status_code} | Type: {task_type} | Body: {resp.text}")
            return None

        data = resp.json()
        if data.get("code") != 200:
            logger.warning(f"[Task API] 返回非200: {task_type} | Resp: {data}")
            return None

        tasks = data.get("data") or []
        if not isinstance(tasks, list):
            logger.warning(f"[Task API] data 格式异常: {task_type} | Data: {tasks}")
            return None
        return tasks
    except Exception as e:
        logger.warning(f"[Task API] 查询未完成任务失败: {task_type} | {e}")
        return None

def alist_cancel_task(task_type, task_id):
    """取消 OpenList/Alist 未完成任务。"""
    if not task_id:
        return False

    api_url = f"{ALIST_HOST}/api/task/{task_type}/cancel?tid={quote(str(task_id), safe='')}"

    try:
        resp = alist_post_request(api_url, {})
        if resp.status_code != 200:
            logger.warning(f"[Task API] 取消任务 HTTP 错误: {resp.status_code} | Type: {task_type} | ID: {task_id} | Body: {resp.text}")
            return False

        data = resp.json()
        if data.get("code") == 200:
            logger.warning(f"[Task API] 已取消离线任务: type={task_type} id={task_id}")
            return True

        logger.warning(f"[Task API] 取消任务返回非200: type={task_type} id={task_id} | Resp: {data}")
    except Exception as e:
        logger.warning(f"[Task API] 取消任务失败: type={task_type} id={task_id} | {e}")

    return False

def task_contains_btih(task, btih):
    """在任务字段中查找 BTIH/infohash。"""
    if not btih:
        return False
    task_text = json.dumps(task, ensure_ascii=False).lower()
    return btih.lower() in task_text

def diagnose_movie_offline_status(movie_path, task):
    """
    内容出现等待超时后的诊断:
    优先按 add_offline_download 返回 task id，其次按 BTIH/infohash 匹配未完成任务。
    """
    task_ids = [str(tid) for tid in task.get("offline_task_ids") or [] if tid]
    btih = task.get("btih")
    task_types = ("offline_download", "offline_download_transfer")

    all_tasks = []
    failed_types = []
    for task_type in task_types:
        tasks = alist_get_undone_tasks(task_type)
        if tasks is None:
            failed_types.append(task_type)
            continue
        all_tasks.extend((task_type, undone_task) for undone_task in tasks)

    if failed_types:
        logger.warning(f"[Movie] [电影整理] 离线任务诊断部分失败: {movie_path} | 类型: {', '.join(failed_types)}")

    if task_ids:
        for task_type, undone_task in all_tasks:
            if str(undone_task.get("id")) in task_ids:
                logger.warning(
                    f"[Movie] [电影整理] 内容仍为空，但离线仍在进行: {movie_path} | "
                    f"type={task_type} id={undone_task.get('id')} "
                    f"state={undone_task.get('state')} progress={undone_task.get('progress')}"
                )
                alist_cancel_task(task_type, undone_task.get("id"))
                return

        logger.warning(
            f"[Movie] [电影整理] 内容仍为空，离线任务不存在: "
            f"{movie_path} | task_ids={task_ids}"
        )
        return

    if btih:
        for task_type, undone_task in all_tasks:
            if task_contains_btih(undone_task, btih):
                logger.warning(
                    f"[Movie] [电影整理] 内容仍为空，但离线仍在进行: {movie_path} | "
                    f"type={task_type} btih={btih} id={undone_task.get('id')} "
                    f"state={undone_task.get('state')} progress={undone_task.get('progress')}"
                )
                alist_cancel_task(task_type, undone_task.get("id"))
                return

        logger.warning(
            f"[Movie] [电影整理] 内容仍为空，离线任务不存在: "
            f"{movie_path} | btih={btih}"
        )
        return

    logger.warning(f"[Movie] [电影整理] 内容仍为空，离线任务不存在或缺少可匹配标识: {movie_path}")

def ensure_path_ready(full_path, skip_prefix_path, category_tag):
    """
    逐级创建目录 (智能跳过基础路径版 + 详细Debug日志)
    """
    logger.info(f"{category_tag} ------ 开始检查云端路径: {full_path} ------")
    
    parts = [p for p in full_path.split('/') if p]
    current_path = ""
    
    norm_skip_prefix = skip_prefix_path.rstrip('/')
    
    for i, part in enumerate(parts):
        parent_path = current_path if current_path else "/"
        current_path = f"{current_path}/{part}"
        
        # 1. 跳过基础路径
        if norm_skip_prefix.startswith(current_path):
            continue

        # 2. 检查是否存在，fs/get 每次强制刷新，避免依赖缓存 TTL
        if check_alist_path_exists(current_path, refresh=True):
            continue
        
        # 3. 不存在则创建，增加详细日志
        logger.info(f"{category_tag} [Step {i+1}] 目录不存在，正在创建: {current_path}")
        mkdir_url = f"{ALIST_HOST}/api/fs/mkdir"
        
        try:
            # 使用封装的 alist_post_request 替代 requests.post
            resp = alist_post_request(mkdir_url, {"path": current_path})
            
            # [关键] 无论成功失败，打印详细响应
            logger.info(f"{category_tag} [Mkdir API] HTTP: {resp.status_code} | Body: {resp.text}")
            
            # 检查 API 逻辑错误
            try:
                resp_json = resp.json()
                if resp_json.get('code') != 200:
                    logger.error(f"{category_tag} [Mkdir Error] 创建指令失败! 错误信息: {resp_json.get('message')}")
            except:
                pass

        except Exception as e:
            logger.warning(f"{category_tag} [Mkdir] 请求异常 (可忽略): {e}")

        # 4. 短轮询确认路径。本轮超时则保守失败，留给下一轮主循环重试。
        if not wait_for_alist_path_exists(current_path, parent_path=parent_path, category_tag=category_tag):
            logger.warning(f"{category_tag} [任务延后] 目录创建后暂未确认，下一轮重试: {current_path}")
            return False

    logger.info(f"{category_tag} ------ 云端路径校验全部通过 ------")
    return True

def add_offline_download(url, save_path, cloud_base_path, category_tag):
    """发送离线下载任务 (使用通用请求函数)"""
    # 将 cloud_base_path 传给 ensure_path_ready 作为 skip_prefix
    if not ensure_path_ready(save_path, cloud_base_path, category_tag):
        logger.error(f"{category_tag} [任务取消] 目录环境未就绪")
        return False

    api_url = f"{ALIST_HOST}/api/fs/add_offline_download"
    
    payload = {
        "path": save_path, 
        "urls": [url],
        "tool": "PikPak", 
        "delete_policy": "delete_on_upload_succeed"
    }

    logger.info(f"{category_tag} [离线下载] 正在提交任务...")
    try:
        # 使用封装的 alist_post_request 替代 requests.post
        response = alist_post_request(api_url, payload)

        # [DEBUG] 打印下载接口的响应
        logger.info(f"{category_tag} [离线下载 API] HTTP: {response.status_code} | Body: {response.text}")

        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get('code') == 200:
                task_ids = collect_task_ids_from_value(resp_json.get("data"))
                btih = extract_btih_from_magnet(url)
                logger.info(f"{category_tag} [离线下载] ✅ 任务添加成功! 目标: {save_path}")
                if task_ids:
                    logger.info(f"{category_tag} [离线下载] 任务 ID: {', '.join(task_ids)}")
                return {
                    "ok": True,
                    "task_ids": task_ids,
                    "btih": btih
                }
            else:
                logger.error(f"{category_tag} [离线下载] ❌ Alist 返回错误: {resp_json.get('message')}")
        else:
            logger.error(f"{category_tag} [离线下载] ❌ HTTP 错误: {response.status_code}")
    except Exception as e:
        logger.error(f"{category_tag} [离线下载] 连接异常: {e}")
    return False

def join_cloud_path(parent, child):
    """拼接云端路径。"""
    return f"{parent.rstrip('/')}/{child}"

def is_dir_entry(entry):
    """兼容 OpenList/Alist 目录字段。"""
    return bool(entry.get("is_dir") or entry.get("isDir"))

def get_entry_name(entry):
    """读取目录项名称。"""
    name = entry.get("name")
    if name is None:
        return ""
    return str(name)

def is_video_entry(entry):
    """判断目录项是否是常见视频文件。"""
    if is_dir_entry(entry):
        return False
    _, ext = os.path.splitext(get_entry_name(entry).lower())
    return ext in VIDEO_EXTENSIONS

def build_dir_snapshot(entries):
    """生成稳定性判断用的单层目录快照。"""
    snapshot = []
    for entry in entries:
        name = get_entry_name(entry)
        if not name:
            continue
        snapshot.append({
            "name": name,
            "is_dir": is_dir_entry(entry),
            "size": entry.get("size"),
            "modified": entry.get("modified")
        })
    return sorted(snapshot, key=lambda item: (not item["is_dir"], item["name"]))

def cleanup_empty_wrapper_dir(movie_path, wrapper_name, category_tag):
    """删除拍平后留下的空包装目录。"""
    wrapper_path = join_cloud_path(movie_path, wrapper_name)
    entries = alist_list_dir(wrapper_path, refresh=True)
    if entries is None:
        logger.info(f"{category_tag} [电影整理] 包装目录暂不可读，稍后重试删除: {wrapper_path}")
        return False

    if entries:
        logger.warning(f"{category_tag} [电影整理] 包装目录仍非空，暂不删除: {wrapper_path}")
        return False

    if alist_fs_remove(movie_path, [wrapper_name], category_tag):
        logger.info(f"{category_tag} [电影整理] 已删除空包装目录: {wrapper_path}")
        return True

    return False

def cleanup_empty_movie_dir(movie_path, category_tag):
    """内容等待超时后，保守删除最初创建的空电影目录。"""
    normalized_path = movie_path.rstrip("/")
    parent_path, movie_folder = normalized_path.rsplit("/", 1)
    if not parent_path:
        parent_path = "/"

    entries = alist_list_dir(normalized_path, refresh=True)
    if entries is None:
        logger.warning(f"{category_tag} [电影整理] 电影目录暂不可读，跳过删除: {normalized_path}")
        return False

    if entries:
        logger.warning(f"{category_tag} [电影整理] 电影目录非空，跳过删除: {normalized_path}")
        return False

    if alist_fs_remove(parent_path, [movie_folder], category_tag):
        logger.warning(f"{category_tag} [电影整理] 已删除空电影目录: {normalized_path}")
        return True

    return False

def wait_for_movie_content(movie_path, task, category_tag):
    """第一阶段: 等待电影根目录出现内容，空目录不参与稳定计数。"""
    content_wait_count = int(task.get("content_wait_count") or 0)

    while content_wait_count < MOVIE_CONTENT_MAX_ATTEMPTS:
        entries = alist_list_dir(movie_path, refresh=True)
        content_wait_count += 1
        update_movie_flatten_task(movie_path, {
            "last_checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "content_wait_count": content_wait_count
        })

        if entries:
            logger.info(
                f"{category_tag} [电影整理] 已检测到目录内容，进入稳定判定: "
                f"{movie_path} ({content_wait_count}/{MOVIE_CONTENT_MAX_ATTEMPTS})"
            )
            update_movie_flatten_task(movie_path, {
                "content_seen_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "last_snapshot": None,
                "stable_count": 0
            })
            return entries

        if entries is None:
            logger.info(
                f"{category_tag} [电影整理] 目标目录暂不可读，等待内容出现: "
                f"{movie_path} ({content_wait_count}/{MOVIE_CONTENT_MAX_ATTEMPTS})"
            )
        else:
            logger.info(
                f"{category_tag} [电影整理] 目标目录仍为空，等待内容出现: "
                f"{movie_path} ({content_wait_count}/{MOVIE_CONTENT_MAX_ATTEMPTS})"
            )

        if content_wait_count < MOVIE_CONTENT_MAX_ATTEMPTS:
            time.sleep(MOVIE_CONTENT_POLL_INTERVAL)

    diagnose_movie_offline_status(movie_path, task)
    cleanup_empty_movie_dir(movie_path, category_tag)
    remove_movie_flatten_task(movie_path, "内容出现等待超时，已记录离线任务诊断")
    return None

def flatten_movie_wrapper_once(movie_path, task):
    """
    对单个电影规范目录执行一次保守型单层拍平检查。
    仅当可明确识别唯一包装子目录时才移动，兼容根目录已有其它版本视频。
    """
    category_tag = "[Movie]"
    movie_path = movie_path.rstrip("/")

    cleanup_wrapper_name = task.get("cleanup_wrapper_name")
    if cleanup_wrapper_name:
        if cleanup_empty_wrapper_dir(movie_path, cleanup_wrapper_name, category_tag):
            remove_movie_flatten_task(movie_path, f"已清理空包装目录 {cleanup_wrapper_name}")
        return

    if not task.get("content_seen_at"):
        entries = wait_for_movie_content(movie_path, task, category_tag)
        if entries is None:
            return
    else:
        entries = alist_list_dir(movie_path, refresh=True)
        if entries is None:
            logger.info(f"{category_tag} [电影整理] 目标目录暂不可读，等待下载完成: {movie_path}")
            return
        if not entries:
            logger.info(f"{category_tag} [电影整理] 内容曾出现但当前为空，稍后重试: {movie_path}")
            return

    snapshot = build_dir_snapshot(entries)
    last_snapshot = task.get("last_snapshot")
    stable_count = int(task.get("stable_count") or 0)
    if snapshot == last_snapshot:
        stable_count += 1
    else:
        stable_count = 1

    update_movie_flatten_task(movie_path, {
        "last_checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "last_snapshot": snapshot,
        "stable_count": stable_count
    })

    if stable_count < MOVIE_FLATTEN_STABLE_CHECKS:
        logger.info(
            f"{category_tag} [电影整理] 等待目录稳定: {movie_path} "
            f"({stable_count}/{MOVIE_FLATTEN_STABLE_CHECKS})"
        )
        return

    dir_entries = [entry for entry in entries if is_dir_entry(entry)]

    if not dir_entries:
        remove_movie_flatten_task(movie_path, "内容已出现但无包装目录，结束拍平任务")
        return

    if len(dir_entries) != 1:
        remove_movie_flatten_task(movie_path, "存在多个目录子项，无法确定新增包装目录，保守跳过")
        return

    wrapper_name = get_entry_name(dir_entries[0])
    if not wrapper_name:
        remove_movie_flatten_task(movie_path, "包装目录名称为空，保守跳过")
        return

    wrapper_path = join_cloud_path(movie_path, wrapper_name)
    child_entries = alist_list_dir(wrapper_path, refresh=True)
    if child_entries is None:
        logger.info(f"{category_tag} [电影整理] 包装目录暂不可读，稍后重试: {wrapper_path}")
        return

    child_names = [get_entry_name(entry) for entry in child_entries if get_entry_name(entry)]
    if not child_names:
        remove_movie_flatten_task(movie_path, "包装目录为空，保守跳过")
        return

    if not alist_fs_move(wrapper_path, movie_path, child_names, category_tag):
        logger.warning(f"{category_tag} [电影整理] 包装目录内容移动失败，稍后重试: {wrapper_path}")
        return

    update_movie_flatten_task(movie_path, {"cleanup_wrapper_name": wrapper_name})
    if cleanup_empty_wrapper_dir(movie_path, wrapper_name, category_tag):
        remove_movie_flatten_task(movie_path, f"已拍平单层包装目录 {wrapper_name}")

def process_movie_flatten_tasks():
    """处理少量电影拍平任务，避免影响主循环性能。"""
    if not MOVIE_FLATTEN_ENABLED:
        return

    with MOVIE_FLATTEN_TASKS_LOCK:
        tasks = list(MOVIE_FLATTEN_TASKS.items())[:MOVIE_FLATTEN_MAX_TASKS_PER_LOOP]

    for movie_path, task in tasks:
        try:
            flatten_movie_wrapper_once(movie_path, task)
        except Exception as e:
            logger.error(f"[Movie] [电影整理] 未捕获异常: {movie_path} | {e}")
            logger.error(traceback.format_exc())

def process_single_dir(watch_dir, cloud_base_path, category_name):
    """
    处理单个监控目录
    """
    category_tag = f"[{category_name}]"
    
    if not os.path.exists(watch_dir):
        logger.warning(f"{category_tag} 监控目录不存在，跳过: {watch_dir}")
        return

    # 确保归档根目录存在
    if not os.path.exists(PROCESSED_DIR):
        os.makedirs(PROCESSED_DIR)

    files = sorted([f for f in os.listdir(watch_dir) if not f.startswith('.')])

    for filename in files:
        file_path = os.path.join(watch_dir, filename)
        # 避免处理归档目录
        if file_path == PROCESSED_DIR or os.path.isdir(file_path): 
            continue

        logger.info(f"{category_tag} 发现新文件: {filename}")
        
        success = False
        magnet = None
        target_path = cloud_base_path
        pending_release_key = None
        pending_movie_task = None
        is_movie = category_name.lower() == "movie"

        if is_movie:
            pending_release_key, pending_movie_task = find_pending_movie_task(filename, category_tag)
            if not pending_movie_task:
                continue
        
        lower_filename = filename.lower()
        if lower_filename.endswith(".torrent"):
            magnet = get_magnet_from_torrent(file_path, category_tag)
        elif lower_filename.endswith(".magnet") or lower_filename.endswith(".txt"):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    magnet_match = re.search(r'magnet:\?[^\s]+', content)
                    if magnet_match:
                        magnet = magnet_match.group(0)
                        logger.info(f"{category_tag} [读取文本] 成功提取磁力链接")
            except Exception as e:
                logger.error(f"{category_tag} [读取文本] 读取失败: {e}")
            
        if magnet:
            if pending_movie_task:
                target_path = get_movie_save_path_from_task(pending_movie_task, cloud_base_path, category_tag)
            else:
                target_path = get_save_path(filename, cloud_base_path, category_tag)
            # 修正：传递 cloud_base_path 给 add_offline_download
            success = add_offline_download(magnet, target_path, cloud_base_path, category_tag)
        else:
            logger.warning(f"{category_tag} 无法提取磁力链接，跳过文件: {filename}")
        
        if success:
            if pending_movie_task:
                remove_pending_movie_task(pending_release_key, category_tag)
                register_movie_flatten_task(
                    target_path,
                    pending_movie_task.get("movie"),
                    category_tag,
                    success
                )

            try:
                # 归档逻辑
                relative_path = ""
                if target_path.startswith(cloud_base_path):
                    relative_path = target_path[len(cloud_base_path):].strip("/")
                
                # 组合本地归档路径
                local_archive_dir = os.path.join(PROCESSED_DIR, category_name, relative_path)
                
                if not os.path.exists(local_archive_dir):
                    os.makedirs(local_archive_dir)
                    
                destination = os.path.join(local_archive_dir, filename)
                shutil.move(file_path, destination)
                logger.info(f"{category_tag} [本地归档] ✅ 文件已移至: {local_archive_dir}/{filename}")
                logger.info("-" * 50) 
                
            except Exception as e:
                logger.error(f"{category_tag} [本地归档] 移动失败: {e}")
                logger.error(traceback.format_exc())

def main():
    logger.info(">>> 自动分类脚本启动 (Token 自动刷新版) <<<")
    logger.info(f"归档总目录: {PROCESSED_DIR}")
    logger.info(f"Alist Host: {ALIST_HOST}")
    load_pending_tasks()
    load_movie_flatten_tasks()
    webhook_server = start_webhook_server()
    
    # 打印监控配置
    for cat, conf in WATCH_CONFIG.items():
        logger.info(f"配置 [{cat}]: 监控 {conf['local']} -> 上传至 {conf['cloud']}")
    
    if not login_and_update_token():
        logger.error(">>> 启动时登录失败，将在任务中自动重试 <<<")

    while True:
        try:
            # 遍历配置的每一个监控目录
            for category, config in WATCH_CONFIG.items():
                process_single_dir(
                    watch_dir=config['local'],
                    cloud_base_path=config['cloud'],
                    category_name=category
                )
            process_movie_flatten_tasks()
        except KeyboardInterrupt:
            logger.info("用户停止脚本")
            if webhook_server:
                webhook_server.shutdown()
            break
        except Exception as e:
            logger.error(f"主循环发生未捕获异常: {e}")
            logger.error(traceback.format_exc())
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
