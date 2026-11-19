# PyInstaller spec for the engine inside Screen Organizer.app (onedir, so it starts fast).
import importlib
import os
import platform
import sys
import sysconfig
from importlib.metadata import distribution

from PyInstaller.config import CONF
from PyInstaller.utils.hooks import collect_all, collect_data_files

datas, binaries, hidden = [], [], []
for pkg in ("pymobiledevice3", "mcp", "uvicorn", "starlette", "sse_starlette"):
    d, b, h = collect_all(pkg)
    datas += d; binaries += b; hidden += h
# Include only the distribution metadata document used by the MCP handshake.
project = distribution("home-screen-organizer")
metadata = next(path for path in project.files if (path.name == "METADATA" and path.parent.name.endswith(".dist-info"))
                or (path.name == "PKG-INFO" and path.parent.name.endswith(".egg-info")))
datas.append((str(project.locate_file(metadata)), str(metadata.parent)))
datas += collect_data_files("homescreen")  # web assets and simulator icon reader source

a = Analysis(["engine.py"], pathex=[".."], binaries=binaries, datas=datas, hiddenimports=hidden + [
    "homescreen.ui", "homescreen.server", "homescreen.cli", "homescreen.preview"],
    excludes=["tkinter", "pytest", "IPython"])
sys.path.insert(0, SPECPATH)
from frozen_engine import project_metadata, replace_sysconfig

module = sysconfig._get_sysconfigdata_name()
replace_sysconfig(a.pure, CONF["code_cache"].get(id(a.pure)), workpath, module,
                  importlib.import_module(module).build_time_vars, sys.base_prefix)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="engine", console=True, target_arch=os.environ.get("HOMESCREEN_BUILD_ARCH", platform.machine()))
coll = COLLECT(exe, a.binaries, project_metadata(a.datas, str(metadata)), name="engine")

from pathlib import Path
from frozen_sources import capture
from native_sources import check_runtime

arch = os.environ["HOMESCREEN_BUILD_ARCH"]
capture(a, pyz, exe, coll, Path(SPECPATH).parent.resolve(), Path(os.environ["HOMESCREEN_FREEZE_SOURCES"]),
        Path(os.environ["HOMESCREEN_FREEZE_WHEELS"]), arch,
        os.environ.get("GITHUB_REPOSITORY", "worldsavepoint/ios-homescreen-organizer"), check_runtime(arch),
        os.environ.get("HOMESCREEN_SOURCE_COMMIT", ""), os.environ.get("HOMESCREEN_LOCAL_SOURCE"))
