#!/usr/bin/env python3
import json
import os
import stat
import subprocess
import tempfile

import boto3

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

def get_latest_ubuntu_ami(codename, product="pro-server"):
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(
        Name=f"/aws/service/canonical/ubuntu/{product}/{codename}/stable/current/amd64/hvm/ebs-gp3/ami-id",
    )
    return resp["Parameter"]["Value"]


def main():
    ubuntu_codename = os.environ.get("UBUNTU_CODENAME")
    force_rebuild = os.environ.get("FORCE_REBUILD", "false").lower() == "true"
    latest_ubuntu_ami = get_latest_ubuntu_ami(ubuntu_codename)
    if force_rebuild or get_last_base(ubuntu_codename) != latest_ubuntu_ami:
        build(ubuntu_codename)
        set_last_base(ubuntu_codename, latest_ubuntu_ami)


if __name__ == "__main__":
    main()
