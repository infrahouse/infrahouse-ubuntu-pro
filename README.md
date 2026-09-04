# infrahouse-ubuntu-pro

[![Need Help?](https://img.shields.io/badge/Need%20Help%3F-Contact%20Us-0066CC)](https://infrahouse.com/contact)
[![AWS EC2](https://img.shields.io/badge/AWS-EC2-orange?logo=amazonec2)](https://aws.amazon.com/ec2/)
[![Ubuntu Pro](https://img.shields.io/badge/Ubuntu-Pro-E95420?logo=ubuntu)](https://ubuntu.com/pro)
[![Packer](https://img.shields.io/badge/Packer-02A8EF?logo=packer)](https://www.packer.io/)

Builds customized Ubuntu Pro AMIs for AWS with pre-installed InfraHouse tools and packages.
AMIs are rebuilt automatically every 6 hours to incorporate the latest Ubuntu Pro security updates.

## Features

* Based on the latest Canonical Ubuntu Pro base image (x86_64, HVM, EBS gp3)
* Pre-configured [InfraHouse APT repository](https://infrahouse.com) with GPG key verification
* Includes [infrahouse-toolkit](https://pypi.org/project/infrahouse-toolkit/) and common packages
* Ubuntu Pro features enabled: ESM Infra, ESM Apps
* Automated builds via GitHub Actions on a 6-hour schedule
* Incremental builds - only rebuilds when Canonical publishes a new base AMI
* Manual trigger with force-rebuild option
* Published as public AMIs (built in us-west-1, copied to configurable regions)
* Age-based AMI retention - unpublished at 30 days, deregistered at 90, never count-based

## Installed Packages

The AMI includes the following on top of the Ubuntu Pro base:

* **System**: awscli, build-essential, jq, net-tools, sysstat
* **Python**: python3, python3-pip, python3-virtualenv, python-is-python3
* **Ruby**: ruby-dev, ruby-rubygems, plus gems (json, aws-sdk-core, aws-sdk-secretsmanager)
* **InfraHouse**: infrahouse-toolkit (from InfraHouse APT repo)

## Architecture

![Architecture](docs/assets/architecture.svg)

### Build Flow

1. **GitHub Actions** triggers on schedule (every 6 hours) or manual dispatch
2. **ami_retention.py** expires old AMIs in every published region, before the build
   - Frees public-AMI quota slots the build is about to need
3. **packer-build.py** checks if Canonical has published a new Ubuntu Pro base AMI
   - Compares the current base AMI ID (from SSM) with the latest Canonical AMI
   - Skips the build if unchanged (unless force-rebuild is set)
4. **Packer** launches an EC2 instance from the latest Ubuntu Pro base
5. **provision.sh** runs inside the instance:
   - Upgrades all system packages
   - Adds the InfraHouse APT repository with GPG key fingerprint verification
   - Installs required packages and Ruby gems
   - Enables Ubuntu Pro ESM features
   - Cleans up logs and system IDs for AMI optimization
6. Packer creates the AMI and publishes it publicly
7. The new base AMI ID is saved to SSM for future comparison

### AMI Retention

Retention is keyed on age, never on how many AMIs exist. A count-based policy deletes recent images
exactly when churn is highest, which is when they matter most, so this policy would rather leave the
account over its quota - and say so - than deregister something a few days old.

| Age | What happens | Why |
|-----|--------------|-----|
| 0-30 days | Public | Consumers can resolve and pin the AMI ID |
| 30 days | Launch permission removed | Frees a public-AMI quota slot; still launchable by this account |
| 90 days | Deregistered, snapshots deleted | Rollback depth expires |

Two images are exempt at any age: whatever `/infrahouse/ubuntu-pro/latest/{codename}` advertises, and the
newest one built. A quiet spell can never leave a region with no image, or the parameter pointing at a
deregistered one.

The newest is exempt independently of SSM because that parameter does not mean the same thing in every
region. In the copy regions `store_regional_ami_ids()` writes the AMI packer copied there; in the build
region `set_last_base()` runs afterwards and writes Canonical's *base* AMI to the same name - the rebuild
trigger's memory, working as designed, and not an ID this module will ever match. Keyed on SSM alone the
exemption would be inert in exactly the region the build runs in.

The public window is what governs the per-region **Public AMIs** quota (default 5, adjustable):
`public window x AMIs published per day <= quota`. Publication runs at ~0.1 AMIs/day (measured from the
images: 20 spanning 211 days), so a 30-day window settles at about 5 public AMIs against the current
quota of 20.

Size this from the AMIs, not from workflow runs. The build runs every 6 hours and mostly decides not to
build, and failed runs produce no image at all - counting runs overstates the rate roughly tenfold.

## AWS Integration

| Resource | Details |
|----------|---------|
| Region | us-west-1 |
| Authentication | OIDC (GitHub Actions) |
| SSM: `/infrahouse/ubuntu-pro/args` | Build configuration - SSH private key, VPC details, ami_regions (SecureString) |
| SSM: `/infrahouse/ubuntu-pro/latest/{codename}` | Last built base AMI ID |
| SSM: `/aws/service/canonical/ubuntu/...` | Canonical's published AMI IDs |

## Supported Ubuntu Releases

| Codename | Version |
|----------|---------|
| noble | 24.04 LTS |

## Key Files

| File | Purpose |
|------|---------|
| `packer.pkr.hcl` | Packer build definition - source AMI filter, instance config, output AMI |
| `packer-build.py` | Python orchestration - SSM parameters, SSH key handling, incremental builds |
| `ami_retention.py` | Age-based AMI retention - unpublish, then deregister with snapshots |
| `provision.sh` | Bash provisioning - package installation, repo setup, Ubuntu Pro enablement |
| `.github/workflows/packer.yml` | GitHub Actions workflow - scheduled and manual triggers |
| `tests/` | pytest suite for the retention policy |

## Manual Build

Requires AWS credentials and SSM parameters to be configured.

```bash
# Set the target Ubuntu release
export UBUNTU_CODENAME=noble

# Run the orchestration script (checks for new base AMI, builds if needed)
python packer-build.py

# Force a rebuild regardless of base AMI changes
FORCE_REBUILD=true python packer-build.py

# Report what retention would remove, without removing it
RETENTION_DRY_RUN=true python ami_retention.py

# Apply retention
python ami_retention.py
```

To run Packer directly:

```bash
packer init .
packer build \
    -var 'region=us-west-1' \
    -var 'ubuntu_codename=noble' \
    -var 'ssh_keypair_name=your-key' \
    -var 'ssh_private_key_file=/path/to/key.pem' \
    -var 'subnet_id=subnet-xxx' \
    -var 'security_group_id=sg-xxx' \
    .
```

## Requirements

* [Packer](https://www.packer.io/) >= 1.10.0
* [Packer Amazon plugin](https://github.com/hashicorp/packer-plugin-amazon) >= 1.3.0
* Python 3 with [boto3](https://pypi.org/project/boto3/)
* AWS credentials with permissions for EC2, SSM, and AMI management
  (retention additionally needs `ec2:DeregisterImage` and `ec2:DeleteSnapshot`)
* [pytest](https://pytest.org/) to run the test suite: `pytest`
