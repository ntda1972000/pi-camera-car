#!/usr/bin/env bash
# setup.sh — download mediamtx binary for HLS/RTSP mode
# Run once after cloning: bash setup.sh
set -e

MEDIAMTX_VER="v1.18.0"
DIR="$(cd "$(dirname "$0")" && pwd)"

# Detect architecture
ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64) ARCH_SUFFIX="arm64" ;;
  armv7l|armhf)  ARCH_SUFFIX="armv7" ;;
  x86_64)        ARCH_SUFFIX="amd64" ;;
  *)
    echo "Unsupported architecture: $ARCH"
    exit 1
    ;;
esac

URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VER}/mediamtx_${MEDIAMTX_VER}_linux_${ARCH_SUFFIX}.tar.gz"

echo "Downloading mediamtx ${MEDIAMTX_VER} for ${ARCH_SUFFIX}..."
curl -L "$URL" -o /tmp/mediamtx.tar.gz
tar -xzf /tmp/mediamtx.tar.gz -C "$DIR" mediamtx
rm /tmp/mediamtx.tar.gz
chmod +x "$DIR/mediamtx"
echo "mediamtx installed: $("$DIR/mediamtx" --version 2>&1 | head -1)"

echo ""
echo "Setup complete. Install Python dependencies:"
echo "  pip install -r requirements.txt"
