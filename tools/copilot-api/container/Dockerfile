FROM python:3.14-slim-bookworm@sha256:945c2676dc2b18e0c1f16879244122c57c0ffede60006bf3df159c507a31bcae

RUN python -m pip install --no-cache-dir setuptools==84.0.0

COPY launch.py /opt/bridge/launch.py

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["python", "/opt/bridge/launch.py"]
