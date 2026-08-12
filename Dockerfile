FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

FROM base AS test

COPY app ./app
COPY tests ./tests
COPY alembic.ini ./
COPY migrations ./migrations
COPY skills ./skills

CMD ["python", "-m", "unittest", "discover", "-s", "tests", "-v"]

FROM base AS production

COPY app ./app
COPY alembic.ini ./
COPY migrations ./migrations
COPY skills ./skills
COPY models/mindbridge-qwen2.5-7b-ft/Modelfile ./models/mindbridge-qwen2.5-7b-ft/Modelfile

EXPOSE 8080

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
