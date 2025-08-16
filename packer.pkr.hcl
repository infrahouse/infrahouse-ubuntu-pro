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
  type    = string
}

variable "instance_type" {
  type    = string
  default = "t3.small"
}

variable "ubuntu_codename" {
  type    = string
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
  type = list(string)
  default = []
}
variable "ami_users" {
  type = list(string)
  default = []
}
// AWS account IDs

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
    owners = ["099720109477"] // Canonical
    most_recent = true
  }

  ami_name        = "infrahouse-ubuntu-pro-${var.ubuntu_codename}-{{timestamp}}"
  ami_description = "Ubuntu Pro ${var.ubuntu_codename} with InfraHouse packages"
  ena_support     = true
  ebs_optimized = true

  // Publish
  ami_groups     = var.ami_groups
  ami_users      = var.ami_users
  snapshot_users = var.ami_users

  tags = {
    Name       = "infrahouse-ubuntu-pro-${var.ubuntu_codename}-{{timestamp}}"
    base       = "Ubuntu Pro ${var.ubuntu_codename}"
    maintainer = "InfraHouse"
  }
  run_tags = {
    created_by = "infrahouse-ubuntu-pro"
  }
}

build {
  name = "infrahouse-ubuntu-pro"
  sources = ["source.amazon-ebs.ubuntu_pro"]

  // Base prep + repo add
  provisioner "shell" {
    execute_command = "chmod +x {{ .Path }}; {{ .Vars }} sudo -E {{ .Path }}"
    script          = "provision.sh"
    pause_before    = "30s"
    max_retries     = 3
  }
}
