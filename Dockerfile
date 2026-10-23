FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1 MEDIA_DIR=/data/media

WORKDIR /srv/okiedoke

# Dependencies come from pyproject.toml (the single source of truth). The code is
# run from source rather than pip-installed, so app/ can find ../web.
COPY pyproject.toml ./
RUN python -c "import tomllib; print('\n'.join(tomllib.load(open('pyproject.toml','rb'))['project']['dependencies']))" > /tmp/requirements.txt \
 && pip install -r /tmp/requirements.txt && rm /tmp/requirements.txt

COPY app ./app
COPY db/migrations ./db/migrations
COPY web ./web

RUN useradd --system --uid 10001 --home-dir /srv/okiedoke okiedoke \
 && mkdir -p /data/media && chown -R okiedoke /data
USER okiedoke
VOLUME /data/media
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

# One worker on purpose: the background sync scheduler lives in the process.
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
