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
from datetime import datetime

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

# =================================================

# 全局变量存储 Token
CURRENT_TOKEN = ""

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

def check_alist_path_exists(path):
    """
    调用 Alist API 查询路径是否存在 (使用通用请求函数)
    """
    api_url = f"{ALIST_HOST}/api/fs/get"
    payload = {"path": path}
    
    try:
        # 使用封装的 alist_post_request 替代 requests.post
        response = alist_post_request(api_url, payload)
        
        if response.status_code == 200:
            data = response.json()
            code = data.get('code')
            if code == 200:
                return True
            else:
                return False
        else:
            logger.error(f"[API Check] HTTP异常 {response.status_code} | Path: {path} | Resp: {response.text}")
            return False
    except Exception as e:
        logger.error(f"[API Check] 请求异常: {e}")
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

def ensure_path_ready(full_path, skip_prefix_path, category_tag, max_wait_seconds=30):
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

        # 2. 检查是否存在
        exists = check_alist_path_exists(current_path)
        
        if exists:
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

        # 4. 刷新父目录并等待确认
        alist_fs_list(parent_path, refresh=True)
        
        layer_start_time = time.time()
        layer_ready = False
        
        while time.time() - layer_start_time < max_wait_seconds:
            if check_alist_path_exists(current_path):
                layer_ready = True
                logger.info(f"{category_tag} [Step {i+1}] >> 确认目录就绪: {current_path}")
                break
            time.sleep(2)
            
        if not layer_ready:
            logger.error(f"{category_tag} [Timeout] 致命错误: 目录创建后无法在云端确认: {current_path}")
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
                logger.info(f"{category_tag} [离线下载] ✅ 任务添加成功! 目标: {save_path}")
                return True
            else:
                logger.error(f"{category_tag} [离线下载] ❌ Alist 返回错误: {resp_json.get('message')}")
        else:
            logger.error(f"{category_tag} [离线下载] ❌ HTTP 错误: {response.status_code}")
    except Exception as e:
        logger.error(f"{category_tag} [离线下载] 连接异常: {e}")
    return False

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
        
        if filename.endswith(".torrent"):
            magnet = get_magnet_from_torrent(file_path, category_tag)
        elif filename.endswith(".magnet") or filename.endswith(".txt"):
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if "magnet:?" in content:
                        magnet = content[content.find("magnet:?"):]
                        logger.info(f"{category_tag} [读取文本] 成功提取磁力链接")
            except Exception as e:
                logger.error(f"{category_tag} [读取文本] 读取失败: {e}")
            
        if magnet:
            target_path = get_save_path(filename, cloud_base_path, category_tag)
            # 修正：传递 cloud_base_path 给 add_offline_download
            success = add_offline_download(magnet, target_path, cloud_base_path, category_tag)
        else:
            logger.warning(f"{category_tag} 无法提取磁力链接，跳过文件: {filename}")
        
        if success:
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
        except KeyboardInterrupt:
            logger.info("用户停止脚本")
            break
        except Exception as e:
            logger.error(f"主循环发生未捕获异常: {e}")
            logger.error(traceback.format_exc())
        
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
