"""Offline safety contracts for the reviewed A2V worker's on-demand repair."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ai_video_factory.inference.salad_ltx_worker_prepare import (
    LtxWorkerPrepareError,
    ensure_a2v_image,
)

EXPECTED = "docker.io/yagobordell/ai-video-factory@sha256:" + "a" * 64
OLD = "docker.io/yagobordell/ai-video-factory@sha256:" + "b" * 64
REPO = "docker.io/yagobordell/ai-video-factory"
DSN = "postgresql://test:test@localhost:5432/test"


class FakeClient:
    def __init__(self, image=OLD, state="stopped", pending=False):
        self.group = {
            "container": {"image": image},
            "current_state": {"status": state},
            "pending_change": pending,
        }

    def describe_container_group(self):
        return self.group


@pytest.fixture
def manager(tmp_path):
    file = tmp_path / "scripts" / "salad" / "manage_salad_worker.ps1"
    file.parent.mkdir(parents=True)
    file.write_text("# mocked trusted manager\n", encoding="utf-8")
    return tmp_path


def ensure(client, manager, *, live=False, jobs=0, execute=None, **kwargs):
    return ensure_a2v_image(
        client=client,
        expected=EXPECTED,
        configured_repository=REPO,
        repo_root=manager,
        controller_dsn=DSN,
        check_controller=lambda _: SimpleNamespace(healthy=live),
        count_jobs=lambda _: jobs,
        find_executable=lambda _: "pwsh",
        execute=execute or (lambda *a, **kw: None),
        **kwargs,
    )


def test_matching_pinned_image_never_redeploys_or_contacts_postgres(manager):
    client = FakeClient(image=EXPECTED, state="running")
    ensure_a2v_image(
        client=client,
        expected=EXPECTED,
        configured_repository=REPO,
        repo_root=manager,
        controller_dsn=None,
        check_controller=lambda _: pytest.fail("Unneeded Postgres access"),
        execute=lambda *a, **kw: pytest.fail("Unneeded deployment"),
    )


def test_safe_stopped_group_prepares_exact_digest_and_rechecks(manager):
    client = FakeClient()
    calls = []

    def execute(args, *, cwd, check):
        calls.append((args, cwd, check))
        client.group["container"]["image"] = EXPECTED

    ensure(client, manager, execute=execute)
    assert len(calls) == 1
    args, cwd, check = calls[0]
    assert cwd == manager and check is True
    assert args[0] == "pwsh"
    assert args[args.index("-Action") + 1] == "Prepare"
    assert args[args.index("-Service") + 1] == "ltx25"
    assert args[args.index("-PinnedImage") + 1] == EXPECTED
    assert "-AllowControllerOverride" not in args
    assert "-NonInteractive" in args


@pytest.mark.parametrize("status,pending", [
    ("running", False), ("deploying", False), ("stopped", True),
])
def test_no_redeployment_during_active_or_changing_group(manager, status, pending):
    client = FakeClient(state=status, pending=pending)
    with pytest.raises(LtxWorkerPrepareError, match="not stably stopped"):
        ensure(
            client, manager,
            execute=lambda *a, **kw: pytest.fail("Must not deploy"),
        )


def test_controller_ownership_blocks_redeploy(manager):
    with pytest.raises(LtxWorkerPrepareError, match="Capacity Controller is active"):
        ensure(
            FakeClient(), manager, live=True,
            execute=lambda *a, **kw: pytest.fail("Must not bypass controller lock"),
        )


@pytest.mark.parametrize("jobs", [1, 3])
def test_queued_a2v_tasks_block_redeploy(manager, jobs):
    with pytest.raises(LtxWorkerPrepareError, match="unfinished A2V"):
        ensure(
            FakeClient(), manager, jobs=jobs,
            execute=lambda *a, **kw: pytest.fail("Must not risk reserved jobs"),
        )


def test_disabled_auto_prepare_never_mutates(manager):
    with pytest.raises(LtxWorkerPrepareError, match="disabled"):
        ensure(FakeClient(), manager, auto_prepare=False)


def test_invalid_repo_or_digest_does_not_deploy(manager):
    with pytest.raises(LtxWorkerPrepareError, match="different Docker repository"):
        ensure_a2v_image(
            client=FakeClient(),
            expected=OLD,
            configured_repository="docker.io/unreviewed/worker",
            repo_root=manager,
            controller_dsn=DSN,
        )


def test_unavailable_controller_safety_fails_closed(manager):
    with pytest.raises(LtxWorkerPrepareError, match="Cannot establish"):
        ensure_a2v_image(
            client=FakeClient(),
            expected=EXPECTED,
            configured_repository=REPO,
            repo_root=manager,
            controller_dsn=DSN,
            check_controller=lambda _: (_ for _ in ()).throw(OSError("DB unavailable")),
        )


def test_preparation_must_verify_real_salad_image(manager):
    with pytest.raises(LtxWorkerPrepareError, match="did not report the pinned"):
        ensure(FakeClient(), manager)


def test_missing_manager_cannot_be_substituted(manager):
    (manager / "scripts" / "salad" / "manage_salad_worker.ps1").unlink()
    with pytest.raises(LtxWorkerPrepareError, match="manager is missing"):
        ensure(FakeClient(), manager)
