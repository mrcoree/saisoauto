# Python 3.11 슬림 버전 사용
FROM python:3.11-slim

# 필요한 리눅스 패키지 설치 (Chrome 구동용)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    unzip \
    curl \
    libglib2.0-0 \
    libnss3 \
    libfontconfig1 \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome 안정화 버전 설치
RUN wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub > /usr/share/keyrings/google-chrome.asc \
    && sh -c 'echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.asc] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list' \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# 작업 디렉토리 설정
WORKDIR /app

# 파이썬 의존성 패키지 복사 및 설치
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 프로젝트 전체 코드 복사
COPY . .

# Gunicorn으로 8080 포트에서 무중단 백그라운드 웹서버 형태로 실행
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "app:app"]
