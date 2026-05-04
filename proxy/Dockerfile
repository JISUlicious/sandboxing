# Egress proxy for the sandbox service. SPEC-402 / SPEC-403.
#
# Default-deny HTTP/HTTPS forwarder. Sandbox containers point
# HTTP_PROXY=http://proxy:3128 (set by ARCH-021) and only the domains
# enumerated in /etc/squid/allowed-domains.txt are reachable.
#
# Build:   docker build -t sandbox-proxy:latest proxy/
# Run:     docker run -d --name proxy --network sandbox_egress \
#               -v $(pwd)/proxy/allowed-domains.txt:/etc/squid/allowed-domains.txt:ro \
#               sandbox-proxy:latest
FROM debian:stable-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        squid \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY squid.conf /etc/squid/squid.conf
COPY allowed-domains.txt /etc/squid/allowed-domains.txt

# Expose the cache directory and log paths for the squid user.
RUN chown -R proxy:proxy /var/spool/squid /var/log/squid \
    && chmod 0644 /etc/squid/squid.conf /etc/squid/allowed-domains.txt

EXPOSE 3128

USER proxy
# -N keeps squid in the foreground so Docker logs and lifecycle work.
# -d 1 sends debug level 1 to stderr (Docker captures it).
CMD ["/usr/sbin/squid", "-N", "-d", "1"]
