// packer.pkr.hcl
packer {
  required_version = ">= 1.10.0"
  required_plugins {
    amazon = {
      source  = "github.com/hashicorp/amazon"
      version = ">= 1.3.0"
    }
  }
}

variable "region" {
  type = string
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "ubuntu_codename" {
  type = string
}

variable "ssh_keypair_name" {
  type = string
}

variable "ssh_private_key_file" {
  type = string
}

variable "subnet_id" {
  type = string
}

variable "security_group_id" {
  type = string
}
// Publication controls:
// - To make PUBLIC set: ami_groups = ["all"]
// - To share to specific accounts: put account IDs in ami_users and snapshot_users.
variable "ami_groups" {
  type    = list(string)
  default = []
}
variable "ami_users" {
  type    = list(string)
  default = []
}

variable "ami_regions" {
  type    = list(string)
  default = []
}

source "amazon-ebs" "ubuntu_pro" {
  region           = var.region
  instance_type    = var.instance_type
  ssh_username     = "ubuntu"
  ssh_keypair_name = var.ssh_keypair_name
  subnet_id        = var.subnet_id
  security_group_ids = [
    var.security_group_id
  ]
  # ssh_agent_auth   = true
  ssh_private_key_file = var.ssh_private_key_file
  source_ami_filter {
    filters = {
      name                = "ubuntu-pro-server/images/hvm-ssd-gp3/ubuntu-${var.ubuntu_codename}-*"
      virtualization-type = "hvm"
      architecture        = "x86_64"
    }
    owners      = ["099720109477"] // Canonical
    most_recent = true
  }

  ami_name        = "infrahouse-ubuntu-pro-${var.ubuntu_codename}-{{timestamp}}"
  ami_description = "Ubuntu Pro ${var.ubuntu_codename} with InfraHouse packages"
  ena_support     = true
  ebs_optimized   = true

  // Publish
  ami_groups     = var.ami_groups
  ami_users      = var.ami_users
  snapshot_users = var.ami_users
  ami_regions    = var.ami_regions

  // created_by belongs here, not only in run_tags. run_tags reaches the build
  // instance and its volume, so the build-region snapshot inherits the tag,
  // but the snapshots packer creates when copying to ami_regions inherit this
  // block instead. Without it those copies carry no created_by, and the role's
  // ec2:DeleteSnapshot grant is conditioned on exactly that tag -- so
  // ami_retention.py can deregister a copied AMI and then be refused
  // permission to delete the snapshot behind it. That is what orphaned
  // snap-0d5770efda2ff3d58 in us-east-1 during run 33904005088.
  tags = {
    Name            = "infrahouse-ubuntu-pro-${var.ubuntu_codename}-{{timestamp}}"
    base            = "Ubuntu Pro ${var.ubuntu_codename}"
    created_by      = "infrahouse-ubuntu-pro"
    maintainer      = "infrahouse"
    ubuntu_codename = var.ubuntu_codename
  }
  run_tags = {
    created_by = "infrahouse-ubuntu-pro"
  }
}

build {
  name    = "infrahouse-ubuntu-pro"
  sources = ["source.amazon-ebs.ubuntu_pro"]

  // Base prep + repo add
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E {{ .Path }}"
    script          = "provision.sh"
    pause_before    = "30s"
    // No max_retries on purpose. A provisioner-level retry re-runs provision.sh
    // from the top -- minutes of work to recover from one flaky mirror, with no
    // indication of which step was flaky, and it requires every step to be
    // idempotent (`pro enable` is not). provision.sh retries the individual
    // network calls instead; see with_retry there.
  }

  post-processor "manifest" {
    output     = "manifest.json"
    strip_path = true
  }
}
