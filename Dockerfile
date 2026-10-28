FROM python:3.12-slim-bookworm@sha256:e697c81897302a4c4333bade000d98038fbb21eb064f8ee1fd42d02ebcd9d337

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app
WORKDIR /app
COPY pyproject.toml run.py ./
COPY app ./app
COPY docker/healthcheck.py /opt/panel-healthcheck.py
RUN pip install --no-cache-dir . \
    && groupadd --gid 10001 panel \
    && useradd --uid 10001 --gid 10001 --no-create-home panel \
    && mkdir /app/data && chown panel:panel /app/data
USER 10001:10001
EXPOSE 8765
CMD ["python", "run.py"]
