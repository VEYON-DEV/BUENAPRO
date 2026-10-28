# Pass an explicitly verified Python 3.12 Debian worker/base image (prefer digest).
ARG PROD2_BASE_IMAGE
FROM ${PROD2_BASE_IMAGE}
ARG VCS_REF
RUN test -n "$VCS_REF"
WORKDIR /app
RUN apt-get update \
 && apt-get install -y --no-install-recommends libarchive-tools libreoffice-writer \
      poppler-utils ghostscript tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
COPY workers/seace/pyproject.toml /app/workers/seace/pyproject.toml
COPY workers/seace/src /app/workers/seace/src
COPY workers/seace/prompts /app/workers/seace/prompts
COPY scripts/run_prod2_selected.py scripts/run_prod2_accepted.py scripts/run_prod2_parallel_discovery.py scripts/reread_prod2_reviews.py /app/scripts/
COPY docs/sources /app/docs/sources
COPY db/migrations /app/db/migrations
RUN pip install --no-cache-dir /app/workers/seace boto3 botocore
LABEL org.opencontainers.image.revision=$VCS_REF
CMD ["python", "-m", "buenapro_worker.prod2.service", "--config", "/run/prod2/service.private.json", "--state-dir", "/var/lib/buenapro/prod2"]
