import json
import re
import time
from urllib.parse import quote

import requests

import state
from config import (
    ALIST_HOST,
    ALIST_PASSWORD,
    ALIST_USERNAME,
    CD2_HOST,
    CD2_REFRESH_ENABLED,
    CD2_REFRESH_MAX_ATTEMPTS,
    CD2_REFRESH_POLL_INTERVAL,
    CD2_TOKEN,
    MOVIE_FLATTEN_LIST_PER_PAGE,
    PATH_READY_MAX_ATTEMPTS,
    PATH_READY_POLL_INTERVAL,
    logger,
)


def login_and_update_token():
    """
    登录 Alist 并更新全局 Token
    """
    api_url = f"{ALIST_HOST}/api/auth/login"
    payload = {
        "username": ALIST_USERNAME,
        "password": ALIST_PASSWORD,
    }

    try:
        logger.info("[身份验证] 正在尝试登录 Alist...")
        response = requests.post(api_url, json=payload)
        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 200:
                token = data["data"]["token"]
                state.CURRENT_TOKEN = token
                logger.info("[身份验证] ✅ 登录成功，Token 已更新")
                return True
            logger.error(f"[身份验证] ❌ 登录失败: {data.get('message')}\n完整响应: {response.text}")
        else:
            logger.error(f"[身份验证] HTTP 错误: {response.status_code}\n完整响应: {response.text}")
    except Exception as e:
        logger.error(f"[身份验证] 连接异常: {e}")

    return False


def get_auth_header():
    """获取带 Token 的 Header，如果无 Token 则尝试登录"""
    if not state.CURRENT_TOKEN:
        login_and_update_token()
    return {"Authorization": state.CURRENT_TOKEN, "Content-Type": "application/json"}


def get_static_auth_header(token):
    """获取固定 Token 的 Header。"""
    return {"Authorization": token, "Content-Type": "application/json"}


def alist_post_request(url, payload, retry=True):
    """
    通用 POST 请求封装，负责自动刷新过期 Token 并重试一次。
    """
    headers = get_auth_header()

    try:
        response = requests.post(url, json=payload, headers=headers)
    except requests.exceptions.RequestException as e:
        raise e

    token_expired = False

    if response.status_code == 401:
        token_expired = True
    elif response.status_code == 200:
        try:
            data = response.json()
            code = data.get("code")
            msg = data.get("message", "").lower()
            if code != 200 and ("token is expired" in msg or "token 无效" in msg):
                token_expired = True
        except ValueError:
            pass

    if token_expired and retry:
        logger.warning(f"[自动恢复] 检测到 Token 失效 (HTTP {response.status_code})，正在尝试刷新...")
        if login_and_update_token():
            logger.info("[自动恢复] Token 刷新成功，正在重发请求...")
            return alist_post_request(url, payload, retry=False)
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
            code = data.get("code")
            msg = data.get("message", "").lower()
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


def static_post_request(url, payload, token):
    """固定 Token 的通用 POST 请求封装。"""
    headers = get_static_auth_header(token)
    return requests.post(url, json=payload, headers=headers, timeout=20)


def alist_get_path_info(path, refresh=True):
    """调用 OpenList/Alist fs/get 获取路径信息，默认强制刷新缓存。"""
    api_url = f"{ALIST_HOST}/api/fs/get"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 0,
        "refresh": refresh,
    }

    try:
        response = alist_post_request(api_url, payload)

        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 200:
                return data.get("data") or {}
            logger.info(f"[API Get] 路径暂不可用: {path} | Resp: {data}")
            return None

        logger.warning(f"[API Get] HTTP异常 {response.status_code} | Path: {path} | Resp: {response.text}")
        return None
    except Exception as e:
        logger.warning(f"[API Get] 请求异常: {e}")
    return None


def cd2_get_path_info(path, refresh=True):
    """调用 CD2 的 fs/get 获取路径信息。"""
    api_url = f"{CD2_HOST}/api/fs/get"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 0,
        "refresh": refresh,
    }

    try:
        response = static_post_request(api_url, payload, CD2_TOKEN)

        if response.status_code == 200:
            data = response.json()
            if data.get("code") == 200:
                return data.get("data") or {}
            logger.info(f"[CD2 Get] 路径暂不可用: {path} | Resp: {data}")
            return None

        logger.warning(f"[CD2 Get] HTTP异常 {response.status_code} | Path: {path} | Resp: {response.text}")
        return None
    except Exception as e:
        logger.warning(f"[CD2 Get] 请求异常: {e}")
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


def cd2_list_dir(path, refresh=True, per_page=1):
    """列出 CD2 目录内容，并可借此触发 refresh。"""
    api_url = f"{CD2_HOST}/api/fs/list"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": per_page,
        "refresh": refresh,
    }

    try:
        logger.info(f"[CD2 List] 正在读取目录内容: {path}")
        resp = static_post_request(api_url, payload, CD2_TOKEN)

        if resp.status_code != 200:
            logger.warning(f"[CD2 List] HTTP 错误: {resp.status_code} | Path: {path} | Body: {resp.text}")
            return None

        data = resp.json()
        if data.get("code") != 200:
            logger.info(f"[CD2 List] 目录暂不可读: {path} | Resp: {data}")
            return None

        result = data.get("data") or {}
        content = result.get("content")
        if content is None:
            return []
        if not isinstance(content, list):
            logger.warning(f"[CD2 List] 返回 content 格式异常: {path} | Content: {content}")
            return None
        return content
    except Exception as e:
        logger.warning(f"[CD2 List] 读取目录失败 {path}: {e}")
        return None


def confirm_cd2_movie_path_ready(movie_path, category_tag):
    """
    可选的 CD2 刷新确认:
    拍平完成或确认无需拍平后，刷新父目录并短轮询确认最终影片目录已可见。
    """
    if not CD2_REFRESH_ENABLED:
        return None

    if not CD2_HOST or not CD2_TOKEN:
        logger.warning(f"{category_tag} [CD2刷新] 已启用但缺少 CD2_HOST/CD2_TOKEN，跳过确认: {movie_path}")
        return False

    normalized_path = movie_path.rstrip("/")
    if not normalized_path:
        logger.warning(f"{category_tag} [CD2刷新] 影片路径为空，跳过确认")
        return False

    if "/" in normalized_path:
        parent_path, _ = normalized_path.rsplit("/", 1)
        if not parent_path:
            parent_path = "/"
    else:
        parent_path = "/"

    for attempt in range(1, CD2_REFRESH_MAX_ATTEMPTS + 1):
        cd2_list_dir(parent_path, refresh=True, per_page=1)
        path_info = cd2_get_path_info(normalized_path, refresh=True)
        entries = cd2_list_dir(normalized_path, refresh=True, per_page=MOVIE_FLATTEN_LIST_PER_PAGE)

        if path_info is not None and entries is not None:
            logger.info(
                f"{category_tag} [CD2刷新] CD2 已刷新并看到最终影片目录: "
                f"{normalized_path} | 子项 {len(entries)} 个 "
                f"({attempt}/{CD2_REFRESH_MAX_ATTEMPTS})"
            )
            return True

        if attempt < CD2_REFRESH_MAX_ATTEMPTS:
            logger.info(
                f"{category_tag} [CD2刷新] CD2 暂未看到最终影片目录，等待重试: "
                f"{normalized_path} ({attempt}/{CD2_REFRESH_MAX_ATTEMPTS})"
            )
            time.sleep(CD2_REFRESH_POLL_INTERVAL)

    logger.warning(
        f"{category_tag} [CD2刷新] CD2 未找到影片目录: "
        f"{normalized_path} ({CD2_REFRESH_MAX_ATTEMPTS} 次)"
    )
    return False


def alist_fs_list(path, refresh=True):
    """
    强制刷新 Alist 缓存
    """
    api_url = f"{ALIST_HOST}/api/fs/list"
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 1,
        "refresh": refresh,
    }
    try:
        logger.info(f"[API List] 正在刷新目录缓存: {path}")
        resp = alist_post_request(api_url, payload)

        if resp.status_code != 200:
            logger.error(f"[API List] HTTP 错误: {resp.status_code} | Body: {resp.text}")
        else:
            data = resp.json()
            if data.get("code") != 200:
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
        "refresh": refresh,
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
        "names": names,
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
        "names": names,
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

    match = re.search(r"xt=urn:btih:([A-Za-z0-9]+)", magnet, re.IGNORECASE)
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
            logger.warning(
                f"[Task API] 取消任务 HTTP 错误: {resp.status_code} | "
                f"Type: {task_type} | ID: {task_id} | Body: {resp.text}"
            )
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


def ensure_path_ready(full_path, skip_prefix_path, category_tag):
    """
    逐级创建目录 (智能跳过基础路径版 + 详细Debug日志)
    """
    logger.info(f"{category_tag} ------ 开始检查云端路径: {full_path} ------")

    parts = [p for p in full_path.split("/") if p]
    current_path = ""

    norm_skip_prefix = skip_prefix_path.rstrip("/")

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
        logger.info(f"{category_tag} [Step {i + 1}] 目录不存在，正在创建: {current_path}")
        mkdir_url = f"{ALIST_HOST}/api/fs/mkdir"

        try:
            resp = alist_post_request(mkdir_url, {"path": current_path})
            logger.info(f"{category_tag} [Mkdir API] HTTP: {resp.status_code} | Body: {resp.text}")

            try:
                resp_json = resp.json()
                if resp_json.get("code") != 200:
                    logger.error(f"{category_tag} [Mkdir Error] 创建指令失败! 错误信息: {resp_json.get('message')}")
            except Exception:
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
    """发送离线下载任务"""
    if not ensure_path_ready(save_path, cloud_base_path, category_tag):
        logger.error(f"{category_tag} [任务取消] 目录环境未就绪")
        return False

    api_url = f"{ALIST_HOST}/api/fs/add_offline_download"

    payload = {
        "path": save_path,
        "urls": [url],
        "tool": "PikPak",
        "delete_policy": "delete_on_upload_succeed",
    }

    logger.info(f"{category_tag} [离线下载] 正在提交任务...")
    try:
        response = alist_post_request(api_url, payload)
        logger.info(f"{category_tag} [离线下载 API] HTTP: {response.status_code} | Body: {response.text}")

        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get("code") == 200:
                task_ids = collect_task_ids_from_value(resp_json.get("data"))
                btih = extract_btih_from_magnet(url)
                logger.info(f"{category_tag} [离线下载] ✅ 任务添加成功! 目标: {save_path}")
                if task_ids:
                    logger.info(f"{category_tag} [离线下载] 任务 ID: {', '.join(task_ids)}")
                return {
                    "ok": True,
                    "task_ids": task_ids,
                    "btih": btih,
                }
            logger.error(f"{category_tag} [离线下载] ❌ Alist 返回错误: {resp_json.get('message')}")
        else:
            logger.error(f"{category_tag} [离线下载] ❌ HTTP 错误: {response.status_code}")
    except Exception as e:
        logger.error(f"{category_tag} [离线下载] 连接异常: {e}")
    return False
