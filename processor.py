import hashlib
import os
import re
import shutil
import traceback

import bencodepy

from alist_client import add_offline_download
from config import PROCESSED_DIR, logger
from movie_flatten import get_movie_save_path_from_task, register_movie_flatten_task
from path_utils import get_save_path
from webhook import find_pending_movie_task, remove_pending_movie_task


def get_magnet_from_torrent(torrent_path, category_tag):
    """读取 .torrent 并计算磁力"""
    try:
        metadata = bencodepy.decode_from_file(torrent_path)
        subj = metadata[b"info"]
        hashcontents = bencodepy.encode(subj)
        digest = hashlib.sha1(hashcontents).digest()
        b32hash = digest.hex()
        magnet = f"magnet:?xt=urn:btih:{b32hash}"
        logger.info(f"{category_tag} [解析种子] 成功: {os.path.basename(torrent_path)}")
        return magnet
    except Exception as e:
        logger.error(f"{category_tag} [解析种子] 失败 {torrent_path}: {e}")
        return None


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

    files = sorted([f for f in os.listdir(watch_dir) if not f.startswith(".")])

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
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    magnet_match = re.search(r"magnet:\?[^\s]+", content)
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
                    success,
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
