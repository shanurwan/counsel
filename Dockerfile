# Runtime image for Cloud Run. Built and pushed by .github/workflows/ci.yml.
FROM python:3.13-slim@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    COUNSEL_ROOT=/app

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . && useradd --system --uid 10001 counsel

# The registry and compiled grants travel with the image so a revision always
# runs the agents it was tested with.
COPY agents ./agents
COPY db ./db

USER 10001
EXPOSE 8080
CMD ["counsel-server"]
