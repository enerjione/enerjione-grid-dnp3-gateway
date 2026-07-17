# EnerjiOne DNP3 Gateway - container image
# (Saha DNP3 outstation cihazlarindan veri toplayip NATS JetStream'e basar.)
#
# Calisma akisi:
#   - Tek imaj, N container: her gateway icin ayri container, ayri .env.
#   - Default DNP3 kutuphanesi: nfm-dnp3 (saf python; PyPI'de Linux desteklenir).
#     yadnp3 (OpenDNP3, Group 110/string destegi) PyPI'de yalnizca Windows wheel'i
#     olarak yayinlanir; Linux'ta kullanilmasi icin kaynaktan derlenmesi gerekir.
#     Build-arg DNP3_LIBRARY=yadnp3 verilirse builder stage f0rw4rd/opendnp3
#     fork'undan derlemeyi dener. Default olarak DEVRE DISI; ek yuk getirmemesi icin.
#
# Build:
#   # hizli (saf python, string DESTEKLENMEZ - yalnizca numeric tip okumalar):
#   docker build -t e1/dnp3-gateway:latest .
#
#   # tam ozellik (yadnp3, string + Group 110 destegi - kaynaktan derler, ~10dk):
#   docker build --build-arg DNP3_LIBRARY=yadnp3 -t e1/dnp3-gateway:latest .
#
# Run (tek instance):
#   docker run --rm -d --name gw-001 \
#     --env-file ./gateways/GW-001/.env \
#     -v e1-gw-001-state:/app/.gateway_state \
#     -p 8020:8020 \
#     e1/dnp3-gateway:latest
#
# Coklu instance: bkz. docs/DOCKER.md ve scripts/render_compose.py.

ARG PYTHON_VERSION=3.11
ARG DNP3_LIBRARY=yadnp3

# ---------- Builder stage --------------------------------------------------
# yadnp3 (OpenDNP3 native) PyPI'de manylinux_2_28_x86_64 wheel'i ile yayinda
# (3.2.1.1+); kaynaktan derlemeye gerek yok. dnp3py modu fallback olarak kalir.
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder

ARG DNP3_LIBRARY

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Proje bagimliliklarini ayri katmanda kur — kaynak degisince yeniden derleme yok.
COPY requirements.txt /build/requirements.txt
RUN pip install --upgrade pip \
    && pip wheel --wheel-dir=/build/wheels -r /build/requirements.txt

# ---------- Runtime stage --------------------------------------------------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime

ARG DNP3_LIBRARY

LABEL org.opencontainers.image.title="enerjione-dnp3-gateway" \
      org.opencontainers.image.vendor="Form Elektrik" \
      org.opencontainers.image.source="https://github.com/fikretsafak/EnerjiOneGrid-DNP3-Gateway" \
      org.opencontainers.image.description="EnerjiOne DNP3 master gateway: telemetry collection -> NATS JetStream"

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GATEWAY_STATE_DIR=/app/.gateway_state \
    WORKER_HEALTH_HOST=0.0.0.0 \
    WORKER_HEALTH_PORT=8020 \
    DNP3_LIBRARY=${DNP3_LIBRARY}

# Non-root user: dnp3 outbound TCP icin yeterli, root yetkisi gereksiz.
RUN groupadd --system --gid 1000 e1 \
    && useradd --system --uid 1000 --gid e1 --home /app --shell /usr/sbin/nologin e1 \
    && mkdir -p /app /app/.gateway_state \
    && chown -R e1:e1 /app

WORKDIR /app

# Onceden derlenmis wheel'leri kur (proje deps + opsiyonel yadnp3).
COPY --from=builder /build/wheels /tmp/wheels
RUN pip install --no-index --find-links=/tmp/wheels \
        $(ls /tmp/wheels/*.whl 2>/dev/null) \
    && rm -rf /tmp/wheels

# DNP3 kutuphanesi PyPI'den dogrudan kurulur (manylinux wheel hazir):
#   - yadnp3 (default, OpenDNP3 native) -> Group 110 string + tum tipler
#   - nfm-dnp3 (fallback, saf python)    -> sadece numeric (no Group 110)
RUN if [ "${DNP3_LIBRARY}" = "yadnp3" ]; then \
        pip install "yadnp3==3.2.1.1" \
        && echo "[image] DNP3 library = yadnp3 (OpenDNP3 native; Group 110 supported)"; \
    else \
        pip install "nfm-dnp3>=1.0.1,<2.0" \
        && echo "[image] DNP3 library = nfm-dnp3 (pure python; no Group 110 strings)"; \
    fi

# Kaynak kodu son katman — sadece kod degistiginde rebuild.
COPY --chown=e1:e1 src /app/src
COPY --chown=e1:e1 pyproject.toml VERSION /app/
COPY --chown=e1:e1 docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh \
    && pip install --no-deps -e /app

USER e1

EXPOSE 8020

# Health check: /health endpoint 200 donmeli.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; \
        sys.exit(0) if urllib.request.urlopen(\
        f'http://127.0.0.1:{__import__(\"os\").environ.get(\"WORKER_HEALTH_PORT\",\"8020\")}/health',\
        timeout=3).status==200 else sys.exit(1)" || exit 1

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "dnp3_gateway"]
