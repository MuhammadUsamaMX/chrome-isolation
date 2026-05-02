# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for the Chrome Isolation stdio JSON bridge.
Produces a single self-contained binary: dist/bridge-bin/bridge
"""
import os
import sys

SRC = os.path.dirname(os.path.abspath(SPEC))
BACKEND = os.path.join(SRC, 'backend')

a = Analysis(
    [os.path.join(BACKEND, 'bridge.py')],
    pathex=[BACKEND],
    binaries=[],
    datas=[],
    hiddenimports=[
        'docker',
        'docker.api',
        'docker.models',
        'docker.models.containers',
        'docker.models.images',
        'docker.types',
        'docker.transport',
        'websocket',
        'requests',
        'urllib3',
        'charset_normalizer',
        'certifi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'PIL', 'PyQt5', 'wx'],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='bridge',
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,
    upx=False,          # UPX can break on some distros; keep off for portability
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=True,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
