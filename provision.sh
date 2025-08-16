#!/usr/bin/env bash

set -eux
set -o pipefail

export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get -y upgrade
apt-get -y install --no-install-recommends \
  gpg \
  lsb-release \
  curl \
  ca-certificates \
  ubuntu-advantage-tools

UBUNTU_CODENAME="$(lsb_release -sc)"
KEYRING_DIR="/etc/apt/keyrings"
KEYRING_PATH="${KEYRING_DIR}/infrahouse.gpg"
REPO_HOST="release-${UBUNTU_CODENAME}.infrahouse.com"
REPO_URL="https://${REPO_HOST}/"
REPO_LIST="/etc/apt/sources.list.d/infrahouse.list"


install -d -m 0755 "${KEYRING_DIR}"
tmpkey="$(mktemp)"
curl --fail --silent --show-error --location --retry 5 \
  "${REPO_URL}DEB-GPG-KEY-release-${UBUNTU_CODENAME}.infrahouse.com" \
  | gpg --dearmor > "${tmpkey}"
install -m 0644 "${tmpkey}" "${KEYRING_PATH}"
rm -f "${tmpkey}"

echo "deb [signed-by=${KEYRING_PATH}] ${REPO_URL} ${UBUNTU_CODENAME} main" \
  | tee "${REPO_LIST}" >/dev/null

apt-get update
apt-get -y install --no-install-recommends \
  awscli \
  build-essential \
  infrahouse-toolkit \
  jq \
  gcc \
  make \
  net-tools \
  python3 \
  python-is-python3 \
  python3-virtualenv \
  python3-pip \
  ruby-dev \
  ruby-rubygems \
  sysstat

export PATH=/opt/puppetlabs/puppet/bin:$PATH
for g in json aws-sdk-core aws-sdk-secretsmanager
do
  gem install "$g"
done

pro auto-attach || true
pro enable esm-infra esm-apps || true

apt-get -y autoremove --purge
apt-get clean
rm -rf /var/lib/apt/lists/*

cloud-init clean --logs
: > /etc/machine-id || true
rm -f /var/lib/dbus/machine-id || true
rm -f /home/ubuntu/.bash_history || true
truncate -s0 /var/log/syslog 2>/dev/null || true
truncate -s0 /var/log/cloud-init.log /var/log/cloud-init-output.log 2>/dev/null || true
journalctl --rotate 2>/dev/null || true
journalctl --vacuum-time=1s 2>/dev/null || true
