# Python 3.11 matches requires-python and the single locked uv project.
# Build context is the repository root:
#   docker build -f backend/runtime_controller/Dockerfile .
ARG PYTHON_BASE=python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb
FROM ghcr.io/astral-sh/uv:0.13.0 AS uv
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    VIRTUAL_ENV=/controller/.venv \
    PATH="/controller/.venv/bin:$PATH"
COPY --from=uv /uv /uvx /bin/

WORKDIR /controller

# The controller installs only its own dependency group from the root lock, so
# the heavy application dependencies never reach this image.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-default-groups --only-group controller --no-install-project && \
    groupadd -g 10002 controller && useradd -u 10002 -g 10002 controller
RUN mkdir /state && chown 10002:10002 /state && chmod 0700 /state
COPY backend/runtime_controller ./runtime_controller
COPY backend/runtime_common ./runtime_common
USER 10002:10002
CMD ["python", "-m", "runtime_controller.app"]
