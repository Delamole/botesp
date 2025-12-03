# Dockerfile
FROM python:3.11-slim

WORKDIR /app

# Устанавливаем только необходимое: ffmpeg + espeak-ng
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    ffmpeg \
    espeak-ng \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "10000"]