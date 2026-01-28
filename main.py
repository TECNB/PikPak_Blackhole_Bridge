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
from datetime import datetime

# ================= Configuration =================

# 1. Alist 认证配置
ALIST_HOST = os.getenv("ALIST_HOST")
ALIST_USERNAME = os.getenv("ALIST_USERNAME")
ALIST_PASSWORD = os.getenv("ALIST_PASSWORD")

# 2. 归档根目录
PROCESSED_DIR = os.getenv("PROCESSED_DIR")

# 3. 监控配置 (路径映射: 本地监控路径 -> 云端基础路径)
# 注意：这里从环境变量读取，如果未设置则使用默认值
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
                logger.error(f"[身份验证] ❌ 登录失败: {data.get('message')}")
        else:
            logger.error(f"[身份验证] HTTP 错误: {response.status_code}")
    except Exception as e:
        logger.error(f"[身份验证] 连接异常: {e}")
    
    return False

def get_auth_header():
    """获取带 Token 的 Header，如果无 Token 则尝试登录"""
    if not CURRENT_TOKEN:
        login_and_update_token()
    return {"Authorization": CURRENT_TOKEN, "Content-Type": "application/json"}

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
    Args:
        filename: 文件名
        cloud_base_path: 对应的云端基础路径 (如 /pikpak/Media/TV)
        category_tag: 日志标签
    """
    base_name = os.path.splitext(filename)[0]
    
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
    调用 Alist API 查询路径是否存在
    """
    api_url = f"{ALIST_HOST}/api/fs/get"
    headers = get_auth_header()
    payload = {"path": path}
    
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        
        if response.status_code == 401:
            logger.warning("[API Check] Token 过期，重试...")
            if login_and_update_token():
                headers = get_auth_header()
                response = requests.post(api_url, json=payload, headers=headers)

        if response.status_code == 200:
            data = response.json()
            if data.get('code') == 200:
                return True
            else:
                return False
    except Exception as e:
        logger.error(f"[API Check] 请求异常: {e}")
    return False

def alist_fs_list(path, refresh=True):
    """
    强制刷新 Alist 缓存
    """
    api_url = f"{ALIST_HOST}/api/fs/list"
    headers = get_auth_header()
    payload = {
        "path": path,
        "password": "",
        "page": 1,
        "per_page": 1,
        "refresh": refresh 
    }
    try:
        requests.post(api_url, json=payload, headers=headers)
    except Exception as e:
        logger.warning(f"[API List] 刷新请求失败 {path}: {e}")

def ensure_path_ready(full_path, category_tag, max_wait_seconds=30):
    """
    逐级创建目录
    """
    logger.info(f"{category_tag} ------ 开始检查云端路径: {full_path} ------")
    
    parts = [p for p in full_path.split('/') if p]
    current_path = ""
    
    for i, part in enumerate(parts):
        parent_path = current_path if current_path else "/"
        current_path = f"{current_path}/{part}"
        
        # 优化：不频繁刷新根目录，只刷新变动的子目录
        # alist_fs_list(parent_path, refresh=True) 
        
        if check_alist_path_exists(current_path):
            continue 
            
        logger.info(f"{category_tag} [Step {i+1}] 目录不存在，正在创建: {current_path}")
        mkdir_url = f"{ALIST_HOST}/api/fs/mkdir"
        headers = get_auth_header()
        
        try:
            resp = requests.post(mkdir_url, json={"path": current_path}, headers=headers)
            if resp.status_code == 401:
                login_and_update_token()
                headers = get_auth_header()
                requests.post(mkdir_url, json={"path": current_path}, headers=headers)
        except Exception as e:
            logger.error(f"{category_tag} [Mkdir] 创建失败: {e}")

        alist_fs_list(parent_path, refresh=True)
        
        layer_start_time = time.time()
        layer_ready = False
        
        while time.time() - layer_start_time < max_wait_seconds:
            if check_alist_path_exists(current_path):
                layer_ready = True
                logger.info(f"{category_tag} [Step {i+1}] >> 确认目录就绪: {current_path}")
                break
            time.sleep(1)
            
        if not layer_ready:
            logger.error(f"{category_tag} [Timeout] 致命错误: 目录创建后无法在云端确认: {current_path}")
            return False

    logger.info(f"{category_tag} ------ 云端路径校验全部通过 ------")
    return True

def add_offline_download(url, save_path, category_tag):
    """发送离线下载任务"""
    if not ensure_path_ready(save_path, category_tag):
        logger.error(f"{category_tag} [任务取消] 目录环境未就绪")
        return False

    api_url = f"{ALIST_HOST}/api/fs/add_offline_download"
    headers = get_auth_header()
    
    payload = {
        "path": save_path, 
        "urls": [url],
        "tool": "PikPak", 
        "delete_policy": "delete_on_upload_succeed"
    }

    logger.info(f"{category_tag} [离线下载] 正在提交任务...")
    try:
        response = requests.post(api_url, json=payload, headers=headers)
        
        if response.status_code == 401:
            logger.warning(f"{category_tag} [离线下载] Token 过期，重新登录并重试...")
            if login_and_update_token():
                headers = get_auth_header()
                response = requests.post(api_url, json=payload, headers=headers)
            else:
                return False

        if response.status_code == 200:
            resp_json = response.json()
            if resp_json.get('code') == 200:
                logger.info(f"{category_tag} [离线下载] ✅ 任务添加成功! 目标: {save_path}")
                return True
            else:
                logger.error(f"{category_tag} [离线下载] ❌ Alist 返回错误: {resp_json}")
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
        # 避免处理归档目录（虽然现在归档目录通常在外部，但为了安全保留检查）
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
            success = add_offline_download(magnet, target_path, category_tag)
        else:
            logger.warning(f"{category_tag} 无法提取磁力链接，跳过文件: {filename}")
        
        if success:
            try:
                # 归档逻辑
                # 计算相对路径，以便在归档目录中保持结构 (如果目标路径比基路径长)
                relative_path = ""
                if target_path.startswith(cloud_base_path):
                    # 例如: target=/pikpak/Media/TV/Show/S01, base=/pikpak/Media/TV
                    # relative = /Show/S01
                    relative_path = target_path[len(cloud_base_path):].strip("/")
                
                # 组合本地归档路径
                # 将文件按分类 (TV/Movie) 放入归档目录的子文件夹中，避免混淆
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
    logger.info(">>> 自动分类脚本启动 (双目录监控版) <<<")
    logger.info(f"归档总目录: {PROCESSED_DIR}")
    logger.info(f"Alist Host: {ALIST_HOST}")
    
    # 打印监控配置
    for cat, conf in WATCH_CONFIG.items():
        logger.info(f"配置 [{cat}]: 监控 {conf['local']} -> 上传至 {conf['cloud']}")
    
    if not login_and_update_token():
        logger.error(">>> 启动时登录失败，将在任务中重试 <<<")

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