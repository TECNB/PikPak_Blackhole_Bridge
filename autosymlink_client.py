import json
import time
import traceback
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from threading import Thread
from typing import Optional
from urllib.parse import quote

import requests

from config import (
    AUTOSYMLINK_API_KEY,
    AUTOSYMLINK_BASE_URL,
    AUTOSYMLINK_COOKIE,
    AUTOSYMLINK_NORMAL_DELAY_SECONDS,
    AUTOSYMLINK_REQUEST_BODY_JSON,
    AUTOSYMLINK_REQUEST_TIMEOUT_SECONDS,
    AUTOSYMLINK_RETRY_COUNT,
    AUTOSYMLINK_RETRY_DELAY_SECONDS,
    AUTOSYMLINK_TASK_UUID,
    logger,
)


@dataclass(frozen=True)
class AutoSymlinkDecision:
    """ani-rss webhook 是否需要触发 Auto_Symlink 刷新的判定结果。"""

    should_refresh: bool
    ignored: bool
    reason: str
    episode: Optional[int] = None
    total_episode_number: Optional[int] = None
    trigger: Optional[str] = None


def parse_positive_integer_episode(value, field_name):
    """解析 ani-rss 集数字段，只接受正整数，拒绝 0、小数和异常值。"""

    # ani-rss WebHook 模板通常把变量渲染成字符串；Decimal 可以稳定区分
    # "1"、"1.0"、"1.5"、"NaN"，避免 float 带来的隐式取整或精度问题。
    if value is None or isinstance(value, bool):
        raise ValueError(f"{field_name} missing")

    try:
        number = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError):
        raise ValueError(f"{field_name} invalid")

    if not number.is_finite():
        raise ValueError(f"{field_name} invalid")
    if number <= 0:
        raise ValueError(f"{field_name} must be positive")
    if number != number.to_integral_value():
        raise ValueError(f"{field_name} must be an integer")

    return int(number)


def evaluate_ani_rss_autosymlink_payload(payload):
    """执行 MVP 刷新规则：只在首集或声明总集数的最后一集刷新。"""

    # 缺字段、0、小数、解析失败都属于业务跳过，不让 ani-rss 认为 WebHook 失败。
    try:
        episode = parse_positive_integer_episode(payload.get("episode"), "episode")
    except ValueError as e:
        return AutoSymlinkDecision(False, True, str(e))

    try:
        total_episode_number = parse_positive_integer_episode(
            payload.get("totalEpisodeNumber"),
            "totalEpisodeNumber",
        )
    except ValueError as e:
        return AutoSymlinkDecision(False, True, str(e), episode=episode)

    # 单集番会先命中首集规则，同一次 WebHook 只安排一次刷新。
    if episode == 1:
        return AutoSymlinkDecision(
            True,
            False,
            "first episode",
            episode=episode,
            total_episode_number=total_episode_number,
            trigger="first_episode",
        )

    # totalEpisodeNumber 是订阅声明总集数，不证明中间集完整；这里只按 MVP 做末集刷新。
    if episode >= total_episode_number:
        return AutoSymlinkDecision(
            True,
            False,
            "final episode",
            episode=episode,
            total_episode_number=total_episode_number,
            trigger="final_episode",
        )

    # 中间集不刷新，避免 Auto_Symlink 被每集下载完成通知打爆。
    return AutoSymlinkDecision(
        False,
        True,
        "middle episode",
        episode=episode,
        total_episode_number=total_episode_number,
    )


def schedule_autosymlink_refresh(payload, decision):
    """把刷新任务放入内存线程，立刻给 ani-rss 返回 200。"""

    title = str(payload.get("title") or "未知番剧")
    season = payload.get("season")
    job_name = f"as-refresh-ep{decision.episode}-total{decision.total_episode_number}-{int(time.time())}"
    context = {
        "title": title,
        "season": season,
        "episode": decision.episode,
        "total_episode_number": decision.total_episode_number,
        "trigger": decision.trigger,
        "job_name": job_name,
    }

    # MVP 接受容器重启会丢失未执行延迟任务；因此用 daemon 线程而不是数据库/队列。
    thread = Thread(
        target=run_delayed_autosymlink_refresh,
        args=(context,),
        name=job_name,
        daemon=True,
    )
    thread.start()

    logger.info(
        "[Auto_Symlink] 已安排延迟刷新: "
        f"title={title} season={season} episode={decision.episode}/"
        f"{decision.total_episode_number} trigger={decision.trigger} "
        f"delay={AUTOSYMLINK_NORMAL_DELAY_SECONDS}s job={job_name}"
    )
    return {"job": job_name, "delay_seconds": AUTOSYMLINK_NORMAL_DELAY_SECONDS}


def run_delayed_autosymlink_refresh(context):
    """等待挂载缓存趋于稳定后，再调用 Auto_Symlink 手动刷新。"""

    delay = max(AUTOSYMLINK_NORMAL_DELAY_SECONDS, 0)
    if delay:
        time.sleep(delay)

    try:
        trigger_autosymlink_refresh_with_retries(context)
    except Exception as e:
        logger.error(f"[Auto_Symlink] 延迟刷新任务异常: {e}")
        logger.error(traceback.format_exc())


def trigger_autosymlink_refresh_with_retries(context):
    """执行一次刷新加有限重试，重试次数来自 AUTOSYMLINK_RETRY_COUNT。"""

    # retry_count 表示失败后的额外重试次数，所以总尝试次数要 +1。
    attempts = AUTOSYMLINK_RETRY_COUNT + 1
    for attempt in range(1, attempts + 1):
        if trigger_autosymlink_refresh_once(context):
            return True

        if attempt < attempts:
            logger.warning(
                "[Auto_Symlink] 刷新失败，等待重试: "
                f"{attempt}/{attempts} retry_delay={AUTOSYMLINK_RETRY_DELAY_SECONDS}s "
                f"title={context.get('title')}"
            )
            time.sleep(max(AUTOSYMLINK_RETRY_DELAY_SECONDS, 0))

    logger.error(
        "[Auto_Symlink] 刷新最终失败: "
        f"title={context.get('title')} episode={context.get('episode')}/"
        f"{context.get('total_episode_number')}"
    )
    return False


def trigger_autosymlink_refresh_once(context, session=requests):
    """调用 Auto_Symlink 手动刷新接口；HTTP 2xx 就按成功处理。"""

    config_error = get_autosymlink_config_error()
    if config_error:
        logger.error(f"[Auto_Symlink] 配置不完整，无法刷新: {config_error}")
        return False

    url = build_autosymlink_sync_url()
    headers = build_autosymlink_headers()
    body = load_autosymlink_request_body()

    try:
        response = session.post(
            url,
            json=body,
            headers=headers,
            timeout=AUTOSYMLINK_REQUEST_TIMEOUT_SECONDS,
        )
    except requests.exceptions.RequestException as e:
        logger.warning(f"[Auto_Symlink] 请求异常: {e}")
        return False

    # 响应 body 只截短写日志，避免把远端完整响应 dump 到容器日志。
    response_text = (response.text or "").replace("\n", " ")[:500]
    if 200 <= response.status_code < 300:
        logger.info(
            "[Auto_Symlink] 刷新调用成功: "
            f"status={response.status_code} title={context.get('title')} "
            f"episode={context.get('episode')}/{context.get('total_episode_number')} "
            f"body={response_text}"
        )
        return True

    logger.warning(
        "[Auto_Symlink] 刷新调用失败: "
        f"status={response.status_code} title={context.get('title')} body={response_text}"
    )
    return False


def get_autosymlink_config_error():
    """集中检查必需配置，防止延迟线程里静默空跑。"""

    missing = []
    if not AUTOSYMLINK_BASE_URL:
        missing.append("AUTOSYMLINK_BASE_URL")
    if not AUTOSYMLINK_API_KEY:
        missing.append("AUTOSYMLINK_API_KEY")
    if not AUTOSYMLINK_TASK_UUID:
        missing.append("AUTOSYMLINK_TASK_UUID")
    return ", ".join(missing)


def build_autosymlink_sync_url():
    """拼出 Auto_Symlink 的手动同步任务接口地址。"""

    task_uuid = quote(AUTOSYMLINK_TASK_UUID, safe="")
    return f"{AUTOSYMLINK_BASE_URL.rstrip('/')}/common_tools/add_sync_task/{task_uuid}"


def build_autosymlink_headers():
    headers = {"Content-Type": "application/json"}
    if AUTOSYMLINK_API_KEY:
        # 不同部署可能读取不同 API Key header；同时发送两种常见形式，日志不会输出 key。
        headers["X-API-Key"] = AUTOSYMLINK_API_KEY
        headers["Authorization"] = f"Bearer {AUTOSYMLINK_API_KEY}"
    if AUTOSYMLINK_COOKIE:
        # Auto_Symlink 的 common_tools UI 接口需要网页登录 cookie；该值只从环境变量读取。
        headers["Cookie"] = AUTOSYMLINK_COOKIE
    return headers


def load_autosymlink_request_body():
    """读取可配置请求体；配置错误时保守回退为空对象。"""

    text = (AUTOSYMLINK_REQUEST_BODY_JSON or "{}").strip() or "{}"
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"[Auto_Symlink] AUTOSYMLINK_REQUEST_BODY_JSON 无效，改用空对象: {e}")
        return {}
    if not isinstance(data, dict):
        logger.warning("[Auto_Symlink] AUTOSYMLINK_REQUEST_BODY_JSON 必须是 JSON object，改用空对象")
        return {}
    return data
