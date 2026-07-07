from pathlib import Path

from scripts.check_supply_chain_pins import _check_action_refs, _check_container_refs

PINNED_SHA = "9c091bb21b7c1c1d1991bb908d89e4e9dddfe3e0"


def _write_workflow(root: Path, text: str) -> None:
    workflow_dir = root / ".github" / "workflows"
    workflow_dir.mkdir(parents=True)
    (workflow_dir / "ci.yml").write_text(text, encoding="utf-8")


def test_action_version_tag_is_not_pinned(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "steps:\n  - uses: actions/checkout@v7.0.0\n")

    errors = _check_action_refs(tmp_path)

    assert errors == [".github/workflows/ci.yml uses actions/checkout@v7.0.0; pin actions to a full commit SHA"]


def test_action_full_commit_sha_is_pinned(tmp_path: Path) -> None:
    _write_workflow(tmp_path, f"steps:\n  - uses: actions/checkout@{PINNED_SHA} # v7.0.0\n")

    assert _check_action_refs(tmp_path) == []


def test_workflow_docker_action_without_digest_is_not_pinned(tmp_path: Path) -> None:
    _write_workflow(tmp_path, "steps:\n  - uses: docker://python:3.14-slim\n")

    errors = _check_action_refs(tmp_path)

    assert errors == [
        ".github/workflows/ci.yml uses docker://python:3.14-slim; pin container images to an immutable sha256 digest"
    ]


def test_workflow_docker_action_with_digest_is_pinned(tmp_path: Path) -> None:
    _write_workflow(
        tmp_path,
        "steps:\n"
        "  - uses: docker://python:3.14-slim@"
        "sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1\n",
    )

    assert _check_action_refs(tmp_path) == []


def test_container_tag_without_digest_is_not_pinned(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "Dockerfile").write_text("FROM python:3.14-slim\n", encoding="utf-8")

    errors = _check_container_refs(tmp_path)

    assert errors == ["deploy/Dockerfile uses python:3.14-slim; pin container images to an immutable sha256 digest"]


def test_container_tag_with_digest_is_pinned(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "Dockerfile").write_text(
        "FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1\n",
        encoding="utf-8",
    )

    assert _check_container_refs(tmp_path) == []


def test_local_image_and_copy_stage_alias_are_allowed(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "Dockerfile").write_text(
        "FROM python:3.14-slim@sha256:b877e50bd90de10af8d82c57a022fc2e0dc731c5320d762a27986facfc3355c1 AS builder\n"
        "COPY --from=builder /app /app\n",
        encoding="utf-8",
    )
    (deploy_dir / "docker-compose.yml").write_text(
        "services:\n  app:\n    image: mcp-telegram:local\n", encoding="utf-8"
    )

    assert _check_container_refs(tmp_path) == []


def test_copy_from_undeclared_bare_image_is_not_pinned(tmp_path: Path) -> None:
    deploy_dir = tmp_path / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "Dockerfile").write_text(
        "FROM scratch AS runtime\nCOPY --from=alpine /bin/busybox /bin/busybox\nCOPY --from=0 /app /app\n",
        encoding="utf-8",
    )

    errors = _check_container_refs(tmp_path)

    assert errors == ["deploy/Dockerfile uses alpine; pin container images to an immutable sha256 digest"]
