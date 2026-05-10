FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Render/Railway/Fly выставляют PORT
EXPOSE 8000
ENV PORT=8000

# gunicorn для Linux-контейнеров
CMD gunicorn app:app --workers 2 --threads 4 --timeout 60 --bind 0.0.0.0:$PORT
