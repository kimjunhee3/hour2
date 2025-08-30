FROM python:3.11-slim

# 1) 기본 패키지
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget gnupg ca-certificates \
    fonts-noto fonts-noto-cjk fonts-noto-color-emoji \
    tini \
    && rm -rf /var/lib/apt/lists/*

# 2) Google Chrome 설치 (공식 저장소 추가)
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-linux.gpg && \
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-linux.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list && \
    apt-get update && apt-get install -y --no-install-recommends google-chrome-stable && \
    rm -rf /var/lib/apt/lists/*

# Chrome 바이너리 경로 환경변수
ENV CHROME_BIN=/usr/bin/google-chrome

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .


# 캐시 디렉토리 (Railway 볼륨을 /data로 마운트)
RUN mkdir -p /data
ENV CACHE_DIR=/data
ENV PYTHONUNBUFFERED=1

ENTRYPOINT ["/usr/bin/tini", "--"]

# Railway가 주는 $PORT로 바인딩
CMD ["sh", "-c", "gunicorn -w 2 -k gthread -t 180 -b 0.0.0.0:${PORT} wsgi:application"]
