FROM python:3.11-slim

WORKDIR /app

COPY 05_scripts/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY 05_scripts/kobrax_moonraker_bridge.py .
COPY 05_scripts/env_loader.py .
COPY 05_scripts/kobrax_client.py .

EXPOSE 7125

ENTRYPOINT ["python", "kobrax_moonraker_bridge.py"]
