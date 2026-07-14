FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY config.example.yaml ./
RUN pip install --no-cache-dir -e .
EXPOSE 8080
CMD ["mcg", "serve", "-c", "config.yaml"]
