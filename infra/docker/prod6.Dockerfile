# Build a selective PROD6 release from the explicitly verified worker base.
# This does not ship the unrelated local PROD2/PROD4 workspace changes.
ARG PROD6_BASE_IMAGE
FROM ${PROD6_BASE_IMAGE}
COPY workers/seace/src/buenapro_worker/prod6 /usr/local/lib/python3.12/site-packages/buenapro_worker/prod6
COPY workers/seace/src/buenapro_worker/seace/client.py /usr/local/lib/python3.12/site-packages/buenapro_worker/seace/client.py
COPY workers/seace/src/buenapro_worker/jobs/extract_tdr.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/extract_tdr.py
COPY workers/seace/src/buenapro_worker/jobs/poll_prod6_profile.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/poll_prod6_profile.py
CMD ["python", "-m", "buenapro_worker.prod6.runtime"]

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF
