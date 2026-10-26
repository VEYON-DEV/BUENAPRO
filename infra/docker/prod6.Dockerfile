# Build a selective PROD6 release from the explicitly verified worker base.
# This does not ship the unrelated local PROD2/PROD4 workspace changes.
ARG PROD6_BASE_IMAGE
FROM ${PROD6_BASE_IMAGE}
COPY workers/seace/src/buenapro_worker/prod6 /usr/local/lib/python3.12/site-packages/buenapro_worker/prod6
COPY workers/seace/src/buenapro_worker/seace/client.py /usr/local/lib/python3.12/site-packages/buenapro_worker/seace/client.py
COPY workers/seace/src/buenapro_worker/seace/egress.py /usr/local/lib/python3.12/site-packages/buenapro_worker/seace/egress.py
COPY workers/seace/src/buenapro_worker/seace/pacing.py /usr/local/lib/python3.12/site-packages/buenapro_worker/seace/pacing.py
COPY workers/seace/src/buenapro_worker/jobs/download_file.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/download_file.py
COPY workers/seace/src/buenapro_worker/jobs/process_contract.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/process_contract.py
COPY workers/seace/src/buenapro_worker/jobs/extract_tdr.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/extract_tdr.py
COPY workers/seace/src/buenapro_worker/jobs/poll_prod6_profile.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/poll_prod6_profile.py
COPY workers/seace/src/buenapro_worker/jobs/derive_summary.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/derive_summary.py
COPY workers/seace/src/buenapro_worker/jobs/analyze_match.py /usr/local/lib/python3.12/site-packages/buenapro_worker/jobs/analyze_match.py
COPY workers/seace/src/buenapro_worker/matching/analyzer.py /usr/local/lib/python3.12/site-packages/buenapro_worker/matching/analyzer.py
COPY workers/seace/src/buenapro_worker/normalization/facets.py /usr/local/lib/python3.12/site-packages/buenapro_worker/normalization/facets.py
COPY workers/seace/src/buenapro_worker/documents/pdf.py /usr/local/lib/python3.12/site-packages/buenapro_worker/documents/pdf.py
COPY workers/seace/prompts/prod6_tdr_reading_v1.txt workers/seace/prompts/prod6_eett_reading_v1.txt /app/workers/seace/prompts/
CMD ["python", "-m", "buenapro_worker.prod6.runtime"]

ARG VCS_REF=unknown
LABEL org.opencontainers.image.revision=$VCS_REF
