# ViralClip AI backend image - used for both the API and the worker.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# ffmpeg (with libass for captions) + the caption fonts
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-montserrat fonts-dejavu-core ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first (better layer caching)
COPY pyproject.toml ./
ARG INSTALL_LOCAL_WHISPER=false
RUN python - <<'PY' > /tmp/requirements.txt
import tomllib
data = tomllib.load(open("pyproject.toml", "rb"))["project"]
deps = list(data["dependencies"]) + data["optional-dependencies"]["s3"]
print("\n".join(deps))
PY
RUN pip install -r /tmp/requirements.txt \
    && if [ "$INSTALL_LOCAL_WHISPER" = "true" ]; then pip install "faster-whisper>=1.0"; fi

# Optional: better face detector (YuNet). Falls back to OpenCV's Haar cascades if unavailable.
RUN mkdir -p /opt/models \
    && (curl -fsSL -o /opt/models/face_detection_yunet.onnx \
        https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx \
        || rm -f /opt/models/face_detection_yunet.onnx)

COPY alembic.ini ./
COPY alembic ./alembic
COPY assets ./assets
COPY app ./app

ENV DATA_DIR=/data \
    FACE_MODEL_PATH=/opt/models/face_detection_yunet.onnx \
    FACE_MODEL_URL="" \
    HF_HOME=/data/hf-cache
RUN mkdir -p /data

EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=5s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
