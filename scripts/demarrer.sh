#!/bin/sh
# Crée un certificat auto-signé au premier démarrage si aucun n'est fourni (HTTPS obligatoire pour le micro des smartphones)
set -e
CONF=${CROSSGATE_CONFIG:-/config/config.yaml}
CERT_DIR=/config/certificats
if [ ! -f "$CERT_DIR/crossgate.crt" ]; then
  mkdir -p "$CERT_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
    -keyout "$CERT_DIR/crossgate.key" -out "$CERT_DIR/crossgate.crt" \
    -subj "/CN=crossgate" -addext "subjectAltName=DNS:crossgate,DNS:localhost,IP:127.0.0.1${CROSSGATE_IP:+,IP:$CROSSGATE_IP}"
  echo "Certificat auto-signé créé dans $CERT_DIR"
fi
exec python -m crossgate "$CONF"
