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
            ".",
        ]

        # 4) Run packer (inherits your current env/AWS creds)
        subprocess.run(cmd, check=True)
    finally:
        # 5) Clean up the private key file
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
        Overwrite=True
    )

def get_latest_ubuntu_ami(codename, product="pro-server"):
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(
        Name=f"/aws/service/canonical/ubuntu/{product}/{codename}/stable/current/amd64/hvm/ebs-gp3/ami-id",
    )
    return resp["Parameter"]["Value"]


def main():
    ubuntu_codename = os.environ.get("UBUNTU_CODENAME")
    latest_ubuntu_ami = get_latest_ubuntu_ami(ubuntu_codename)
    if get_last_base(ubuntu_codename) != latest_ubuntu_ami:
        build(ubuntu_codename)
        set_last_base(ubuntu_codename, latest_ubuntu_ami)


if __name__ == "__main__":
    main()
