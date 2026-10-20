FROM python:3.12-slim

WORKDIR /app

# 安装依赖
COPY requirements-minimal.txt requirements-minimal.lock.txt ./
RUN pip install --no-cache-dir -r requirements-minimal.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 复制代码
COPY . .

# Never bake a developer or deployment override into the image.
COPY config.example.yml ./config.yml

# 暴露端口（与 config.yml / Java 侧 grpc 地址一致）
EXPOSE 10003

# 启动服务
CMD ["python", "server.py"]
