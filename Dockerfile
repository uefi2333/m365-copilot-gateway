FROM python:3.12-slim

WORKDIR /app

# system deps (certs for MSAL / https)
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config.example.yaml ./

RUN pip install --no-cache-dir -e . \
  && mkdir -p /app/data

# default config if user mounts none (still must set secrets)
RUN if [ ! -f /app/config.yaml ]; then cp config.example.yaml config.yaml; fi

ENV PYTHONUNBUFFERED=1
EXPOSE 8080

# bind all interfaces inside container
CMD ["mcg", "serve", "-c", "config.yaml", "--host", "0.0.0.0", "--port", "8080"]
