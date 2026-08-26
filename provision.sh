#!/usr/bin/env bash

set -eux
set -o pipefail

export DEBIAN_FRONTEND=noninteractive

configure_apt_lock_timeout() {
    # Puppet's Package provider, cloud-init and the AWS agents (guardduty,
    # inspector) all shell out to apt-get without options, so a global drop-in
    # is the only place that makes them wait for the dpkg lock instead of
    # failing outright when something else holds it.
    echo 'DPkg::Lock::Timeout "300";' > /etc/apt/apt.conf.d/99-lock-timeout
}

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

verify_kernel_current() {
    # The image must not ship a kernel older than the archive offers.
    #
    # Guards a specific trap: `apt-get upgrade` never installs *new* packages. A
    # kernel metapackage bump that crosses a series (6.17.x -> 7.0.x) needs a
    # brand-new linux-image-<version>-aws binary, so apt silently keeps it back
    # and the build still succeeds. The AMI then ships a stale kernel, every
    # instance installs the newer one at boot via unattended-upgrade, and none of
    # them ever runs it -- instances here are replaced, not rebooted. Nothing
    # about that is visible without this check: `uname -r` on a fresh instance
    # disagrees with `dpkg -l`, and /var/run/reboot-required is not written.
    #
    # Deliberately compares against the candidate rather than asserting a
    # specific apt flag, so it also catches a pinned kernel, a held package, a
    # stale mirror, or any future cause.
    #
    # Must run while /var/lib/apt/lists is still populated -- apt-cache needs it.
    local metapackage installed candidate
    metapackage="linux-image-aws"
    installed=$(dpkg-query -W -f='${Version}' "${metapackage}")
    candidate=$(apt-cache policy "${metapackage}" | awk '/Candidate:/ {print $2}')

    if [ "${installed}" != "${candidate}" ]; then
        echo "FATAL: ${metapackage} is ${installed}, archive offers ${candidate}." >&2
        echo "The upgrade step did not take it, so this AMI would ship a stale kernel." >&2
        echo "A series change needs --with-new-pkgs (or dist-upgrade); plain" >&2
        echo "'apt-get upgrade' keeps it back. apt says:" >&2
        apt-get -s upgrade 2>/dev/null | sed -n '/kept back/,+2p' >&2 || true
        return 1
    fi
    echo "kernel check: ${metapackage} ${installed} matches the archive candidate"
}

cleanup_timer_stamps() {
    # Persistent=true timers record their last trigger here. If these survive
    # into the snapshot, every launched instance sees a last-trigger of AMI
    # build time and systemd fires a catch-up run shortly after boot -- for
    # apt-daily-upgrade that means an unattended-upgrade holding the dpkg lock
    # during provisioning. Stock Ubuntu ships this directory empty and systemd
    # recreates it on first boot, so there is nothing to restore.
    # Timers are stopped first so none of them can re-stamp in the window
    # between this script exiting and packer powering the instance off.
    systemctl stop '*.timer' 2>/dev/null || true
    rm -rf /var/lib/systemd/timers/ || true
}

configure_apt_lock_timeout

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
REPO_LIST="/etc/apt/sources.list.d/50-infrahouse.list"


install -d -m 0755 "${KEYRING_DIR}"
tmpkey="$(mktemp)"
curl --fail --silent --show-error --location --retry 5 --connect-timeout 10 --max-time 30 \
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
  nfs-common \
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

verify_kernel_current

apt-get -y autoremove --purge
apt-get clean
rm -rf /var/lib/apt/lists/*

cleanup_logs
cleanup_system_ids
cleanup_timer_stamps
