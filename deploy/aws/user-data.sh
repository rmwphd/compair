#!/bin/bash
# EC2 first-boot script for compair on Amazon Linux 2023 (t3.micro / t4g.micro).
# Installs Docker + the compose plugin, adds swap, and clones the repo to /opt/compair.
# It deliberately does NOT start the app: secrets (.env) are written over SSH afterwards,
# so they never sit in EC2 user-data (which anyone with ec2:Describe* access can read).
set -euxo pipefail

REPO_URL="${REPO_URL:-https://github.com/rmwphd/compair.git}"

# 1 GB swap: pip/pandas builds and bootstrap fits comfortably in 1 GB RAM + swap.
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

# docker compose v2 plugin (not packaged for AL2023)
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
  "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)"
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

git clone "$REPO_URL" /opt/compair
mkdir -p /opt/compair/data
chown -R ec2-user:ec2-user /opt/compair   # ec2-user is uid 1000 = PUID default

touch /var/lib/cloud/compair-bootstrap-done
