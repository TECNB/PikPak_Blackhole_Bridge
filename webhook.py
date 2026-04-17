import json
import os
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

import state
from config import (
    PENDING_TASK_FILE,
    WEBHOOK_HOST,
    WEBHOOK_MAX_BODY_BYTES,
    WEBHOOK_PATHS,
    WEBHOOK_PORT,
    logger,
)
from path_utils import get_file_release_key, normalize_release_title


def save_pending_tasks_locked():
    """持久化 pending webhook 任务。调用方需持有 PENDING_TASKS_LOCK。"""
    pending_dir = os.path.dirname(PENDING_TASK_FILE)
    if pending_dir:
        os.makedirs(pending_dir, exist_ok=True)

    tmp_path = f"{PENDING_TASK_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state.PENDING_TASKS, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, PENDING_TASK_FILE)


def load_pending_tasks():
    """启动时加载未匹配的 Radarr Grab webhook 任务。"""
    if not os.path.exists(PENDING_TASK_FILE):
        logger.info(f"[Webhook] Pending 任务文件不存在，将在首次写入时创建: {PENDING_TASK_FILE}")
        return

    try:
        with open(PENDING_TASK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)

        if not isinstance(data, dict):
            logger.warning(f"[Webhook] Pending 任务文件格式异常，已忽略: {PENDING_TASK_FILE}")
            return

        with state.PENDING_TASKS_LOCK:
            state.PENDING_TASKS.clear()
            state.PENDING_TASKS.update(
                {
                    key: task
                    for key, task in data.items()
                    if isinstance(key, str) and isinstance(task, dict)
                }
            )

        logger.info(f"[Webhook] 已加载 Radarr pending 任务: {len(state.PENDING_TASKS)} 个")
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
            "folderPath": movie.get("folderPath"),
        },
        "release": {
            "quality": release.get("quality"),
            "releaseGroup": release.get("releaseGroup"),
            "indexer": release.get("indexer"),
            "size": release.get("size"),
        },
    }

    with state.PENDING_TASKS_LOCK:
        state.PENDING_TASKS[release_key] = task
        save_pending_tasks_locked()

    logger.info(f"[Webhook] 已记录 Radarr Grab: {movie_title} ({movie_year}) | releaseTitle: {release_title}")
    return {"ignored": False, "release_key": release_key, "movie": f"{movie_title} ({movie_year})"}


def find_pending_movie_task(filename, category_tag):
    """按标准化后的 releaseTitle 查找黑洞文件对应的 Radarr Grab 任务。"""
    release_key = get_file_release_key(filename)
    if not release_key:
        return None, None

    with state.PENDING_TASKS_LOCK:
        task = state.PENDING_TASKS.get(release_key)

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

    with state.PENDING_TASKS_LOCK:
        if release_key not in state.PENDING_TASKS:
            return
        task = state.PENDING_TASKS.pop(release_key)
        save_pending_tasks_locked()

    movie = task.get("movie") or {}
    logger.info(f"{category_tag} [Webhook匹配] 已清理 pending 任务: {movie.get('title')} ({movie.get('year')})")


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
