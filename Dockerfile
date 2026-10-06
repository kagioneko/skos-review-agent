# Cloud Run image. Sessions live in process memory, so deploy with
# --max-instances=1 (one instance = one consistent session store).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY agent/ agent/
COPY web/ web/
COPY demo/ demo/
COPY vendor/ vendor/

RUN useradd --create-home --uid 10001 app
USER app

CMD ["sh", "-c", "exec uvicorn web.server:app --host 0.0.0.0 --port ${PORT} --no-server-header"]
