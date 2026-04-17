from urllib.parse import urlparse

import grpc

import clouddrive_pb2 as clouddrive_pb2
import clouddrive_pb2_grpc as clouddrive_pb2_grpc
from config import CD2_HOST, CD2_TOKEN, logger


def _get_cd2_channel_target():
    """把 CD2_HOST 解析成 grpc channel 可用的 host:port。"""
    if not CD2_HOST:
        return None, False

    raw_host = CD2_HOST if "://" in CD2_HOST else f"http://{CD2_HOST}"
    parsed = urlparse(raw_host)
    if not parsed.hostname:
        return None, False

    default_port = 443 if parsed.scheme == "https" else 80
    port = parsed.port or default_port
    return f"{parsed.hostname}:{port}", parsed.scheme == "https"


def _build_cd2_channel():
    """按 CD2_HOST 构建 gRPC channel。"""
    target, use_tls = _get_cd2_channel_target()
    if not target:
        raise ValueError("CD2_HOST 无法解析为有效的 host:port")

    if use_tls:
        return grpc.secure_channel(target, grpc.ssl_channel_credentials())
    return grpc.insecure_channel(target)


def build_cd2_authorized_metadata():
    """构造 CD2 Bearer Token metadata。"""
    if not CD2_TOKEN:
        return []
    return [("authorization", f"Bearer {CD2_TOKEN}")]


def clouddrive_find_file_by_path(parent_path, path, metadata):
    """调用 CloudDrive gRPC 的 FindFileByPath 查询目标路径。"""
    channel = None
    try:
        channel = _build_cd2_channel()
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        request = clouddrive_pb2.FindFileByPathRequest(parentPath=parent_path, path=path)
        return stub.FindFileByPath(request, metadata=metadata)
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            return None
        logger.warning(f"[CD2 gRPC] FindFileByPath 失败: path={path} | {e.code().name}: {e.details()}")
        return None
    except Exception as e:
        logger.warning(f"[CD2 gRPC] FindFileByPath 异常: path={path} | {e}")
        return None
    finally:
        if channel is not None:
            channel.close()


def clouddrive_list_sub_files(path, metadata, force_refresh=True):
    """调用 CloudDrive gRPC 的 GetSubFiles 列目录，并可强制 refresh。"""
    channel = None
    try:
        channel = _build_cd2_channel()
        stub = clouddrive_pb2_grpc.CloudDriveFileSrvStub(channel)
        request = clouddrive_pb2.ListSubFileRequest(path=path, forceRefresh=force_refresh)
        items = []
        for reply in stub.GetSubFiles(request, metadata=metadata):
            items.extend(reply.subFiles)
        return items
    except grpc.RpcError as e:
        if e.code() == grpc.StatusCode.NOT_FOUND:
            return None
        logger.warning(f"[CD2 gRPC] GetSubFiles 失败: path={path} | {e.code().name}: {e.details()}")
        return None
    except Exception as e:
        logger.warning(f"[CD2 gRPC] GetSubFiles 异常: path={path} | {e}")
        return None
    finally:
        if channel is not None:
            channel.close()
