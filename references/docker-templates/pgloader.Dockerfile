# -----------------------------------------------------------------------
# Stage 1: Build pgloader from source with 4GB SBCL dynamic space.
#
# The Ubuntu/Debian package ships with a hardcoded 1GB heap (DYNSIZE=1024),
# which is insufficient for MSSQL databases with 1000+ tables.  Building
# from source with DYNSIZE=4096 gives a 4GB heap.
# -----------------------------------------------------------------------
FROM debian:bookworm AS builder

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        sbcl \
        unzip \
        curl \
        make \
        git \
        gawk \
        freetds-dev \
        libsqlite3-dev \
        libzip-dev \
        libssl-dev \
        ca-certificates \
        && \
    rm -rf /var/lib/apt/lists/*

# Clone latest stable pgloader
RUN git clone --depth=1 --branch v3.6.10 \
      https://github.com/dimitri/pgloader.git /pgloader 2>/dev/null || \
    git clone --depth=1 https://github.com/dimitri/pgloader.git /pgloader

WORKDIR /pgloader

# Build with 4GB dynamic space.
# 'make pgloader' automatically:
#   1. Downloads Quicklisp bootstrap
#   2. Installs all CL dependencies via Quicklisp
#   3. Compiles pgloader into a single binary
# This step requires internet access (Quicklisp downloads CL packages).
RUN make pgloader DYNSIZE=4096 && \
    echo "Built: $(./build/bin/pgloader --version)" && \
    ls -lh ./build/bin/pgloader

# -----------------------------------------------------------------------
# Stage 2: Minimal runtime image
# -----------------------------------------------------------------------
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        freetds-dev \
        libsybdb5 \
        freetds-common \
        libsqlite3-0 \
        ca-certificates \
        && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /pgloader/build/bin/pgloader /usr/local/bin/pgloader

# Configure FreeTDS for SQL Server 2022:
#   tds version = 7.4  — required for SQL Server 2012+
#   encryption = off   — do not initiate TLS
RUN printf '[global]\n\ttds version = 7.4\n\tencryption = off\n\ttimeout = 30\n\tconnect timeout = 30\n' \
    > /etc/freetds/freetds.conf

# Wrapper entrypoint: installs SPG certs mounted at /spg-certs/ before running pgloader.
# Solves the self-signed cert chain issue without requiring --no-ssl-cert-verification.
RUN printf '#!/bin/sh\n\
if [ -d /spg-certs ] && ls /spg-certs/*.crt >/dev/null 2>&1; then\n\
    cp /spg-certs/*.crt /usr/local/share/ca-certificates/\n\
    update-ca-certificates -f >/dev/null 2>&1\n\
fi\n\
exec pgloader "$@"\n' > /usr/local/bin/pgloader-wrapper && \
    chmod +x /usr/local/bin/pgloader-wrapper

ENTRYPOINT ["/usr/local/bin/pgloader-wrapper"]
