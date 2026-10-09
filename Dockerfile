# AnyChain Transaction Assistant: the CLI and the local API in one image (PHASE4 T4, D58).
# No secrets inside: a network config (and, for a written answer, an API key) come from outside at run time.
#   docker build -t anychain .
#   docker run --rm -p 127.0.0.1:8000:8000 anychain                      # API + page, Ethereum config
#   docker run --rm anychain anychain explain 0x... --no-llm               # the CLI
#   docker run --rm -v $PWD/my.yaml:/app/configs/my.yaml -p 127.0.0.1:8000:8000 anychain \
#       anychain serve --config configs/my.yaml --host 0.0.0.0
FROM python:3.11-slim

RUN pip install --no-cache-dir uv==0.9.18
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1

# dependencies first, so a code change does not reinstall them
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY prompts ./prompts
COPY configs ./configs
RUN uv sync --frozen --no-dev

ENV PATH=/app/.venv/bin:$PATH ANYCHAIN_CONFIG=configs/ethereum-mainnet.yaml
RUN useradd --create-home anychain && mkdir -p /app/data && chown anychain /app/data
USER anychain
VOLUME /app/data
EXPOSE 8000
# 0.0.0.0 inside the container only; publish it on the host's 127.0.0.1 (the API has no authentication)
CMD ["anychain", "serve", "--host", "0.0.0.0", "--port", "8000"]
