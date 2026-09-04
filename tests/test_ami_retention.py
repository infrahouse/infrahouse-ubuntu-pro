"""Tests for the AMI retention policy.

The interesting cases here are the ones where nothing should happen. This module
deregisters images and deletes snapshots, so a test that proves an image was
*left alone* is worth more than one that proves it was removed.
"""

from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pytest
from botocore.exceptions import ClientError

import ami_retention

NOW = datetime(2026, 9, 4, 12, 0, 0, tzinfo=timezone.utc)


def make_image(
    image_id: str,
    age_days: float,
    public: bool = True,
    snapshots: Optional[List[str]] = None,
) -> Dict:
    """Build a DescribeImages entry *age_days* old.

    :param image_id: AMI id to report.
    :param age_days: How long before NOW the image was created.
    :param public: Whether the image carries the ``all`` group launch permission.
    :param snapshots: Snapshot ids behind the image. Defaults to one.
    :return: An image dict shaped like the EC2 API returns.
    """
    created = NOW - timedelta(days=age_days)
    if snapshots is None:
        snapshots = [f"snap-{image_id[4:]}"]
    return {
        "ImageId": image_id,
        "Name": f"infrahouse-ubuntu-pro-noble-{int(created.timestamp())}",
        "CreationDate": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "Public": public,
        "BlockDeviceMappings": [
            {"Ebs": {"SnapshotId": snapshot}} for snapshot in snapshots
        ],
    }


def with_current(*images: Dict) -> List[Dict]:
    """Put *images* in a region that also holds a fresh build.

    The newest image is exempt by design, so a lone old image is exempt too. Any
    test expecting an image to be acted on has to say that a current build
    exists, or it passes for the wrong reason.

    :param images: Older images, in any order.
    :return: Those images preceded by a freshly built one.
    """
    return [make_image("ami-current-build", age_days=0), *images]


class FakePaginator:  # pylint: disable=too-few-public-methods
    """Single-page paginator over a fixed list of images."""

    def __init__(self, images: List[Dict]):
        self._images = images

    def paginate(self, **kwargs) -> List[Dict]:
        """Return every image, ignoring the filters.

        Filtering is the API's job; the tests that matter here are about which
        images the policy acts on, not about how they were found.
        """
        if kwargs.get("ExecutableUsers") == ["all"]:
            return [
                {"Images": [image for image in self._images if image.get("Public")]}
            ]
        return [{"Images": self._images}]


class FakeEC2:
    """EC2 client that records mutations instead of making them.

    DryRun probes are answered the way EC2 answers them -- by raising, with
    ``DryRunOperation`` meaning permitted -- and are never recorded as calls, so
    an assertion on ``calls`` is an assertion about real mutations only.
    """

    def __init__(self, images: List[Dict], denied: Optional[List[str]] = None):
        self.images = images
        self.calls: List[tuple] = []
        self.denied = set(denied or [])

    def get_paginator(self, name: str) -> FakePaginator:
        """Return a paginator over the canned images."""
        assert name == "describe_images"
        return FakePaginator(self.images)

    def modify_image_attribute(self, **kwargs) -> None:
        """Record an unpublish, or answer a permission probe."""
        if kwargs.pop("DryRun", False):
            self._probe("modify_image_attribute")
        self.calls.append(("unpublish", kwargs["ImageId"]))

    def deregister_image(self, **kwargs) -> None:
        """Record a deregister."""
        self.calls.append(("deregister", kwargs["ImageId"]))

    def delete_snapshot(self, **kwargs) -> None:
        """Record a snapshot delete, or answer a permission probe."""
        if kwargs.pop("DryRun", False):
            self._probe("delete_snapshot")
        self.calls.append(("delete_snapshot", kwargs["SnapshotId"]))

    def _probe(self, operation: str) -> None:
        """Raise as EC2 does for a DryRun: permitted is also an exception."""
        code = (
            "UnauthorizedOperation" if operation in self.denied else "DryRunOperation"
        )
        raise ClientError({"Error": {"Code": code, "Message": operation}}, operation)


class ParameterNotFound(Exception):
    """Stand-in for the botocore-generated SSM exception."""


class FakeSSM:  # pylint: disable=too-few-public-methods
    """SSM client returning one canned parameter value."""

    class exceptions:  # pylint: disable=invalid-name,too-few-public-methods
        """Namespace matching the botocore client's ``exceptions`` attribute."""

        ParameterNotFound = ParameterNotFound

    def __init__(self, value: Optional[str]):
        self._value = value

    def get_parameter(self, **kwargs) -> Dict:
        """Return the canned value, or raise as the real client would."""
        if self._value is None:
            raise ParameterNotFound(kwargs["Name"])
        return {"Parameter": {"Value": self._value}}


@pytest.fixture(name="prune")
def prune_fixture(monkeypatch):
    """Run prune_region against fake clients and hand back the fake EC2 client."""

    def run(
        images: List[Dict],
        advertised: Optional[str] = None,
        dry_run: bool = False,
        denied: Optional[List[str]] = None,
    ) -> FakeEC2:
        ec2 = FakeEC2(images, denied)
        # Stashed before the call so a test that expects a raise can still
        # assert on what was, and was not, mutated.
        run.client = ec2
        monkeypatch.setattr(
            ami_retention.boto3,
            "client",
            lambda service, **kwargs: ec2 if service == "ec2" else FakeSSM(advertised),
        )
        ami_retention.prune_region("noble", "us-west-1", NOW, dry_run)
        return ec2

    return run


def test_image_inside_public_window_is_untouched(prune):
    """An image younger than PUBLIC_DAYS keeps its publication."""
    assert prune(with_current(make_image("ami-young", age_days=3))).calls == []


def test_image_past_public_window_is_unpublished(prune):
    """Between the two windows the image loses public access and nothing else."""
    assert prune(
        with_current(make_image("ami-mid", age_days=ami_retention.PUBLIC_DAYS + 1))
    ).calls == [("unpublish", "ami-mid")]


def test_already_private_image_is_not_unpublished_again(prune):
    """A private image in the middle window costs no API call."""
    assert (
        prune(
            with_current(
                make_image(
                    "ami-priv", age_days=ami_retention.PUBLIC_DAYS + 1, public=False
                )
            )
        ).calls
        == []
    )


def test_image_past_delete_window_is_deregistered_with_its_snapshots(prune):
    """Snapshots go after the deregister, never before, and none are left behind."""
    image = make_image(
        "ami-old",
        age_days=ami_retention.DELETE_DAYS + 1,
        snapshots=["snap-a", "snap-b"],
    )
    assert prune(with_current(image)).calls == [
        ("deregister", "ami-old"),
        ("delete_snapshot", "snap-a"),
        ("delete_snapshot", "snap-b"),
    ]


def test_advertised_image_is_exempt_at_any_age(prune):
    """The id SSM hands out is never expired, however old it is.

    A quiet spell long enough to age out every image must not leave the
    parameter pointing at a deregistered AMI.
    """
    image = make_image("ami-current", age_days=ami_retention.DELETE_DAYS * 10)
    assert prune([image], advertised="ami-current").calls == []


def test_unset_parameter_does_not_protect_an_arbitrary_image(prune):
    """A region that never published still prunes, and protects nothing."""
    image = make_image(
        "ami-orphan", age_days=ami_retention.DELETE_DAYS + 1, snapshots=["snap-o"]
    )
    assert prune(with_current(image), advertised=None).calls == [
        ("deregister", "ami-orphan"),
        ("delete_snapshot", "snap-o"),
    ]


def test_dry_run_mutates_nothing(prune):
    """Every stage reports and returns without touching the account."""
    images = [
        make_image("ami-mid", age_days=ami_retention.PUBLIC_DAYS + 1),
        make_image("ami-old", age_days=ami_retention.DELETE_DAYS + 1),
    ]
    assert prune(with_current(*images), dry_run=True).calls == []


def test_image_with_no_snapshots_is_still_deregistered(prune):
    """A mapping without an EBS snapshot must not break the delete stage."""
    image = make_image("ami-bare", age_days=ami_retention.DELETE_DAYS + 1, snapshots=[])
    assert prune(with_current(image)).calls == [("deregister", "ami-bare")]


def test_newest_image_is_exempt_when_ssm_names_something_else(prune):
    """The build region's parameter holds Canonical's base AMI, not one of ours.

    advertised_image() there returns an id find_build_images() can never match,
    so an exemption keyed on SSM alone would be inert in exactly the region the
    build runs in, and a stall past PUBLIC_DAYS would unpublish the newest image
    with nothing guarding it.
    """
    images = [
        make_image("ami-newest", age_days=ami_retention.PUBLIC_DAYS + 1),
        make_image("ami-older", age_days=ami_retention.PUBLIC_DAYS + 2),
    ]
    assert prune(images, advertised="ami-canonical-base").calls == [
        ("unpublish", "ami-older")
    ]


def test_sole_image_is_never_deleted_however_old(prune):
    """A region must not end a run holding no image at all."""
    image = make_image("ami-only", age_days=ami_retention.DELETE_DAYS * 5)
    assert prune([image], advertised=None).calls == []


def test_undeletable_snapshot_leaves_the_image_registered(prune):
    """The orphan guard: no permission to delete the snapshot, no deregister.

    DeregisterImage is the point of no return -- afterwards the block device
    mapping is gone and a snapshot nothing references is unfindable. Run
    33904005088 deregistered an AMI in us-east-1 and was then refused
    DeleteSnapshot, stranding snap-0d5770efda2ff3d58. The image must be left
    exactly as it was so the next run can retry it.
    """
    image = make_image("ami-old", age_days=ami_retention.DELETE_DAYS + 1)
    with pytest.raises(ClientError) as excinfo:
        prune(with_current(image), denied=["delete_snapshot"])
    assert excinfo.value.response["Error"]["Code"] == "UnauthorizedOperation"
    # The assertion that matters. Without the probe the deregister lands first
    # and this list holds ("deregister", "ami-old") -- the orphan, created.
    assert prune.client.calls == []


def test_undeletable_snapshot_is_caught_on_a_dry_run_too(prune):
    """A dry run has to exercise the permission, or it validates nothing.

    Skipping the probe when dry_run is set is what let run 33904005088 report a
    clean dry run and then fail for real on the same images.
    """
    image = make_image("ami-old", age_days=ami_retention.DELETE_DAYS + 1)
    with pytest.raises(ClientError):
        prune(with_current(image), dry_run=True, denied=["delete_snapshot"])
    assert prune.client.calls == []


def test_permission_probes_are_not_mutations(prune):
    """A permitted probe leaves no trace beyond the real call it precedes."""
    image = make_image("ami-old", age_days=ami_retention.DELETE_DAYS + 1)
    assert prune(with_current(image), dry_run=True).calls == []


def test_windows_leave_room_between_them():
    """The middle window must exist, or unpublishing would never happen."""
    assert 0 < ami_retention.PUBLIC_DAYS < ami_retention.DELETE_DAYS
