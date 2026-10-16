FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir . \
    && groupadd --system chaptera \
    && useradd --system --gid chaptera --home-dir /home/chaptera chaptera \
    && mkdir -p /data \
    && chown chaptera:chaptera /data
ENV CHAPTERA_DB_PATH=/data/jobs.sqlite3
USER chaptera
CMD ["chaptera-server", "serve"]
