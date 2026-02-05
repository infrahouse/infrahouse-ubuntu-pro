# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## First Steps

**Your first tool call in this repository MUST be reading .claude/CODING_STANDARD.md.
Do not read any other files, search, or take any actions until you have read it.**
This contains InfraHouse's comprehensive coding standards for Terraform, Python, and general formatting rules.

## Project Overview

This repository builds customized Ubuntu Pro AMIs for AWS with pre-installed InfraHouse tools. 
Builds run automatically every 6 hours via GitHub Actions to incorporate the latest Ubuntu Pro security updates.

## Architecture

The build process:
1. **GitHub Actions** (`.github/workflows/packer.yml`) triggers on schedule or manual dispatch
2. **packer-build.py** orchestrates the build - retrieves SSH keys and parameters from AWS SSM Parameter Store, invokes Packer, stores the resulting AMI ID back in SSM
3. **packer.pkr.hcl** defines the Packer build configuration - filters for latest Canonical Ubuntu Pro base image
4. **provision.sh** runs inside the instance to install packages, configure the InfraHouse APT repository, enable Ubuntu Pro features, and clean up for AMI optimization

## Key Files

| File | Purpose |
|------|---------|
| `packer.pkr.hcl` | Packer build definition (HCL) - source AMI filter, instance type, output AMI config |
| `packer-build.py` | Python orchestration - SSM parameter handling, SSH key management, Packer invocation |
| `provision.sh` | Bash provisioning - package installation, repo setup, Ubuntu Pro enablement |

## Build Commands

```bash
# Full build (requires AWS credentials and SSM parameters)
python packer-build.py

# Manual Packer build
packer init .
packer build -var 'region=us-west-1' \
             -var 'ubuntu_codename=noble' \
             -var 'ssh_keypair_name=your-key' \
             -var 'ssh_private_key_file=/path/to/key.pem' \
             -var 'subnet_id=subnet-xxx' \
             -var 'security_group_id=sg-xxx' \
             .
```

## AWS Integration

- **Region**: us-west-1 (default)
- **Authentication**: OIDC for GitHub Actions
- **SSM Parameters**:
  - `/infrahouse/ubuntu-pro/args` - Build configuration (SecureString)
  - `/infrahouse/ubuntu-pro/latest/{codename}` - Last built AMI ID
