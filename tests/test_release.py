"""The repository releases everything from one version (scripts/release.py)."""

import importlib.util
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("release", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_all_manifests_share_one_version():
    assert release.check(None) == 0
    assert len(set(release.versions().values())) == 1


def test_tag_must_match_the_manifest_version():
    version = next(iter(release.versions().values()))
    assert release.check(f"v{version}") == 0
    assert release.check(f"refs/tags/v{version}") == 0
    assert release.check("v999.0.0") == 1


def test_version_rewrite_only_touches_the_right_section(tmp_path):
    manifest = tmp_path / "Cargo.toml"
    manifest.write_text(textwrap.dedent("""\
        [package]
        name = "x"
        version = "0.1.0"

        [dependencies]
        serde = { version = "1" }

        [other]
        version = "7.7.7"
        """))
    release._replace_toml_version(manifest, "package", "1.2.3-rc.1")
    text = manifest.read_text()
    assert 'version = "1.2.3-rc.1"' in text
    assert 'version = "7.7.7"' in text and 'serde = { version = "1" }' in text

    top = tmp_path / "extension.toml"
    top.write_text('id = "x"\nversion = "0.1.0"\n\n[language_servers.x]\nversion = "keep"\n')
    release._replace_toml_version(top, None, "2.0.0")
    assert top.read_text() == 'id = "x"\nversion = "2.0.0"\n\n[language_servers.x]\nversion = "keep"\n'

    with pytest.raises(SystemExit):
        release._replace_toml_version(top, "missing", "1.0.0")


def test_bump_rejects_non_semver():
    assert release.bump("1.0", commit=False) == 1
