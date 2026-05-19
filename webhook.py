import json
import os
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlparse

import state
from autosymlink_client import evaluate_ani_rss_autosymlink_payload, schedule_autosymlink_refresh
from config import (
    ANI_RSS_AUTOSYMLINK_WEBHOOK_PATH,
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


class WebhookRequestError(Exception):
    def __init__(self, status_code, message):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


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

        # ani-rss 下载完成刷新是专用路径，先分流，避免被 Radarr Grab 校验误伤。
        if path == ANI_RSS_AUTOSYMLINK_WEBHOOK_PATH:
            self.handle_ani_rss_autosymlink()
            return

        if path not in WEBHOOK_PATHS:
            write_json_response(self, 404, {"ok": False, "error": "not found"})
            return

        self.handle_radarr_grab()

    def read_json_payload(self):
        """统一处理 JSON 协议校验；协议错误才返回 4xx。"""

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            raise WebhookRequestError(400, "invalid Content-Length")

        if length <= 0:
            raise WebhookRequestError(400, "empty body")
        if length > WEBHOOK_MAX_BODY_BYTES:
            raise WebhookRequestError(413, "body too large")

        try:
            raw_body = self.rfile.read(length)
            payload = json.loads(raw_body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON body must be an object")
            return payload
        except json.JSONDecodeError as e:
            raise WebhookRequestError(400, f"invalid json: {e}")
        except ValueError as e:
            raise WebhookRequestError(400, str(e))

    def handle_radarr_grab(self):
        """处理既有 Radarr Grab webhook，保持原有行为。"""

        try:
            payload = self.read_json_payload()
            result = register_radarr_grab(payload)
            write_json_response(self, 200, {"ok": True, **result})
        except WebhookRequestError as e:
            write_json_response(self, e.status_code, {"ok": False, "error": e.message})
        except ValueError as e:
            write_json_response(self, 400, {"ok": False, "error": str(e)})
        except Exception as e:
            logger.error(f"[Webhook] 处理请求失败: {e}")
            logger.error(traceback.format_exc())
            write_json_response(self, 500, {"ok": False, "error": "internal server error"})

    def handle_ani_rss_autosymlink(self):
        """处理 ani-rss 下载完成通知，并按集数规则决定是否安排 AS 刷新。"""

        try:
            payload = self.read_json_payload()
            decision = evaluate_ani_rss_autosymlink_payload(payload)

            title = payload.get("title") or "未知番剧"
            if decision.ignored:
                # 业务跳过也返回 200，避免 ani-rss 把“中间集不刷新”记成通知失败。
                log_method = logger.info if decision.reason == "middle episode" else logger.warning
                log_method(
                    "[Auto_Symlink] 已忽略 ani-rss 下载完成 webhook: "
                    f"reason={decision.reason} title={title} "
                    f"episode={payload.get('episode')} total={payload.get('totalEpisodeNumber')}"
                )
                write_json_response(
                    self,
                    200,
                    {
                        "ok": True,
                        "ignored": True,
                        "reason": decision.reason,
                        "episode": decision.episode,
                        "totalEpisodeNumber": decision.total_episode_number,
                    },
                )
                return

            # 调度成功只代表已安排延迟任务，不代表 Auto_Symlink 已经执行完成。
            schedule = schedule_autosymlink_refresh(payload, decision)
            write_json_response(
                self,
                200,
                {
                    "ok": True,
                    "ignored": False,
                    "scheduled": True,
                    "reason": decision.reason,
                    "trigger": decision.trigger,
                    "episode": decision.episode,
                    "totalEpisodeNumber": decision.total_episode_number,
                    **schedule,
                },
            )
        except WebhookRequestError as e:
            write_json_response(self, e.status_code, {"ok": False, "error": e.message})
        except Exception as e:
            logger.error(f"[Auto_Symlink] 处理 ani-rss webhook 失败: {e}")
            logger.error(traceback.format_exc())
            write_json_response(self, 500, {"ok": False, "error": "internal server error"})

    def log_message(self, fmt, *args):
        logger.debug(f"[Webhook] {self.address_string()} - {fmt % args}")


def start_webhook_server():
    """启动 webhook HTTP 服务。"""
    try:
        server = ThreadingHTTPServer((WEBHOOK_HOST, WEBHOOK_PORT), WebhookRequestHandler)
    except OSError as e:
        logger.error(f"[Webhook] 启动失败 {WEBHOOK_HOST}:{WEBHOOK_PORT}: {e}")
        return None

    thread = Thread(target=server.serve_forever, name="webhook-server", daemon=True)
    thread.start()
    paths = ", ".join(sorted([*WEBHOOK_PATHS, ANI_RSS_AUTOSYMLINK_WEBHOOK_PATH]))
    logger.info(f"[Webhook] 已启动: http://{WEBHOOK_HOST}:{WEBHOOK_PORT} ({paths})")
    return server
