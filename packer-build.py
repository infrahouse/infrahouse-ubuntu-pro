#!/usr/bin/env python3
import json
import os
import stat
import subprocess
import tempfile

import boto3

# Metapackage provision.sh's `apt-get -y upgrade` would pull the kernel from. It
# tracks whichever HWE line is current, so its version moves across series
# (6.17.x -> 7.0.x) as well as within one.
KERNEL_PACKAGE = "linux-image-aws"

def build(codename):
    ssm = boto3.client("ssm")

    # 1) Read JSON args from SSM Parameter Store (SecureString)
    resp = ssm.get_parameter(
        Name="/infrahouse/ubuntu-pro/args",
        WithDecryption=True,
    )
    params = json.loads(resp["Parameter"]["Value"])
    ami_regions = params.get("ami_regions", [])

    # 2) Write SSH private key to a secure temp file
    key_fd, key_path = tempfile.mkstemp(prefix="packer_key_", suffix=".pem")
    try:
        with os.fdopen(key_fd, "w") as f:
            f.write(params["ssh_private_key"])
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

        # 3) Build the packer command
        cmd = [
            "packer", "build",
            "-var", f"region={params['region']}",
            "-var", f"ssh_keypair_name={params['ssh_keypair_name']}",
            "-var", f"security_group_id={params['security_group_id']}",
            "-var", f"subnet_id={params['subnet_id']}",
            "-var", f"ssh_private_key_file={key_path}",
            "-var", f"ubuntu_codename={codename}",
            "-var", 'ami_groups=["all"]',
        ]
        if ami_regions:
            cmd += ["-var", f"ami_regions={json.dumps(ami_regions)}"]
        cmd.append(".")

        # 4) Run packer (inherits your current env/AWS creds)
        subprocess.run(cmd, check=True)

        # 5) Store per-region AMI IDs in SSM
        store_regional_ami_ids(codename, params["region"], ami_regions)
    finally:
        # 6) Clean up the private key file
        try:
            os.remove(key_path)
        except FileNotFoundError:
            pass

def get_last_base(codename):
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(
        Name=f"/infrahouse/ubuntu-pro/latest/{codename}",
    )
    return resp["Parameter"]["Value"]


def set_last_base(codename, ami_id):
    ssm = boto3.client("ssm")
    ssm.put_parameter(
        Name=f"/infrahouse/ubuntu-pro/latest/{codename}",
        Value=ami_id,
        Type="String",
        Overwrite=True,
    )


def store_regional_ami_ids(codename, build_region, ami_regions):
    """Read the Packer manifest and store each region's AMI ID in that region's SSM."""
    manifest_path = "manifest.json"
    if not os.path.exists(manifest_path):
        return

    with open(manifest_path) as f:
        manifest = json.load(f)

    last_build = manifest["builds"][-1]
    artifact_id = last_build["artifact_id"]

    # artifact_id format: "us-west-1:ami-abc123,us-east-1:ami-def456,..."
    region_ami_map = {}
    for pair in artifact_id.split(","):
        region, ami_id = pair.split(":")
        region_ami_map[region] = ami_id

    # Write AMI ID to SSM in each region (build region + copy regions)
    all_regions = [build_region] + [r for r in ami_regions if r != build_region]
    for region in all_regions:
        ami_id = region_ami_map.get(region)
        if ami_id:
            ssm = boto3.client("ssm", region_name=region)
            ssm.put_parameter(
                Name=f"/infrahouse/ubuntu-pro/latest/{codename}",
                Value=ami_id,
                Type="String",
                Overwrite=True,
            )

def get_candidate_kernel(codename):
    """Version of KERNEL_PACKAGE apt would install for *codename* right now.

    Asked of a container of the target release rather than derived from
    Launchpad: the metapackage points at whichever HWE line is current, and that
    mapping is not recoverable from the source-package version. Asking apt
    answers the question that actually matters -- what provision.sh's
    `apt-get -y upgrade` would install if we built this minute.

    Returns None when the lookup fails. Callers treat that as "no signal", never
    as a reason to build, so a flaky network cannot trigger a rebuild loop.
    """
    script = (
        "set -e; "
        "apt-get update -qq >/dev/null 2>&1; "
        f"apt-cache policy {KERNEL_PACKAGE} | awk '/Candidate:/ {{print $2}}'"
    )
    try:
        result = subprocess.run(
            # --platform is pinned because the answer is architecture-specific and
            # packer.pkr.hcl builds x86_64. Runner architecture must not change it.
            [
                "docker", "run", "--rm", "--platform", "linux/amd64",
                f"ubuntu:{codename}", "bash", "-c", script,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except (subprocess.SubprocessError, OSError) as err:
        print(f"kernel check: could not read {KERNEL_PACKAGE} candidate: {err}")
        return None

    version = result.stdout.strip()
    if not version or version == "(none)":
        print(f"kernel check: no candidate for {KERNEL_PACKAGE} on {codename}")
        return None
    return version


def get_last_kernel(codename):
    """Kernel candidate recorded at the last successful build, or None if unset."""
    ssm = boto3.client("ssm")
    try:
        resp = ssm.get_parameter(Name=f"/infrahouse/ubuntu-pro/kernel/{codename}")
    except ssm.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"]


def set_last_kernel(codename, version):
    ssm = boto3.client("ssm")
    ssm.put_parameter(
        Name=f"/infrahouse/ubuntu-pro/kernel/{codename}",
        Value=version,
        Type="String",
        Overwrite=True,
    )


def get_latest_ubuntu_ami(codename, product="pro-server"):
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(
        Name=f"/aws/service/canonical/ubuntu/{product}/{codename}/stable/current/amd64/hvm/ebs-gp3/ami-id",
    )
    return resp["Parameter"]["Value"]


def main():
    """Rebuild when the base AMI moves, or when a newer kernel is available.

    The base-AMI trigger alone leaves a structural gap: Canonical publishes Pro
    AMIs less often than it publishes kernels (measured on noble/linux-aws: a
    kernel every ~15-19 days, occasionally 2 days apart for out-of-band security
    fixes). Between those, a fresh instance boots the AMI's kernel, installs the
    newer one during provisioning, and goes on running the old one -- an
    instance is not rebooted to activate it. Rebuilding on the kernel trigger
    means new instances boot the fixed kernel and that gap never opens.
    """
    ubuntu_codename = os.environ.get("UBUNTU_CODENAME")
    force_rebuild = os.environ.get("FORCE_REBUILD", "false").lower() == "true"

    latest_ubuntu_ami = get_latest_ubuntu_ami(ubuntu_codename)
    base_changed = get_last_base(ubuntu_codename) != latest_ubuntu_ami

    candidate_kernel = get_candidate_kernel(ubuntu_codename)
    last_kernel = get_last_kernel(ubuntu_codename)
    # Both must be known: an unreadable candidate is no signal, and an unset
    # parameter means this codename has no baseline yet -- neither is a reason
    # to build.
    kernel_changed = (
        candidate_kernel is not None
        and last_kernel is not None
        and candidate_kernel != last_kernel
    )

    reasons = []
    if force_rebuild:
        reasons.append("forced")
    if base_changed:
        reasons.append(f"base AMI -> {latest_ubuntu_ami}")
    if kernel_changed:
        reasons.append(f"{KERNEL_PACKAGE} {last_kernel} -> {candidate_kernel}")

    if not reasons:
        print(
            f"{ubuntu_codename}: no rebuild "
            f"(base {latest_ubuntu_ami}, kernel {candidate_kernel})"
        )
        # Seed the baseline on first run so the next one has something to compare.
        if last_kernel is None and candidate_kernel is not None:
            set_last_kernel(ubuntu_codename, candidate_kernel)
        return

    print(f"{ubuntu_codename}: rebuilding -- " + "; ".join(reasons))
    build(ubuntu_codename)
    set_last_base(ubuntu_codename, latest_ubuntu_ami)
    # Recorded after the build, so a failed build is retried on the next run
    # rather than being marked done.
    if candidate_kernel is not None:
        set_last_kernel(ubuntu_codename, candidate_kernel)


if __name__ == "__main__":
    main()
