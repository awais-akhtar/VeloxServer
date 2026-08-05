FROM python:3.13-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN useradd --system --create-home --home-dir /srv/veloxserver veloxserver

WORKDIR /srv/veloxserver
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN python -m pip install --no-cache-dir .

USER veloxserver
EXPOSE 8080/tcp 8080/udp
ENTRYPOINT ["veloxserver"]
CMD ["--config", "/etc/veloxserver/veloxserver.toml"]
