# 使用 3.10-slim
FROM python:3.10-slim-bookworm

# 设置环境变量
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Shanghai

# 设置工作目录
WORKDIR /app

# 优化系统依赖安装：替换清华源并清理缓存
RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources || \
    sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list && \
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

# 先复制 requirements.txt 以利用镜像层缓存
COPY requirements.txt .

# 使用清华源加速安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt \
    -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

# 复制项目代码
COPY . .

# 创建上传目录并设置权限
RUN mkdir -p /data/uploads && chmod 777 /data/uploads

# 暴露端口
EXPOSE 5000

# 启动命令
CMD ["python", "run_all.py"]
