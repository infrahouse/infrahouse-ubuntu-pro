#!/usr/bin/env bash

set -eux
set -o pipefail

export DEBIAN_FRONTEND=noninteractive
declare -A fingerprints=(
  [noble]="A627 B776 0019 0BA5 1B90  3453 D37A 181B 689A D619"
)

cleanup_logs() {
    cloud-init clean --logs
    truncate -s0 /var/log/syslog 2>/dev/null || true
    truncate -s0 /var/log/cloud-init.log /var/log/cloud-init-output.log 2>/dev/null || true
    journalctl --rotate 2>/dev/null || true
    journalctl --vacuum-time=1s 2>/dev/null || true
}

cleanup_system_ids() {
    : > /etc/machine-id || true
    rm -f /var/lib/dbus/machine-id || true
    rm -f /home/ubuntu/.bash_history || true
}

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
EXPECTED_FINGERPRINT="${fingerprints[$UBUNTU_CODENAME]}"
GPG_KEY="$(curl --fail --silent --show-error --location --retry 5 --connect-timeout 10 --max-time 30 \
  "${REPO_URL}DEB-GPG-KEY-release-${UBUNTU_CODENAME}.infrahouse.com")"
echo "$GPG_KEY" | gpg --show-keys --fingerprint | grep -q "$EXPECTED_FINGERPRINT" || exit 1
echo "$GPG_KEY" | gpg --dearmor > "${tmpkey}"
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

pro auto-attach
pro enable esm-infra esm-apps

apt-get -y autoremove --purge
apt-get clean
rm -rf /var/lib/apt/lists/*

cleanup_logs
cleanup_system_ids
