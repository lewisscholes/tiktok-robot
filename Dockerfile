FROM python:3.11-slim
RUN apt-get update && apt-get install -y ffmpeg git && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV TRANSFORMERS_CACHE=/app/.cache
COPY main.py .
# Render sets $PORT at runtime. Use it.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]

