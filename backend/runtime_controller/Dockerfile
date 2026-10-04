FROM python:3.11-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /controller
COPY runtime_controller/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && groupadd -g 10002 controller && useradd -u 10002 -g 10002 controller
COPY runtime_controller ./runtime_controller
USER 10002:10002
CMD ["python", "-m", "runtime_controller.app"]
