FROM python:3.11-slim-bookworm@sha256:2333bd330d12de02514770b3585cad313644316047cdee24a7acfdece6de6efb
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /controller
COPY runtime_controller/requirements.lock .
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: -r requirements.lock && groupadd -g 10002 controller && useradd -u 10002 -g 10002 controller
COPY runtime_controller ./runtime_controller
USER 10002:10002
CMD ["python", "-m", "runtime_controller.app"]
