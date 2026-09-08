FROM python:3.12-slim-bookworm AS build
RUN apt-get update && apt-get install -y --no-install-recommends gcc g++ libfftw3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY app/requirements.txt /tmp/requirements.txt
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --require-hashes -r /tmp/requirements.txt

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends libfftw3-double3 libfftw3-single3 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 flight && useradd --uid 10001 --gid flight --create-home flight
COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app
COPY --chown=flight:flight app /app
ARG APP_REVISION=development
ENV APP_REVISION=$APP_REVISION PYTHONUNBUFFERED=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1
ENV STORAGE_PATH=/data
USER flight
EXPOSE 8080
CMD ["sh", "-c", "python -c 'from runtime_config import cookie_secret, require_persistent_storage; cookie_secret(); require_persistent_storage()' && python setup_db.py && exec python serve.py --port ${PORT:-8080} --address 0.0.0.0 --use-xheaders"]
