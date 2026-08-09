FROM python:3.12-slim

# 换国内 apt 源
RUN sed -i 's|deb.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources && \
    sed -i 's|security.debian.org|mirrors.aliyun.com|g' /etc/apt/sources.list.d/debian.sources

# 系统依赖：pyexiv2 需要 exiv2 库
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libexiv2-dev \
    libboost-python-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 换国内 pip 源
RUN pip config set global.index-url https://mirrors.aliyun.com/pypi/simple/ \
    && pip config set global.trusted-host mirrors.aliyun.com

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# 拷贝代码
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

# 默认启动命令（compose 里会覆盖）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
