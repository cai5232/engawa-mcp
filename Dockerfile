FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    ENGAWA_HOST=0.0.0.0 \
    ENGAWA_PORT=8080

WORKDIR /app

COPY pyproject.toml README.en.md LICENSE ./
COPY engawa_mcp ./engawa_mcp

RUN python -m pip install --upgrade pip \
    && python -m pip install .

EXPOSE 8080

CMD ["python", "-m", "engawa_mcp", "--http"]
