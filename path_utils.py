import os
import re
import unicodedata
from urllib.parse import unquote

from config import VIDEO_EXTENSIONS, logger


def normalize_release_title(value):
    """
    将 Radarr releaseTitle 和黑洞文件名归一成同一个可比对 key。
    只做格式标准化，不尝试从名称里推断影片信息。
    """
    if not value:
        return ""

    text = unquote(str(value))
    text = unicodedata.normalize("NFKC", text).lower()
    text = re.sub(r"[\W_]+", " ", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_file_release_key(filename):
    """取黑洞文件名主干并生成 releaseTitle 匹配 key。"""
    base_name = os.path.splitext(os.path.basename(filename))[0]
    return normalize_release_title(base_name)


def sanitize_path_component(value):
    """清理路径分段，避免电影标题中的路径分隔符产生意外子目录。"""
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[\\/]+", " - ", text)
    text = re.sub(r"[\x00-\x1f\x7f]+", "", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return text


def get_save_path(filename, cloud_base_path, category_tag):
    """
    解析文件名并生成保存路径
    """
    base_name = os.path.splitext(filename)[0]
    category_name = category_tag.strip("[]").lower()

    # 电影专属规则: {Movie Title} ({Release Year})
    if category_name == "movie":
        normalized_name = base_name.replace(".", " ").replace("_", " ")
        normalized_name = re.sub(r"\s+", " ", normalized_name).strip()
        # 模式1: Title (Year) / Title [Year]
        movie_match = re.match(r"^(.*?)\s*[\(\[]((?:19|20)\d{2})[\)\]](?:\s|$)", normalized_name)
        # 模式2: Title Year 其他发布信息
        if not movie_match:
            movie_match = re.match(r"^(.*?)\s((?:19|20)\d{2})(?:\s|$)", normalized_name)

        if movie_match:
            raw_title = movie_match.group(1).strip(" -._")
            release_year = movie_match.group(2)
            clean_title = re.sub(r"\[.*?\]|【.*?】", "", raw_title).strip()
            clean_title = re.sub(r"\s+", " ", clean_title).strip()

            # 多语标题干扰处理:
            # 例如 "Выживший + The Revenant" 优先保留英文标题段
            if clean_title:
                title_parts = [p.strip(" -._") for p in re.split(r"\s*[+/|]+\s*", clean_title) if p.strip(" -._")]
                if title_parts:
                    ascii_parts = [p for p in title_parts if re.search(r"[A-Za-z]", p)]
                    if ascii_parts:
                        clean_title = max(ascii_parts, key=lambda p: len(re.findall(r"[A-Za-z]", p)))
                    else:
                        clean_title = title_parts[-1]
                    clean_title = re.sub(r"\s+", " ", clean_title).strip()

            if clean_title:
                movie_folder = f"{clean_title} ({release_year})"
                base = cloud_base_path.rstrip("/")
                final_path = f"{base}/{movie_folder}"
                logger.info(f"{category_tag} [路径解析] 电影提取: [{movie_folder}]")
                return final_path

        logger.warning(f"{category_tag} [路径解析] 未匹配到电影格式，使用基础路径: {cloud_base_path}")
        return cloud_base_path

    # 1. 去除所有括号内容
    base_name = re.sub(r"\[.*?\]", "", base_name)
    base_name = re.sub(r"【.*?】", "", base_name)
    base_name = re.sub(r"\(.*?\)", "", base_name)
    base_name = re.sub(r"（.*?）", "", base_name)

    # 2. 核心匹配 Sxx
    match = re.search(r"^(.*?)[\._\s]+S(\d+)", base_name, re.IGNORECASE)

    if match:
        raw_name = match.group(1)
        season_num = match.group(2)

        # 3. 强制去除中文 (非ASCII字符)
        clean_name = re.sub(r"[^\x00-\x7F]+", "", raw_name)

        # 4. 格式化
        clean_name = clean_name.replace(".", " ").replace("_", " ").strip()

        # 5. 合并多余空格
        clean_name = re.sub(r"\s+", " ", clean_name).strip()

        try:
            season_folder = f"Season {int(season_num):02d}"
        except Exception:
            season_folder = f"Season {season_num}"

        if clean_name:
            base = cloud_base_path.rstrip("/")
            final_path = f"{base}/{clean_name}/{season_folder}"
            logger.info(f"{category_tag} [路径解析] 提取: [{clean_name}] | 季度: [{season_folder}]")
            return final_path

    logger.warning(f"{category_tag} [路径解析] 未匹配到剧集格式，使用基础路径: {cloud_base_path}")
    return cloud_base_path


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
        snapshot.append(
            {
                "name": name,
                "is_dir": is_dir_entry(entry),
                "size": entry.get("size"),
                "modified": entry.get("modified"),
            }
        )
    return sorted(snapshot, key=lambda item: (not item["is_dir"], item["name"]))
