ARG BASE_IMAGE=public.ecr.aws/ubuntu/ubuntu:24.04@sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467
FROM ${BASE_IMAGE}

ARG DEV_USER
ARG DEV_UID
ARG DEV_GID

LABEL maintainer="H100 Platform Automation <codexops@localhost>"
LABEL org.opencontainers.image.title="H100 long-lived development container"
LABEL org.opencontainers.image.description="Ubuntu 24.04 LTS SSH development environment without Docker daemon or host NVIDIA driver"
LABEL org.opencontainers.image.base.name="public.ecr.aws/ubuntu/ubuntu:24.04"
LABEL h100.base.digest="sha256:019e8eb29a85e74d64925745884f2ec79aa27e3feab36353d24656f4d6b89467"
LABEL h100.dev.user="${DEV_USER}"
LABEL h100.dev.uid="${DEV_UID}"
LABEL h100.dev.gid="${DEV_GID}"

ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DEV_USER=${DEV_USER}

RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      openssh-server \
      sudo \
      ca-certificates \
      curl \
      wget \
      git \
      vim \
      tmux \
      build-essential \
      python3 \
      python3-pip \
      python3-venv \
      python3-dev \
      pkg-config \
      cmake \
      ninja-build \
      jq \
      rsync \
      unzip \
      zip \
      tree \
      htop \
      less \
      procps \
      psmisc \
      lsof \
      file \
      iproute2 \
      iputils-ping \
      net-tools \
      dnsutils \
      locales \
      tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid "${DEV_GID}" "${DEV_USER}" \
    && useradd \
      --uid "${DEV_UID}" \
      --gid "${DEV_GID}" \
      --create-home \
      --shell /bin/bash \
      "${DEV_USER}" \
    && passwd --lock "${DEV_USER}" \
    && printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "${DEV_USER}" \
      > /etc/sudoers.d/90-h100-dev-user \
    && chmod 0440 /etc/sudoers.d/90-h100-dev-user \
    && visudo -cf /etc/sudoers \
    && install -d -o root -g root -m 0755 /run/sshd /etc/ssh/persistent

COPY --chmod=0644 sshd_config /etc/ssh/sshd_config
COPY --chmod=0755 entrypoint.sh /usr/local/sbin/h100-dev-entrypoint
COPY --chmod=0755 healthcheck.sh /usr/local/sbin/h100-dev-healthcheck

RUN ssh-keygen -q -t ed25519 -N '' -f /etc/ssh/persistent/ssh_host_ed25519_key \
    && ssh-keygen -q -t rsa -b 3072 -N '' -f /etc/ssh/persistent/ssh_host_rsa_key \
    && /usr/sbin/sshd -t \
    && rm -f /etc/ssh/persistent/ssh_host_ed25519_key \
      /etc/ssh/persistent/ssh_host_ed25519_key.pub \
      /etc/ssh/persistent/ssh_host_rsa_key \
      /etc/ssh/persistent/ssh_host_rsa_key.pub

EXPOSE 22/tcp

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["/usr/local/sbin/h100-dev-healthcheck"]

ENTRYPOINT ["/usr/local/sbin/h100-dev-entrypoint"]
