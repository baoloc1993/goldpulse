# GoldPulse — một image cho cả web và worker (khác nhau ở lệnh chạy, xem deployment/production/docker-compose.yml)
FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1 PYTHONUTF8=1 TZ=UTC
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
RUN useradd --create-home app && chown -R app /app
USER app
EXPOSE 8000
# Không --forwarded-allow-ips "*": uvicorn sẽ tin X-Forwarded-For do người gọi tự đặt. IP thật lấy từ
# CF-Connecting-IP trong api/limiter.py.
CMD ["uvicorn", "goldpulse.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
