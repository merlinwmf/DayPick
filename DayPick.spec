# -*- mode: python ; coding: utf-8 -*-
import glob
import os
from PyInstaller.utils.hooks import collect_data_files

datas = []
datas += collect_data_files('tzdata')

# futu's generated *_pb2.py files bare-import each other (e.g. "import Common_pb2")
# via a sys.path.append(os.path.dirname(__file__)) trick in futu/common/pb/__init__.py.
# That only works if the sibling files are real files on disk, so force them to be
# extracted as data instead of bundled purely inside the PYZ archive.
import futu
futu_dir = os.path.dirname(futu.__file__)
pb_dir = os.path.join(futu_dir, 'common', 'pb')
for py_file in glob.glob(os.path.join(pb_dir, '*.py')):
    datas.append((py_file, 'futu/common/pb'))
# futu/__init__.py reads its own VERSION.txt at import time.
datas.append((os.path.join(futu_dir, 'VERSION.txt'), 'futu'))

# The UI is an HTML/CSS/JS page loaded by pywebview - not Python source, so
# PyInstaller won't pick it up on its own; bundle it explicitly.
datas.append(('web/daypick.html', 'web'))
datas.append(('web/ios16.css', 'web'))


a = Analysis(
    ['daypick_app.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Not used by this app; pandas' PyInstaller hook pulls these in transitively
    # (charting/Excel/image support) and they roughly triple the bundle size.
    excludes=['matplotlib', 'scipy', 'PIL', 'openpyxl', 'PyQt5', 'PySide2', 'PySide6', 'PyQt6'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='DayPick',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='DayPick',
)
