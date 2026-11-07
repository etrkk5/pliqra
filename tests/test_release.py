"""Publishing boundaries: reject ambiguous tags and mismatched installation files."""
from pathlib import Path
import pytest
from scripts.release_metadata import metadata


def release_tree(tmp_path: Path, version: str) -> Path:
    (tmp_path / "pliqra").mkdir()
    (tmp_path / "pliqra/__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / "docs/releases").mkdir(parents=True)
    (tmp_path / f"docs/releases/v{version}.md").write_text("Release notes\n")
    (tmp_path / "compose.release.yaml").write_text("image: ${PLIQRA_IMAGE:-ghcr.io/etrkk5/pliqra:" + version + "}\n")
    return tmp_path


@pytest.mark.parametrize("version,prerelease", [("2.1.0", "false"), ("2.1.0-rc.1", "true"), ("2.1.0-beta.2", "true")])
def test_release_classification(tmp_path, version, prerelease):
    result = metadata("v" + version, release_tree(tmp_path, version))
    assert result["version"] == version and result["prerelease"] == prerelease


@pytest.mark.parametrize("tag", ["2.1.0", "v2.1", "v02.1.0", "v2.1.0-rc.0", "v2.1.0-rc.01", "v2.1.0\nlatest=true"])
def test_invalid_release_tag_never_reaches_filesystem(tag, tmp_path):
    with pytest.raises(ValueError, match="Expected"):
        metadata(tag, tmp_path)


def test_release_requires_matching_version_notes_and_installation(tmp_path):
    root = release_tree(tmp_path, "2.1.0-rc.1")
    with pytest.raises(ValueError, match="does not match"):
        metadata("v2.1.0", root)
    notes = root / "docs/releases/v2.1.0-rc.1.md"
    notes.unlink()
    with pytest.raises(ValueError, match="Missing release notes"):
        metadata("v2.1.0-rc.1", root)
    notes.write_text("Notes")
    (root / "compose.release.yaml").write_text("image: ${PLIQRA_IMAGE:-ghcr.io/etrkk5/pliqra:2.1.0-rc.10}\n")
    with pytest.raises(ValueError, match="exact release version"):
        metadata("v2.1.0-rc.1", root)
