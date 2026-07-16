FROM node:22-alpine AS admin-web-build

WORKDIR /build/apps/admin_web
COPY apps/admin_web/package.json apps/admin_web/package-lock.json ./
RUN npm ci
COPY apps/admin_web/ ./
RUN npm run build


FROM python:3.12-slim AS admin-runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TRIPPOST_ADMIN_STATIC_DIR=/app/apps/admin_web/dist

WORKDIR /app

RUN addgroup --system --gid 10001 trippost \
    && adduser --system --uid 10001 --ingroup trippost --home /nonexistent --no-create-home trippost

COPY pyproject.toml ./
COPY src/ ./src/
RUN python -m pip install --no-cache-dir \
        "fastapi>=0.115" \
        "pydantic>=2" \
        "python-multipart>=0.0.9" \
        "uvicorn>=0.30" \
    && python -m pip install --no-cache-dir --no-deps .

COPY apps/__init__.py ./apps/__init__.py
COPY apps/admin_api/ ./apps/admin_api/
COPY --from=admin-web-build /build/apps/admin_web/dist/ ./apps/admin_web/dist/

RUN chown -R trippost:trippost /app

USER 10001:10001
EXPOSE 8787

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/api/meta', timeout=3).read()"]

CMD ["python", "-m", "uvicorn", "apps.admin_api.app.main:app", "--host", "0.0.0.0", "--port", "8787", "--no-access-log"]
