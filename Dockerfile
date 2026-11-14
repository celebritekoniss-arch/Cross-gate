FROM python:3.12-slim

# PortAudio pour les cartes son USB, openssl pour le certificat auto-signé
RUN apt-get update \
 && apt-get install -y --no-install-recommends libportaudio2 openssl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY crossgate ./crossgate
COPY web ./web
COPY scripts ./scripts

ENV PYTHONUNBUFFERED=1
EXPOSE 8443/tcp 50001/udp
CMD ["sh", "scripts/demarrer.sh"]
