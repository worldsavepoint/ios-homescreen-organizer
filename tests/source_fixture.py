"""Small committed project and independently constructed native inputs for publication tests."""
import hashlib
import io
import json
from pathlib import Path
import plistlib
import shutil
import stat
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "macapp"))
from frozen_sources import distribution_notice
from source_archive import payload_tar
import mcp_bundle
from source_inputs import json_bytes, sha256


def fixture_checkout(root, version):
    scripts = root / "macapp"
    scripts.mkdir(parents=True)
    for name in ("publish_release.sh", "release.py", "source_archive.py", "source_inputs.py", "native_sources.py",
                 "frozen_sources.py", "mcp_bundle.py", "wheelhouse.py", "source_checkout.py", "source_tar.py"):
        shutil.copyfile(ROOT / "macapp" / name, scripts / name)
    (root / "LICENSE").write_text("Fixture original grant\n")
    (scripts / "mcp_launcher.sh").write_text("#!/bin/sh\nexit 0\n")
    (scripts / "engine.spec").write_text("fixture build instructions\n")
    (scripts / "SOURCE.md").write_text("fixture source instructions\n")
    (root / "pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (root / "homescreen").mkdir()
    (root / "homescreen/example.py").write_text("print('fixture')\n")
    inputs = {name: ("source of " + name).encode() for name in ("python/example", "pbs/arm64", "pbs/x86_64", "native/dependency", "openssl/4.0.3")}
    assets = {name: {"name": name.split("/")[-1]+".tar.gz", "url": "https://example.com/"+name+".tar.gz",
                     "sha256": hashlib.sha256(data).hexdigest(), "size": len(data)} for name,data in inputs.items()}
    packages = {"example": {"version": "1.0", "wheels": dict.fromkeys(("arm64", "x86_64"), "a" * 64), "source": assets["python/example"]}}
    runtime = {arch: {"version":"3.12.1", "archive":"pbs/"+arch, "libpython":"c"*64} for arch in ("arm64","x86_64")}
    native = {"assets":[dict(assets[n],id=n,owners=["cpython"],architectures=[n.split("/")[1]] if n.startswith("pbs/") else ["arm64","x86_64"])
                        for n in sorted(inputs) if not n.startswith("python/")],"runtime":runtime,"cargo":{}}
    openssl = next(item for item in native["assets"] if item["id"] == "openssl/4.0.3")
    openssl.update(architectures=["x86_64"], notices={"openssl-4.0.3/LICENSE.txt": hashlib.sha256(b"authenticated OpenSSL grant").hexdigest()})
    locks = scripts / "sources"
    locks.mkdir()
    for name,value in (("python",packages),("native",native)):
        (locks/(name+".json")).write_bytes(json_bytes(value))
    def git(*args):
        return subprocess.check_output(["git","-C",root,*args],text=True).strip()
    git("init","-q")
    git("add",".")
    git("-c","user.name=Fixture","-c","user.email=fixture@example.com","-c","commit.gpgsign=false","commit","-qm","Build fixture")
    return git("rev-parse","HEAD"), inputs, assets, runtime


def release_source(folder, version, root, repository="example/project"):
    commit, inputs, assets, runtimes = fixture_checkout(root, version)
    files = {}
    for path in root.rglob("*"):
        if path.is_file() and ".git" not in path.relative_to(root).parts:
            files["project/"+path.relative_to(root).as_posix()] = path.read_bytes()
    files["README.md"] = files["project/macapp/SOURCE.md"]
    selected = {}
    for name, raw in inputs.items():
        pin = assets[name]
        path = "sources/"+pin["sha256"]+"-"+pin["name"]
        files[path]=raw
        selected[name]={"url":pin["url"],"sha256":pin["sha256"],"path":path}
    native = {}
    prefix = "Screen Organizer.app/Contents/Resources/"
    for arch in ("arm64","x86_64"):
        body=("native "+arch).encode()
        engine_hash=hashlib.sha256(body).hexdigest()
        lib=b"module"
        notice=b"original example notice"
        index=b"components"
        volume={"projectCommit":commit,"projectFiles":{},"version":version,"arch":arch,"engine":engine_hash,"runtime":runtimes[arch],
                "owners":{"example":{"version":"1.0","wheel":"a"*64}},
                "members":[["file","module.so","example","module.c",hashlib.sha256(lib).hexdigest()]],
                "notices":{"example/LICENSE":hashlib.sha256(notice).hexdigest(),"components.json":hashlib.sha256(index).hexdigest()},"overlays":{}}
        if arch == "x86_64":
            volume["notices"]["native/openssl/4.0.3/openssl-4.0.3/LICENSE.txt"] = hashlib.sha256(b"authenticated OpenSSL grant").hexdigest()
        name="inventories/"+arch+".json"
        files[name]=json_bytes(volume)
        archive=folder/f"home-screen-organizer-{version}-macos-{arch}.zip"
        with zipfile.ZipFile(archive,"w") as z:
            mode=zipfile.ZipInfo(prefix+"engine/engine")
            mode.external_attr=(stat.S_IFREG|0o755)<<16
            z.writestr(mode,body)
            z.writestr(prefix+"engine/_internal/module.so",lib)
            z.writestr(prefix+"SourceInventory.json",files[name])
            z.writestr(prefix+"THIRD-PARTY-NOTICES.txt",distribution_notice(repository,version))
            z.writestr(prefix+"ThirdPartyLicenses/example/LICENSE",notice)
            z.writestr(prefix+"ThirdPartyLicenses/components.json",index)
            if arch == "x86_64":
                z.writestr(prefix+"ThirdPartyLicenses/native/openssl/4.0.3/openssl-4.0.3/LICENSE.txt",b"authenticated OpenSSL grant")
            for n in ["LICENSE","dependencies.txt","requirements-build.lock"]+(["requirements-cryptography-x86_64.lock","OPENSSL-LICENSE.txt"] if arch=="x86_64" else []):
                z.writestr(prefix+n,b"authenticated OpenSSL grant" if n=="OPENSSL-LICENSE.txt" else b"notice")
            z.writestr("Screen Organizer.app/Contents/Info.plist",plistlib.dumps({"CFBundleShortVersionString":version.split("-")[0],
                       "HomeScreenOrganizerReleaseVersion":version,"HomeScreenOrganizerArchitecture":arch}))
        native[arch]={"zip":sha256(archive),"inventory":name}
    for arch in ("arm64", "x86_64"):
        (folder / f"home-screen-organizer-{version}-macos-{arch}.dmg").touch()
    (folder / "SHA256SUMS.txt").write_text("".join(
        sha256(path) + "  " + path.name + "\n" for path in sorted(folder.iterdir()) if path.suffix in (".dmg", ".zip")))
    from unittest.mock import patch
    with patch.object(mcp_bundle, "ROOT", root):
        mcp_bundle.assemble(folder, folder, repository, version)
    name=f"home-screen-organizer-{version}-corresponding-source.tar.gz"
    payload_tar(folder/name,((n,0o644,io.BytesIO(v),len(v)) for n,v in sorted(files.items())),
                {"version":version,"commit":commit,"architectures":native,"inputs":selected})
    return commit
