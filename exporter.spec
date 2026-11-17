# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path
import os
import sys
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

windows_dir = Path(os.environ["WINDIR"])
os.environ["PATH"] = os.pathsep.join(str(p) for p in (
    Path(sys.executable).parent, Path(sys.base_prefix), windows_dir / "System32", windows_dir,
) if p.is_dir())
root = Path(SPECPATH)
bridge_deps = root / 'deployment' / 'bridge-deps'
sys.path.insert(0, str(bridge_deps))
datas = collect_data_files("rapidocr_onnxruntime")
datas += [(str(root / "order_exporter" / "assets" / "copy-icon.png"), "order_exporter/assets")]
datas += [(str(root / "order_exporter" / "assets" / "calendar-down.png"), "order_exporter/assets")]
datas += [(str(root / "order_exporter" / "assets" / "order-hook.js"), "order_exporter/assets")]
datas += [(str(root / "order_exporter" / "assets" / "runtime-profiles.json"), "order_exporter/assets")]
datas += [(str(path), 'order_exporter/assets/licenses') for path in (root / 'order_exporter/assets/licenses').glob('*.txt')]
binaries = collect_dynamic_libs("onnxruntime")
a = Analysis([str(root / "main.py")], pathex=[str(root), str(bridge_deps)], binaries=binaries, datas=datas,
             hiddenimports=["rapidocr_onnxruntime", "onnxruntime.capi.onnxruntime_pybind11_state"],
             excludes=["PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.QtQml", "PySide6.QtQuick", "torch", "tensorflow", "matplotlib", "pandas", "scipy", "pytest", "unittest"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="微信小店订单导出器", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False,
          icon=str(root / "order_exporter" / "assets" / "app.ico"))
