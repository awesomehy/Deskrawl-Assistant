from pathlib import Path
import os
import ssl
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

root = Path(SPECPATH).parent
assets = [
    'game-catalog.json',
    'experience-types.json',
    'recommendation-types.json',
    'loot-plan-types.json',
    'loot-item-ui.json',
    'recommendation-catalog.json',
    'progression-catalog.json',
    'synthesis-catalog.json',
    'steam-market-catalog.json',
    'equipment-ui.json',
    'affix-groups-static.json',
    'affix-class-filters.json',
    'runtime-type-hints.json',
    'ui-runtime-hints.json',
    'container-transfer-types.json',
    'carriage-types.json',
    'item-ui.json',
    'compatibility-baseline.json',
    'game-actions-profile.json',
]
datas = [(str(root / 'data' / name), 'data') for name in assets]
datas.append((str(root / 'deskrawl_assistant' / 'web'), 'deskrawl_assistant/web'))
datas.append((str(root / 'deskrawl_assistant' / 'update_replace.ps1'), 'deskrawl_assistant'))
datas += collect_data_files('webview', subdir='js')

# Some Python distributions load an auxiliary public trust policy from ssl.py.
# Keep that dependency beside the frozen module; do not copy user certificates
# or change certificate verification to work around missing runtime files.
ssl_source = Path(ssl.__file__)
if 'codex-rejected-cas.pem' in ssl_source.read_text(encoding='utf-8'):
    ssl_policy = ssl_source.with_name('codex-rejected-cas.pem')
    if not ssl_policy.is_file():
        raise RuntimeError('Python SSL runtime policy file is missing.')
    datas.append((str(ssl_policy), '.'))

a = Analysis(
    [str(root / 'run_assistant.pyw')],
    pathex=[str(root)],
    binaries=collect_dynamic_libs('capstone'),
    datas=datas,
    hiddenimports=['truststore._windows'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['frida', 'UnityPy', 'PIL', 'tkinter', 'PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'cefpython3', 'gi'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Deskrawl装备助手-v1.5.4',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=str(root / 'packaging' / 'version-info.txt'),
    uac_admin=False,
    runtime_tmpdir=os.environ.get('DESKRAWL_ASSISTANT_BUNDLE_DIR'),
)
