import re
from pathlib import Path

FULL_COMMIT_SHA_PATTERN = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)
WORKFLOW_USES_PATTERN = re.compile(r"^\s*(?:-\s*)?uses:\s*[\"']?(?P<uses>[^\"'\s#]+)", re.MULTILINE)
FROM_PATTERN = re.compile(
    r"^\s*FROM\s+(?:--platform=[^\s]+\s+)?(?P<image>[^\s]+)(?:\s+AS\s+(?P<alias>[^\s]+))?",
    re.IGNORECASE | re.MULTILINE,
)
COPY_FROM_PATTERN = re.compile(r"^\s*COPY\s+--from=(?P<image>[^\s]+)", re.MULTILINE)
IMAGE_PATTERN = re.compile(r"^\s*image:\s*(?P<image>[^\s#]+)", re.MULTILINE)
RESERVED_IMAGES = {"scratch"}


def _workflow_files(root: Path) -> list[Path]:
    workflows = root / ".github" / "workflows"
    if not workflows.exists():
        return []
    return sorted([*workflows.glob("*.yml"), *workflows.glob("*.yaml")])


def _check_action_refs(root: Path) -> list[str]:
    errors: list[str] = []
    for path in _workflow_files(root):
        text = path.read_text(encoding="utf-8")
        for match in WORKFLOW_USES_PATTERN.finditer(text):
            uses = match.group("uses")
            source = str(path.relative_to(root))
            if uses.startswith("./"):
                continue
            if uses.startswith("docker://"):
                errors.extend(_check_image_ref(uses, source))
                continue
            if "@" not in uses:
                errors.append(f"{source} uses {uses}; pin actions to a full commit SHA")
                continue
            action, ref = uses.rsplit("@", maxsplit=1)
            if FULL_COMMIT_SHA_PATTERN.fullmatch(ref) is None:
                errors.append(f"{source} uses {action}@{ref}; pin actions to a full commit SHA")
    return errors


def _is_local_image(image: str) -> bool:
    return "/" not in image and image.endswith(":local")


def _copy_stage_aliases(text: str) -> set[str]:
    return {alias for match in FROM_PATTERN.finditer(text) if (alias := match.group("alias")) is not None}


def _is_copy_stage_alias(image: str, stage_aliases: set[str]) -> bool:
    return image in stage_aliases or image.isdecimal()


def _check_image_ref(image: str, source: str) -> list[str]:
    checked_image = image.removeprefix("docker://")
    if checked_image in RESERVED_IMAGES or _is_local_image(checked_image):
        return []
    if "@sha256:" not in checked_image:
        return [f"{source} uses {image}; pin container images to an immutable sha256 digest"]
    return []


def _dockerfiles(root: Path) -> list[Path]:
    return sorted(path for path in [root / "Dockerfile", root / "deploy" / "Dockerfile"] if path.exists())


def _compose_files(root: Path) -> list[Path]:
    return sorted(
        path for path in [root / "docker-compose.yml", root / "deploy" / "docker-compose.yml"] if path.exists()
    )


def _check_container_refs(root: Path) -> list[str]:
    errors: list[str] = []
    for dockerfile in _dockerfiles(root):
        text = dockerfile.read_text(encoding="utf-8")
        source = str(dockerfile.relative_to(root))
        stage_aliases = _copy_stage_aliases(text)
        for match in FROM_PATTERN.finditer(text):
            errors.extend(_check_image_ref(match.group("image"), source))
        for match in COPY_FROM_PATTERN.finditer(text):
            image = match.group("image")
            if _is_copy_stage_alias(image, stage_aliases):
                continue
            errors.extend(_check_image_ref(image, source))

    for compose in _compose_files(root):
        text = compose.read_text(encoding="utf-8")
        source = str(compose.relative_to(root))
        for match in IMAGE_PATTERN.finditer(text):
            errors.extend(_check_image_ref(match.group("image").strip('"').strip("'"), source))
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    errors = [*_check_action_refs(root), *_check_container_refs(root)]
    if errors:
        print("Supply-chain pin check failed:")
        for error in errors:
            print(f"  {error}")
        return 1

    print("Supply-chain pin check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
