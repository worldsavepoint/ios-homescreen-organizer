"""Release publication uses the built commit and resumes interrupted drafts."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "macapp/publish_release.sh"
TARGET = "a" * 40


def publish(tmp_path, state="", tag_target="", corrupt=False, tag="v1.2.0", api_error=False, omit_intel=False):
    assets = tmp_path / "release"
    assets.mkdir()
    version = tag.removeprefix("v")
    names = [f"home-screen-organizer-{version}-macos-{arch}.{extension}"
             for arch in ("arm64", "x86_64") for extension in ("dmg", "zip")]
    names += [f"home-screen-organizer-{version}-macos.mcpb", f"home-screen-organizer-{version}-corresponding-source.tar.gz", "server.json"]
    if omit_intel:
        names = [name for name in names if "x86_64" not in name]
    from source_fixture import release_source
    target = release_source(assets, version, tmp_path / "project")
    (assets / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256((assets / name).read_bytes()).hexdigest()}  {name}\n" for name in names))
    if corrupt:
        (assets / names[0]).write_bytes(b"changed archive")
    notes = tmp_path / "notes.md"
    notes.write_text("Release notes\n")
    binary = tmp_path / "bin"
    binary.mkdir()
    log = tmp_path / "github-calls.jsonl"
    gh = binary / "gh"
    gh.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
with Path(os.environ["GH_CALL_LOG"]).open("a") as stream:
    stream.write(json.dumps(args) + "\\n")
if args[0] == "api":
    if os.environ["API_ERROR"] == "1":
        sys.exit(1)
    if "--paginate" in args:
        print(os.environ["RELEASE_STATE"], end="")
    elif "matching-refs" in args[1] and os.environ["TAG_TARGET"]:
        print("refs/tags/v1.2.0")
    elif "/commits/" in args[1]:
        print(os.environ["TAG_TARGET"])
''')
    gh.chmod(0o755)
    scripts = tmp_path / "project/macapp"
    result = subprocess.run(["bash", str(scripts / SCRIPT.name), str(assets), str(notes)],
                            capture_output=True, text=True, env={
        **os.environ, "PATH": f"{binary}:{Path(sys.executable).parent}:{os.environ['PATH']}",
        "GH_CALL_LOG": str(log), "RELEASE_STATE": state, "TAG_TARGET": target if tag_target == TARGET else tag_target,
        "GH_REPO": "example/project", "RELEASE_TAG": tag, "RELEASE_TARGET": target,
        "API_ERROR": "1" if api_error else "0",
    })
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def test_new_release_targets_built_commit(tmp_path):
    result, calls = publish(tmp_path)
    assert result.returncode == 0, result.stderr
    creation = next(args for args in calls if args[:2] == ["release", "create"])
    assert creation[2] == "v1.2.0"
    assert creation[creation.index("--title") + 1] == "Screen Organizer v1.2.0"
    assert creation[creation.index("--target") + 1] == subprocess.check_output(["git", "-C", tmp_path / "project", "rev-parse", "HEAD"], text=True).strip()
    assert creation[-8:] == ["./home-screen-organizer-1.2.0-macos-arm64.dmg",
                            "./home-screen-organizer-1.2.0-macos-arm64.zip",
                            "./home-screen-organizer-1.2.0-macos-x86_64.dmg",
                            "./home-screen-organizer-1.2.0-macos-x86_64.zip",
                            "./home-screen-organizer-1.2.0-macos.mcpb", "./home-screen-organizer-1.2.0-corresponding-source.tar.gz", "server.json", "SHA256SUMS.txt"]


def test_published_version_is_retained(tmp_path):
    result, calls = publish(tmp_path, state="v1.2.0\tfalse\n")
    assert result.returncode == 0, result.stderr
    assert all(args[0] == "api" for args in calls)


def test_existing_draft_uploads_assets_then_publishes(tmp_path):
    result, calls = publish(tmp_path, state="v1.2.0\ttrue\n", tag_target=TARGET)
    assert result.returncode == 0, result.stderr
    mutations = [args for args in calls if args[0] == "release"]
    assert [args[1] for args in mutations] == ["upload", "edit"]
    assert "--clobber" in mutations[0]
    assert "--draft=false" in mutations[1]
    assert mutations[1][mutations[1].index("--title") + 1] == "Screen Organizer v1.2.0"


def test_tag_at_another_commit_requires_new_version(tmp_path):
    result, calls = publish(tmp_path, tag_target="b" * 40)
    assert result.returncode != 0
    assert "version" in result.stderr.lower()
    assert all(args[0] == "api" for args in calls)


def test_archive_integrity_checked_before_publication(tmp_path):
    result, calls = publish(tmp_path, corrupt=True)
    assert result.returncode != 0
    assert calls == []


def test_prerelease_version_is_marked_as_prerelease(tmp_path):
    result, calls = publish(tmp_path, tag="v1.2.0-beta.1")
    assert result.returncode == 0, result.stderr
    creation = next(args for args in calls if args[:2] == ["release", "create"])
    assert creation[2] == "v1.2.0-beta.1"
    assert "--prerelease=true" in creation


def test_release_lookup_failure_stops_publication(tmp_path):
    result, calls = publish(tmp_path, api_error=True)
    assert result.returncode != 0
    assert all(args[0] == "api" for args in calls)


def test_both_architectures_are_required_for_publication(tmp_path):
    result, calls = publish(tmp_path, omit_intel=True)
    assert result.returncode != 0
    assert calls == []


def test_draft_upload_includes_standalone_bundle_and_registry_metadata(tmp_path):
    result, calls = publish(tmp_path, state="v1.2.0\ttrue\n", tag_target=TARGET)
    assert result.returncode == 0, result.stderr
    upload = next(args for args in calls if args[:2] == ["release", "upload"])
    assert "./home-screen-organizer-1.2.0-macos.mcpb" in upload
    assert "server.json" in upload
