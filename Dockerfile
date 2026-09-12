FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        git \
        build-essential \
        libsndfile1 \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install CPU-only torch first (keeps image smaller)
RUN pip install --upgrade pip && \
    pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

RUN mkdir -p uploads outputs

EXPOSE 5000

# -w 1: single worker because jobs are in-process
# -t 3600: 1-hour timeout for long Whisper/Demucs runs
CMD ["gunicorn", "-b", "0.0.0.0:5000", "-w", "1", "-t", "3600", "--threads", "4", "app:app"]