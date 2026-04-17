import time
import traceback

from alist_client import login_and_update_token
from config import ALIST_HOST, CHECK_INTERVAL, PROCESSED_DIR, WATCH_CONFIG, logger
from movie_flatten import load_movie_flatten_tasks, process_movie_flatten_tasks
from processor import process_single_dir
from webhook import load_pending_tasks, start_webhook_server


def main():
    logger.info(">>> 自动分类脚本启动 (Token 自动刷新版) <<<")
    logger.info(f"归档总目录: {PROCESSED_DIR}")
    logger.info(f"Alist Host: {ALIST_HOST}")
    load_pending_tasks()
    load_movie_flatten_tasks()
    webhook_server = start_webhook_server()

    for cat, conf in WATCH_CONFIG.items():
        logger.info(f"配置 [{cat}]: 监控 {conf['local']} -> 上传至 {conf['cloud']}")

    if not login_and_update_token():
        logger.error(">>> 启动时登录失败，将在任务中自动重试 <<<")

    while True:
        try:
            for category, config in WATCH_CONFIG.items():
                process_single_dir(
                    watch_dir=config["local"],
                    cloud_base_path=config["cloud"],
                    category_name=category,
                )
            process_movie_flatten_tasks()
        except KeyboardInterrupt:
            logger.info("用户停止脚本")
            if webhook_server:
                webhook_server.shutdown()
            break
        except Exception as e:
            logger.error(f"主循环发生未捕获异常: {e}")
            logger.error(traceback.format_exc())

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
