# 使用 3.10
FROM python:3.10-bookworm

# 设置工作目录
WORKDIR /app

RUN sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list.d/debian.sources || \
    sed -i 's/deb.debian.org/mirrors.tuna.tsinghua.edu.cn/g' /etc/apt/sources.list

RUN apt-get update --fix-missing && \
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
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# 使用清华源加速
RUN pip install -r requirements.txt \
    -i https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple

COPY . .

# 暴露 Flask 端口
EXPOSE 5000

# 启动命令
CMD ["python", "run_all.py"]