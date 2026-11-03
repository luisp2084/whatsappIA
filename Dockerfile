FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
RUN useradd -r -u 10001 bot
USER bot
CMD ["sh", "-c", "exec uvicorn app.main:build --factory --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers"]
