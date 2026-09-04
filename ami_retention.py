#!/usr/bin/env python3
"""Age-based retention for the AMIs this repository publishes.

Retention is keyed on the AMI's age, never on how many AMIs exist. A count-based
policy deletes recent images exactly when churn is highest -- which is when the
recent images matter most -- so this module would rather leave the account over
its quota, and say so, than deregister something three days old.

Two stages:

* ``PUBLIC_DAYS`` -- the AMI stops being public. It stays registered and
  launchable by this account.
* ``DELETE_DAYS`` -- the AMI is deregistered and its snapshots are deleted.

Splitting them decouples the quota from the retention window. The "Public AMIs"
quota counts only public images, so unpublishing frees a slot immediately while
the image itself survives for rollback at the cost of snapshot storage.

The AMI that SSM currently advertises is exempt from both stages at any age.
That is referential integrity, not retention: it is what consumers resolve right
now, and expiring it would leave the pointer dangling after a quiet spell in
which nothing new was built.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import boto3

# Days an AMI stays public. This is a contract with consumers -- an AMI id pinned
# in someone else's launch template keeps working for this long -- and it is also
# what governs the per-region "Public AMIs" quota:
#
#     public window (days) x AMIs published per day <= quota
#
# At the ~1.3 AMIs/day observed in Sep 2026, 30 days needs a quota of about 40.
# The quota defaults to 5 and is adjustable; this account was at 20 in us-west-1
# when this was written, which is where build 33890534131 died with
# ResourceLimitExceeded. Until the increase lands, publication keeps failing --
# shortening this window to paper over that would be the count-based policy
# wearing a disguise.
#
# The quota counts public AMIs *including those in the Recycle Bin*. Unpublishing
# frees the slot outright; deregistering alone would not, if a Recycle Bin rule
# covers images.
PUBLIC_DAYS = 30

# Days before the AMI is deregistered and its snapshots deleted. Everything
# between PUBLIC_DAYS and DELETE_DAYS is private and still launchable by this
# account: rollback depth that costs only snapshot storage.
DELETE_DAYS = 90

# Both filters are required before anything destructive runs. The name pattern
# alone would match a hand-built image someone named to match; the tag alone
# would match every InfraHouse AMI in the account.
NAME_PATTERN = "infrahouse-ubuntu-pro-{codename}-*"
MAINTAINER_TAG = "infrahouse"


def image_age(image: Dict, now: datetime) -> timedelta:
    """Age of *image* at *now*.

    :param image: One ``Images`` entry as returned by ``DescribeImages``.
    :param now: Reference time. Must be timezone-aware.
    :return: How long ago the image was created.
    """
    created = datetime.fromisoformat(image["CreationDate"].replace("Z", "+00:00"))
    return now - created


def find_build_images(ec2, codename: str) -> List[Dict]:
    """AMIs this repository built for *codename*, in the client's region.

    :param ec2: An EC2 client bound to the region to search.
    :param codename: Ubuntu codename, e.g. ``noble``.
    :return: Matching images, newest first.
    """
    images: List[Dict] = []
    paginator = ec2.get_paginator("describe_images")
    for page in paginator.paginate(
        Owners=["self"],
        Filters=[
            {"Name": "name", "Values": [NAME_PATTERN.format(codename=codename)]},
            {"Name": "tag:maintainer", "Values": [MAINTAINER_TAG]},
        ],
    ):
        images += page["Images"]
    return sorted(images, key=lambda image: image["CreationDate"], reverse=True)


def count_public_images(ec2) -> int:
    """Public AMIs this account owns in the client's region.

    Deliberately not restricted to this repository's images: the quota counts
    every public AMI the account owns, so a number filtered down to ours would
    not be the number that runs out.

    :param ec2: An EC2 client bound to the region to count.
    :return: How many owned images are public.
    """
    total = 0
    paginator = ec2.get_paginator("describe_images")
    for page in paginator.paginate(Owners=["self"], ExecutableUsers=["all"]):
        total += len(page["Images"])
    return total


def advertised_image(codename: str, region: str) -> Optional[str]:
    """AMI id SSM currently advertises for *codename* in *region*.

    :param codename: Ubuntu codename, e.g. ``noble``.
    :param region: AWS region to read the parameter from.
    :return: The AMI id, or None if no build has ever published one there.
    """
    ssm = boto3.client("ssm", region_name=region)
    try:
        resp = ssm.get_parameter(Name=f"/infrahouse/ubuntu-pro/latest/{codename}")
    except ssm.exceptions.ParameterNotFound:
        return None
    return resp["Parameter"]["Value"]


def unpublish_image(ec2, image: Dict, dry_run: bool) -> None:
    """Drop the ``all`` group launch permission from *image*.

    Frees a quota slot. The image stays registered and this account can still
    launch it.

    :param ec2: An EC2 client bound to the image's region.
    :param image: The image to unpublish.
    :param dry_run: When true, report the action and change nothing.
    """
    print(f"  unpublish {image['ImageId']} {image['Name']}")
    if dry_run:
        return
    ec2.modify_image_attribute(
        ImageId=image["ImageId"],
        LaunchPermission={"Remove": [{"Group": "all"}]},
    )


def delete_image(ec2, image: Dict, dry_run: bool) -> None:
    """Deregister *image* and delete the snapshots behind it.

    Snapshot ids are read from the image before deregistering, because the block
    device mapping is gone afterwards and the snapshots would be orphaned --
    invisible, unreferenced, and billed forever. They are deleted after, because
    a snapshot backing a registered AMI cannot be deleted.

    :param ec2: An EC2 client bound to the image's region.
    :param image: The image to remove.
    :param dry_run: When true, report the action and change nothing.
    """
    snapshots = [
        mapping["Ebs"]["SnapshotId"]
        for mapping in image.get("BlockDeviceMappings", [])
        if "SnapshotId" in mapping.get("Ebs", {})
    ]
    print(
        f"  delete {image['ImageId']} {image['Name']} snapshots={','.join(snapshots) or 'none'}"
    )
    if dry_run:
        return
    ec2.deregister_image(ImageId=image["ImageId"])
    for snapshot_id in snapshots:
        ec2.delete_snapshot(SnapshotId=snapshot_id)


def prune_region(codename: str, region: str, now: datetime, dry_run: bool) -> None:
    """Apply both retention stages to *codename*'s images in *region*.

    :param codename: Ubuntu codename, e.g. ``noble``.
    :param region: AWS region to prune.
    :param now: Reference time for every age comparison, so one run cannot
        straddle a day boundary and treat two images inconsistently.
    :param dry_run: When true, report what would happen and change nothing.
    """
    ec2 = boto3.client("ec2", region_name=region)
    advertised = advertised_image(codename, region)
    print(f"{region}: retention for {codename}, advertised {advertised or 'unset'}")

    for image in find_build_images(ec2, codename):
        if image["ImageId"] == advertised:
            continue
        age = image_age(image, now)
        if age >= timedelta(days=DELETE_DAYS):
            delete_image(ec2, image, dry_run)
        elif age >= timedelta(days=PUBLIC_DAYS) and image.get("Public"):
            unpublish_image(ec2, image, dry_run)

    print(f"{region}: {count_public_images(ec2)} public AMIs owned after retention")


def build_regions() -> List[str]:
    """Every region this repository publishes into.

    Read from the same SSM parameter the build reads, so retention cannot drift
    out of step with what packer copies to.

    :return: Build region first, then the copy regions.
    """
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name="/infrahouse/ubuntu-pro/args", WithDecryption=True)
    params = json.loads(resp["Parameter"]["Value"])
    regions = [params["region"]]
    regions += [
        region for region in params.get("ami_regions", []) if region != params["region"]
    ]
    return regions


def main() -> None:
    """Prune every published region.

    Runs before the build rather than after it. Pruning after would free slots
    that the build it just failed to publish had already needed.
    """
    codename = os.environ["UBUNTU_CODENAME"]
    dry_run = os.environ.get("RETENTION_DRY_RUN", "false").lower() == "true"
    now = datetime.now(timezone.utc)

    print(
        f"retention: public at {PUBLIC_DAYS}d, delete at {DELETE_DAYS}d"
        + (" (dry run)" if dry_run else "")
    )
    for region in build_regions():
        prune_region(codename, region, now, dry_run)


if __name__ == "__main__":
    main()
