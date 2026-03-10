FROM mcr.microsoft.com/playwright:v1.52.0-jammy

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    HOME=/runtime-state \
    CHROME_BIN=/ms-playwright/chromium-1169/chrome-linux/chrome

WORKDIR /runtime-service

RUN apt-get -o Acquire::Retries=3 update && apt-get -o Acquire::Retries=3 install -y --fix-missing --no-install-recommends \
    python3-pip \
    python3-venv \
    git \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

RUN npm install -g openclaw@2026.3.7

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "app.py"]
