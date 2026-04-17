import time
import traceback
from datetime import datetime

import state
from alist_client import (
    alist_cancel_task,
    alist_fs_move,
    alist_fs_remove,
    alist_get_undone_tasks,
    alist_list_dir,
    task_contains_btih,
)
from config import (
    MOVIE_CONTENT_MAX_ATTEMPTS,
    MOVIE_CONTENT_POLL_INTERVAL,
    MOVIE_FLATTEN_ENABLED,
    MOVIE_FLATTEN_MAX_TASKS_PER_LOOP,
    MOVIE_FLATTEN_STABLE_CHECKS,
    MOVIE_FLATTEN_TASK_FILE,
    logger,
)
from path_utils import (
    build_dir_snapshot,
    get_entry_name,
    is_dir_entry,
    join_cloud_path,
    sanitize_path_component,
)

import json
import os


def save_movie_flatten_tasks_locked():
    """持久化电影目录拍平任务。调用方需持有 MOVIE_FLATTEN_TASKS_LOCK。"""
    task_dir = os.path.dirname(MOVIE_FLATTEN_TASK_FILE)
    if task_dir:
        os.makedirs(task_dir, exist_ok=True)

    tmp_path = f"{MOVIE_FLATTEN_TASK_FILE}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state.MOVIE_FLATTEN_TASKS, f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, MOVIE_FLATTEN_TASK_FILE)


def load_movie_flatten_tasks():
    """启动时加载未完成的电影目录拍平任务。"""
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

        with state.MOVIE_FLATTEN_TASKS_LOCK:
            state.MOVIE_FLATTEN_TASKS.clear()
            state.MOVIE_FLATTEN_TASKS.update(
                {
                    key: task
                    for key, task in data.items()
                    if isinstance(key, str) and isinstance(task, dict)
                }
            )

        logger.info(f"[电影整理] 已加载待处理任务: {len(state.MOVIE_FLATTEN_TASKS)} 个")
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
        "btih": offline_info.get("btih"),
    }

    with state.MOVIE_FLATTEN_TASKS_LOCK:
        if normalized_path in state.MOVIE_FLATTEN_TASKS:
            existing = state.MOVIE_FLATTEN_TASKS[normalized_path]
            existing["movie"] = movie_info or existing.get("movie") or {}
            existing["offline_task_ids"] = offline_info.get("task_ids") or existing.get("offline_task_ids") or []
            existing["btih"] = offline_info.get("btih") or existing.get("btih")
            existing["content_wait_count"] = 0
            existing["content_seen_at"] = None
            existing["last_snapshot"] = None
            existing["stable_count"] = 0
            state.MOVIE_FLATTEN_TASKS[normalized_path] = existing
        else:
            state.MOVIE_FLATTEN_TASKS[normalized_path] = task
        save_movie_flatten_tasks_locked()

    logger.info(f"{category_tag} [电影整理] 已登记下载完成后拍平检查: {normalized_path}")


def remove_movie_flatten_task(movie_path, reason):
    """移除已完成或已判定无需处理的电影目录拍平任务。"""
    normalized_path = movie_path.rstrip("/")

    with state.MOVIE_FLATTEN_TASKS_LOCK:
        if normalized_path not in state.MOVIE_FLATTEN_TASKS:
            return
        state.MOVIE_FLATTEN_TASKS.pop(normalized_path)
        save_movie_flatten_tasks_locked()

    logger.info(f"[电影整理] 任务结束: {normalized_path} | {reason}")


def update_movie_flatten_task(movie_path, updates):
    """更新电影目录拍平任务状态。"""
    normalized_path = movie_path.rstrip("/")

    with state.MOVIE_FLATTEN_TASKS_LOCK:
        task = state.MOVIE_FLATTEN_TASKS.get(normalized_path)
        if not task:
            return
        task.update(updates)
        state.MOVIE_FLATTEN_TASKS[normalized_path] = task
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
        update_movie_flatten_task(
            movie_path,
            {
                "last_checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "content_wait_count": content_wait_count,
            },
        )

        if entries:
            logger.info(
                f"{category_tag} [电影整理] 已检测到目录内容，进入稳定判定: "
                f"{movie_path} ({content_wait_count}/{MOVIE_CONTENT_MAX_ATTEMPTS})"
            )
            update_movie_flatten_task(
                movie_path,
                {
                    "content_seen_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    "last_snapshot": None,
                    "stable_count": 0,
                },
            )
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

    update_movie_flatten_task(
        movie_path,
        {
            "last_checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "last_snapshot": snapshot,
            "stable_count": stable_count,
        },
    )

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

    with state.MOVIE_FLATTEN_TASKS_LOCK:
        tasks = list(state.MOVIE_FLATTEN_TASKS.items())[:MOVIE_FLATTEN_MAX_TASKS_PER_LOOP]

    for movie_path, task in tasks:
        try:
            flatten_movie_wrapper_once(movie_path, task)
        except Exception as e:
            logger.error(f"[Movie] [电影整理] 未捕获异常: {movie_path} | {e}")
            logger.error(traceback.format_exc())
