FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Mount point for config and session cache
VOLUME ["/data"]

# Default config dir (can override with CONFIG_DIR env var)
ENV CONFIG_DIR=/data

EXPOSE 8000

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
