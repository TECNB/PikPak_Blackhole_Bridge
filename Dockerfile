FROM python:3.9-slim

WORKDIR /app

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制脚本
COPY main.py .
COPY config.py .
COPY state.py .
COPY alist_client.py .
COPY clouddrive_client.py .
COPY clouddrive_pb2.py .
COPY clouddrive_pb2_grpc.py .
COPY path_utils.py .
COPY webhook.py .
COPY movie_flatten.py .
COPY processor.py .

CMD ["python", "main.py"]
