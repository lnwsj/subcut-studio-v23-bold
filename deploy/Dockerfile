FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-noto-core fonts-noto-extra \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv/subcut
COPY app/requirements.txt app/requirements.txt
RUN python -m pip install --upgrade pip wheel setuptools \
    && python -m pip install -r app/requirements.txt

COPY app app
COPY scripts scripts
COPY docs docs
COPY README.md CHANGELOG.md ./

EXPOSE 8787
CMD ["python", "app/main.py"]
