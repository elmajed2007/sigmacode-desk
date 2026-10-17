# PyInstaller spec — onedir build of SigmaCode Desk (run from the repo root).
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

ROOT = Path(SPECPATH).parent
APP = ROOT / "app"

datas = [
    (str(APP / "assets"), "assets"),
    (str(APP / "sigmadesk" / "web"), "sigmadesk/web"),
]
datas += collect_data_files("pypdfium2_raw") + collect_data_files("pypdfium2")
datas += collect_data_files("docx")
binaries = collect_dynamic_libs("onnxruntime") + collect_dynamic_libs("pypdfium2_raw")

a = Analysis(
    [str(APP / "run.py")],
    pathex=[str(APP)],
    binaries=binaries,
    datas=datas,
    hiddenimports=["uvicorn.logging", "uvicorn.loops.auto", "uvicorn.protocols.http.auto",
                   "uvicorn.protocols.websockets.auto", "uvicorn.lifespan.on", "multipart",
                   "python_multipart", "fastapi.testclient", "httpx"],
    excludes=["tkinter", "matplotlib", "IPython", "pytest", "paddle", "torch"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="SigmaCodeDesk",
    console=False,
    icon=str(ROOT / "packaging" / "icon.ico"),
    version=str(ROOT / "packaging" / "version.txt"),
)
coll = COLLECT(exe, a.binaries, a.datas, name="SigmaCodeDesk")
