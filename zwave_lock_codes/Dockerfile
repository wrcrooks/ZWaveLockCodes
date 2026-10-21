# Plain Alpine keeps the image and memory footprint small: Python plus aiohttp
# is the only runtime, and there is no s6 supervisor (config.yaml sets init: true).
FROM alpine:3.21

RUN apk add --no-cache python3 py3-aiohttp

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY app/zwave_lock_codes /app/zwave_lock_codes

CMD ["python3", "-m", "zwave_lock_codes"]
