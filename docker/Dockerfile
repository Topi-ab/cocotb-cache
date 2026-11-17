# Toolchain image: GHDL + NVC + Verilator + Icarus Verilog, plus the Python
# wheels (cocotb from PyPI at the version pinned in /cocotb.version, passed in
# by docker/build.sh) that the entrypoint installs into /work/.venv.
FROM ubuntu:24.04 AS build

ARG VERILATOR_VERSION=v5.054
ARG IVERILOG_VERSION=v13_0

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        git ca-certificates build-essential autoconf flex bison gperf help2man \
        perl python3 libfl-dev zlib1g-dev ccache libgoogle-perftools-dev numactl \
    && rm -rf /var/lib/apt/lists/*

# Verilator
RUN git clone --depth 1 --branch ${VERILATOR_VERSION} https://github.com/verilator/verilator.git /src/verilator \
    && cd /src/verilator && autoconf && ./configure --prefix=/opt/verilator \
    && make -j"$(nproc)" && make install

# Icarus Verilog
RUN git clone --depth 1 --branch ${IVERILOG_VERSION} https://github.com/steveicarus/iverilog.git /src/iverilog \
    && cd /src/iverilog && sh autoconf.sh && ./configure --prefix=/opt/iverilog \
    && make -j"$(nproc)" && make install


FROM ubuntu:24.04

ARG NVC_VERSION=1.23.1
ARG GHDL_VERSION=6.0.0

ADD https://github.com/nickg/nvc/releases/download/r${NVC_VERSION}/nvc_${NVC_VERSION}-1_amd64_ubuntu-24.04.deb /tmp/nvc.deb

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        /tmp/nvc.deb \
        python3 python3-venv python3-dev \
        build-essential make perl git ca-certificates curl ccache \
        libfl2 zlib1g-dev libgoogle-perftools4 numactl \
        libgnat-13 libllvm18 \
    && rm -rf /var/lib/apt/lists/* /tmp/nvc.deb

# GHDL (LLVM backend, prebuilt for Ubuntu 24.04)
RUN mkdir -p /opt/ghdl \
    && curl -fsSL https://github.com/ghdl/ghdl/releases/download/v${GHDL_VERSION}/ghdl-llvm-${GHDL_VERSION}-ubuntu24.04-x86_64.tar.gz \
       | tar -xz -C /opt/ghdl --strip-components=1

# versitygw (an S3-compatible server) for hermetic store tests:
# the test fixture runs it as a local subprocess, no Docker access needed.
ARG VERSITYGW_VERSION=1.10.0
ARG VERSITYGW_SHA256=8f9869752939d4dc9cfe1bddb935a02cc6610e95319a3a9fc2947d2a6ebd2c91
RUN cd /tmp \
    && curl -fsSLo vgw.tar.gz https://github.com/versity/versitygw/releases/download/v${VERSITYGW_VERSION}/versitygw_v${VERSITYGW_VERSION}_Linux_x86_64.tar.gz \
    && echo "${VERSITYGW_SHA256}  vgw.tar.gz" | sha256sum -c - \
    && mkdir -p /opt/versitygw/bin \
    && tar -xzf vgw.tar.gz --strip-components=1 -C /opt/versitygw/bin versitygw_v${VERSITYGW_VERSION}_Linux_x86_64/versitygw \
    && rm vgw.tar.gz

COPY --from=build /opt/verilator /opt/verilator
COPY --from=build /opt/iverilog /opt/iverilog

# Python wheels baked into the image. The venv itself is created at container
# start in the mounted project (/work/.venv), offline from these wheels (see
# entrypoint.sh). cocotb is the PyPI release, the same users install; there is
# no source build of cocotb anywhere (ext/cocotb is a read-only reference).
ARG COCOTB_VERSION
RUN test -n "${COCOTB_VERSION}" || { echo "COCOTB_VERSION build arg missing (use docker/build.sh)"; exit 1; } \
    && python3 -m venv /opt/tools \
    && /opt/tools/bin/pip download --no-cache-dir -d /opt/wheels \
       "cocotb==${COCOTB_VERSION}" \
       pip pytest "setuptools>=77,<84" wheel \
       boto3 pytest-xdist "pyslang==12.1.0" \
    && rm -rf /opt/tools
ENV COCOTB_CACHE_IMAGE_COCOTB_VERSION=${COCOTB_VERSION}

ENV PATH=/opt/versitygw/bin:/opt/ghdl/bin:/opt/verilator/bin:/opt/iverilog/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    VERILATOR_ROOT=/opt/verilator/share/verilator \
    HOME=/tmp/home \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN mkdir -p /tmp/home && chmod 1777 /tmp/home

# Content-based image identity for the cache key (toolchain.image): installed
# packages, everything under /opt and the wheel set. Exported by the entrypoint
# as COCOTB_CACHE_IMAGE.
RUN { dpkg-query -W -f '${Package} ${Version} ${Architecture}\n'; \
      find /opt -type f -printf '%P %s\n' | sort; } \
    | sha256sum | cut -d' ' -f1 > /etc/cocotb-cache-image-id

COPY versions.sh /usr/local/bin/versions
COPY entrypoint.sh /usr/local/bin/entrypoint
RUN chmod 755 /usr/local/bin/versions /usr/local/bin/entrypoint

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/entrypoint"]
CMD ["/bin/bash"]
