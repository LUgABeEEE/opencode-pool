# opencode-pool 中转镜像
#
# 容器内约定：
#   * 配置挂进 /data/pool.config.json（OPENCODE_POOL_CONFIG 已指过去）
#   * 容器里 config 的 "listen" 必须写 0.0.0.0:8790 —— 默认的 127.0.0.1 只在容器内可达
#   * 建议 config 的 "secrets_file" 写 /data/secrets.json（挂载点内，密钥不随容器丢）
FROM python:3.12-slim

# 非 root 运行；uid 1000 方便和宿主普通用户共享 /data 挂载的属主
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin pool

WORKDIR /app
COPY pyproject.toml README.md ./
COPY opencode_pool/ opencode_pool/
RUN pip install --no-cache-dir .

# /data 是挂载点：pool.config.json / secrets.json / pool_state.json 都在这里
RUN mkdir -p /data && chown pool:pool /data
VOLUME ["/data"]

ENV OPENCODE_POOL_CONFIG=/data/pool.config.json
USER pool

EXPOSE 8790

# 打就绪端点。注意：/healthz 是就绪语义（0.2.0 起），号池无可用账号时回 503，
# 届时容器显示 unhealthy 属预期，不代表进程死了。
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=4)"

CMD ["python3", "-m", "opencode_pool", "serve"]
