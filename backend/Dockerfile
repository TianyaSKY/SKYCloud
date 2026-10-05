# Python 3.11 matches the tested dependency locks
FROM python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /app

# Freeze Debian package resolution at the same signed snapshot as the runtime.
RUN rm -f /etc/apt/sources.list.d/debian.sources && \
    printf 'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261001T000000Z/ bookworm main\n' > /etc/apt/sources.list && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
    libreoffice \
    fonts-wqy-zenhei \
    libxinerama1 \
    libdbus-1-3 \
    libx11-6 \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender1 \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Production includes only hashed runtime dependencies; test tools are separate.
COPY requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock \
    --index-url https://pypi.org/simple

# 复制项目代码
COPY . .

# 创建上传目录并设置权限
RUN mkdir -p /data/uploads && chmod 777 /data/uploads

# 暴露端口
EXPOSE 5000

# 启动命令
CMD ["python", "-m", "entry.api"]
