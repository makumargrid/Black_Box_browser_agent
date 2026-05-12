FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir uv

COPY . .
RUN uv sync --frozen --no-dev
RUN ./.venv/bin/python -m playwright install --with-deps chromium

EXPOSE 8080

CMD ["./.venv/bin/python", "-m", "blackbox_service.main"]
