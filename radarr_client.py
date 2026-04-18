import re
import time
import traceback

import requests

from config import (
    RADARR_API_KEY,
    RADARR_HOST,
    RADARR_REFRESH_CONFIRM_ENABLED,
    RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS,
    RADARR_REFRESH_CONFIRM_POLL_INTERVAL,
    logger,
)


RADARR_REFRESH_LOG_PREFIX = "[Movie] [RadarrRefresh]"
RADARR_REQUEST_TIMEOUT = 15
RADARR_REFRESH_MAX_ATTEMPTS = 3
RADARR_REFRESH_RETRY_BACKOFF = 2
RADARR_REFRESH_CONFIRM_FAILED_STATES = {"failed", "aborted", "cancelled"}


def build_radarr_headers():
    return {
        "X-Api-Key": RADARR_API_KEY,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def normalize_movie_title(title):
    text = str(title or "").strip().lower()
    if not text:
        return ""
    text = re.sub(r"[^a-z0-9]+", "", text)
    return text


def get_radarr_movies():
    api_url = f"{RADARR_HOST}/api/v3/movie"
    logger.info(f"{RADARR_REFRESH_LOG_PREFIX} 查询影片列表以映射 movieId: GET {api_url}")

    response = requests.get(
        api_url,
        headers=build_radarr_headers(),
        timeout=RADARR_REQUEST_TIMEOUT,
    )
    logger.info(
        f"{RADARR_REFRESH_LOG_PREFIX} 影片列表响应: HTTP {response.status_code} | Body: {response.text}"
    )
    response.raise_for_status()
    return response.json()


def get_radarr_command(command_id):
    api_url = f"{RADARR_HOST}/api/v3/command/{command_id}"
    logger.info(f"{RADARR_REFRESH_LOG_PREFIX} 查询命令状态以确认已触发: GET {api_url}")

    response = requests.get(
        api_url,
        headers=build_radarr_headers(),
        timeout=RADARR_REQUEST_TIMEOUT,
    )
    logger.info(
        f"{RADARR_REFRESH_LOG_PREFIX} 命令状态响应: HTTP {response.status_code} | Body: {response.text}"
    )

    if response.status_code == 404:
        return None

    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"Radarr command 返回格式异常: {type(data).__name__}")
    return data


def normalize_command_state(command):
    value = (command or {}).get("state") or (command or {}).get("status")
    return str(value or "").strip().lower()


def extract_command_movie_ids(command):
    movie_ids = set()
    if not isinstance(command, dict):
        return movie_ids

    candidates = [command, command.get("body")]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue

        movie_id = candidate.get("movieId")
        if movie_id not in (None, ""):
            try:
                movie_ids.add(int(movie_id))
            except (TypeError, ValueError):
                logger.warning(
                    f"{RADARR_REFRESH_LOG_PREFIX} 命令中的 movieId 无法转为整数，已忽略: {movie_id!r}"
                )

        movie_ids_value = candidate.get("movieIds")
        if isinstance(movie_ids_value, list):
            for item in movie_ids_value:
                if item in (None, ""):
                    continue
                try:
                    movie_ids.add(int(item))
                except (TypeError, ValueError):
                    logger.warning(
                        f"{RADARR_REFRESH_LOG_PREFIX} 命令中的 movieIds 项无法转为整数，已忽略: {item!r}"
                    )

    return movie_ids


def confirm_radarr_refresh_command(command_id, movie_id, movie_title, movie_year, movie_path):
    if not RADARR_REFRESH_CONFIRM_ENABLED:
        logger.info(
            f"{RADARR_REFRESH_LOG_PREFIX} 已关闭命令确认，直接视为触发成功: "
            f"movieId={movie_id} commandId={command_id}"
        )
        return True

    for attempt in range(1, RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS + 1):
        try:
            command = get_radarr_command(command_id)
            if command is None:
                logger.warning(
                    f"{RADARR_REFRESH_LOG_PREFIX} 命令确认未命中: "
                    f"movieId={movie_id} commandId={command_id} "
                    f"attempt={attempt}/{RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS}"
                )
            else:
                command_name = str(command.get("name") or "").strip()
                command_state = normalize_command_state(command)
                command_movie_ids = extract_command_movie_ids(command)

                if command_name != "RefreshMovie":
                    logger.warning(
                        f"{RADARR_REFRESH_LOG_PREFIX} 命令确认命中到非 RefreshMovie 命令，继续等待: "
                        f"movieId={movie_id} commandId={command_id} name={command_name!r} "
                        f"attempt={attempt}/{RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS}"
                    )
                elif command_movie_ids and movie_id not in command_movie_ids:
                    logger.warning(
                        f"{RADARR_REFRESH_LOG_PREFIX} 命令确认命中到其它 movieId，继续等待: "
                        f"expectedMovieId={movie_id} actualMovieIds={sorted(command_movie_ids)} "
                        f"commandId={command_id} attempt={attempt}/{RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS}"
                    )
                elif command_state in RADARR_REFRESH_CONFIRM_FAILED_STATES:
                    logger.error(
                        f"{RADARR_REFRESH_LOG_PREFIX} 命令已入队但状态异常，判定本次触发失败: "
                        f"title={movie_title} year={movie_year} movieId={movie_id} path={movie_path} "
                        f"commandId={command_id} state={command_state!r}"
                    )
                    return False
                elif command_state:
                    logger.info(
                        f"{RADARR_REFRESH_LOG_PREFIX} 已确认命令确实触发: "
                        f"title={movie_title} year={movie_year} movieId={movie_id} path={movie_path} "
                        f"commandId={command_id} state={command_state!r}"
                    )
                    return True
                else:
                    logger.warning(
                        f"{RADARR_REFRESH_LOG_PREFIX} 命令已查询到但状态为空，继续等待: "
                        f"movieId={movie_id} commandId={command_id} "
                        f"attempt={attempt}/{RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS}"
                    )
        except Exception as e:
            logger.error(
                f"{RADARR_REFRESH_LOG_PREFIX} 命令确认异常: "
                f"title={movie_title} year={movie_year} movieId={movie_id} path={movie_path} "
                f"commandId={command_id} attempt={attempt}/{RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS} | {e}"
            )
            logger.error(traceback.format_exc())

        if attempt < RADARR_REFRESH_CONFIRM_MAX_ATTEMPTS:
            time.sleep(RADARR_REFRESH_CONFIRM_POLL_INTERVAL)

    logger.error(
        f"{RADARR_REFRESH_LOG_PREFIX} 未能确认命令已真正触发，将按失败处理并交给上层重试: "
        f"title={movie_title} year={movie_year} movieId={movie_id} path={movie_path} "
        f"commandId={command_id}"
    )
    return False


def resolve_radarr_movie_id(movie):
    movie = movie or {}

    direct_movie_id = movie.get("id")
    if direct_movie_id not in (None, ""):
        logger.info(f"{RADARR_REFRESH_LOG_PREFIX} 使用 webhook 携带的 movie.id: {direct_movie_id}")
        return int(direct_movie_id)

    tmdb_id = movie.get("tmdbId")
    imdb_id = str(movie.get("imdbId") or "").strip().lower()
    title = movie.get("title")
    year = str(movie.get("year") or "").strip()

    movies = get_radarr_movies()
    if not isinstance(movies, list):
        raise ValueError(f"Radarr movie 列表返回格式异常: {type(movies).__name__}")

    if tmdb_id not in (None, ""):
        for item in movies:
            if str(item.get("tmdbId")) == str(tmdb_id):
                resolved_id = item.get("id")
                logger.info(f"{RADARR_REFRESH_LOG_PREFIX} 通过 tmdbId 映射到 movie.id: {resolved_id}")
                return int(resolved_id)

    if imdb_id:
        for item in movies:
            if str(item.get("imdbId") or "").strip().lower() == imdb_id:
                resolved_id = item.get("id")
                logger.info(f"{RADARR_REFRESH_LOG_PREFIX} 通过 imdbId 映射到 movie.id: {resolved_id}")
                return int(resolved_id)

    if title and year:
        normalized_title = normalize_movie_title(title)
        matched = []
        for item in movies:
            candidate_title = normalize_movie_title(item.get("title"))
            candidate_year = str(item.get("year") or "").strip()
            if candidate_title == normalized_title and candidate_year == year:
                matched.append(item)

        if len(matched) == 1:
            resolved_id = matched[0].get("id")
            logger.info(
                f"{RADARR_REFRESH_LOG_PREFIX} 通过 title+year 映射到 movie.id: {resolved_id} | "
                f"{title} ({year})"
            )
            return int(resolved_id)

        if len(matched) > 1:
            raise ValueError(f"title+year 命中多个 Radarr 影片，无法安全映射: {title} ({year})")

    raise ValueError(
        f"无法解析当前影片的 Radarr movie.id: title={title!r} year={year!r} "
        f"tmdbId={tmdb_id!r} imdbId={movie.get('imdbId')!r}"
    )


def trigger_radarr_movie_refresh(movie, movie_path):
    movie = movie or {}

    if not RADARR_HOST or not RADARR_API_KEY:
        logger.warning(
            f"{RADARR_REFRESH_LOG_PREFIX} 未配置 RADARR_HOST/RADARR_API_KEY，跳过定向刷新: {movie_path}"
        )
        return False

    try:
        movie_id = resolve_radarr_movie_id(movie)
    except Exception as e:
        logger.error(
            f"{RADARR_REFRESH_LOG_PREFIX} 解析 movie.id 失败: "
            f"title={movie.get('title')} year={movie.get('year')} path={movie_path} | {e}"
        )
        logger.error(traceback.format_exc())
        return False

    api_url = f"{RADARR_HOST}/api/v3/command"
    payload = {"name": "RefreshMovie", "movieIds": [movie_id]}
    movie_title = movie.get("title")
    movie_year = movie.get("year")

    for attempt in range(1, RADARR_REFRESH_MAX_ATTEMPTS + 1):
        try:
            logger.info(
                f"{RADARR_REFRESH_LOG_PREFIX} 准备触发 Radarr refresh/rescan: "
                f"title={movie_title} year={movie_year} movieId={movie_id} path={movie_path} | "
                f"POST {api_url} | attempt={attempt}/{RADARR_REFRESH_MAX_ATTEMPTS}"
            )
            response = requests.post(
                api_url,
                headers=build_radarr_headers(),
                json=payload,
                timeout=RADARR_REQUEST_TIMEOUT,
            )
            logger.info(
                f"{RADARR_REFRESH_LOG_PREFIX} 接口响应: HTTP {response.status_code} | Body: {response.text}"
            )

            if response.status_code in (200, 201, 202):
                data = response.json()
                if not isinstance(data, dict):
                    logger.error(
                        f"{RADARR_REFRESH_LOG_PREFIX} 定向刷新响应格式异常: "
                        f"movieId={movie_id} | type={type(data).__name__}"
                    )
                else:
                    command_id = data.get("id")
                    logger.info(
                        f"{RADARR_REFRESH_LOG_PREFIX} 定向刷新请求已提交，开始确认是否真正触发: "
                        f"movieId={movie_id} commandId={command_id} name={data.get('name')}"
                    )

                    if command_id in (None, ""):
                        logger.error(
                            f"{RADARR_REFRESH_LOG_PREFIX} 定向刷新响应缺少 commandId，无法确认是否真正触发: "
                            f"movieId={movie_id} | Body: {response.text}"
                        )
                    elif confirm_radarr_refresh_command(
                        int(command_id),
                        movie_id,
                        movie_title,
                        movie_year,
                        movie_path,
                    ):
                        return True

                    logger.error(
                        f"{RADARR_REFRESH_LOG_PREFIX} 定向刷新请求已提交，但未能确认命令真正触发: "
                        f"movieId={movie_id} commandId={command_id} | Body: {response.text}"
                    )

                continue

            logger.error(
                f"{RADARR_REFRESH_LOG_PREFIX} 定向刷新失败: HTTP {response.status_code} | "
                f"movieId={movie_id} | Body: {response.text}"
            )
        except Exception as e:
            logger.error(
                f"{RADARR_REFRESH_LOG_PREFIX} 调用异常: "
                f"movieId={movie_id} title={movie_title} year={movie_year} path={movie_path} | {e}"
            )
            logger.error(traceback.format_exc())

        if attempt < RADARR_REFRESH_MAX_ATTEMPTS:
            time.sleep(RADARR_REFRESH_RETRY_BACKOFF * attempt)

    logger.error(
        f"{RADARR_REFRESH_LOG_PREFIX} 多次重试后仍失败，跳过回滚并继续主流程: "
        f"title={movie_title} year={movie_year} movieId={movie_id} path={movie_path}"
    )
    return False
