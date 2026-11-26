FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg ca-certificates curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY khazna_bot.py gen_warp.py ./

# Free Cloudflare WARP egress: register a WireGuard profile at build time and
# expose it as a local SOCKS5 proxy via wireproxy (pure userspace, no root).
RUN python gen_warp.py /app/wireproxy.conf && \
    curl -fsSL https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz \
      | tar -xz -C /usr/local/bin wireproxy && \
    chmod +x /usr/local/bin/wireproxy

ENV PORT=7860
EXPOSE 7860
CMD ["sh", "-c", "/usr/local/bin/wireproxy -c /app/wireproxy.conf >/tmp/wireproxy.log 2>&1 & exec python khazna_bot.py"]
