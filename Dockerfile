# Runtime dependencies are pinned by pyproject.toml; no private build inputs are accepted.
FROM python:3.12-slim AS wheels
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN python -m pip wheel --no-cache-dir --wheel-dir /wheels .

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    NL2SPARQL_BACKEND=stub
WORKDIR /app
COPY --from=wheels /wheels /wheels
RUN python -m pip install --no-cache-dir --no-index --find-links=/wheels nl2sparql \
    && rm -rf /wheels \
    && groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --no-create-home gateway \
    && mkdir -p /app/logs /app/eval/reports /app/assets \
    && chown -R gateway:gateway /app/logs /app/eval/reports /app/assets
# Keep source paths stable: Settings.REPO_ROOT must resolve to /app, beside frontend/data.
COPY src/ ./src/
COPY frontend/ ./frontend/
COPY data/examples.jsonl data/team_examples.jsonl data/schema_cards.json data/property_kinds.json ./data/
# The 45 dev reference queries are retrieval examples (CONTEXT_DEV_REFERENCES, on by default); without this
# file they are silently missing from the image. Only its dev rows are read.
COPY eval/query_questions.jsonl ./eval/query_questions.jsonl
USER 10001:10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3).close()"]
CMD ["python", "-m", "nl2sparql", "--host", "0.0.0.0", "--port", "8080"]
