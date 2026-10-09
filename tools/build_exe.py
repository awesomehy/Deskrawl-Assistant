"""Build the Windows one-file release using an explicit asset allowlist."""
from __future__ import annotations

import hashlib
from importlib.metadata import distribution, version
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from zipfile import ZipFile, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Deskrawl装备助手-v1.1.4-xp'


def license_text(package):
    info = distribution(package)
    files = [f for f in info.files if '/licenses/' in str(f)]
    if not files:
        raise RuntimeError(f'Missing license: {package}')
    return '\n'.join(info.locate_file(f).read_text(encoding='utf-8') for f in files)


def main():
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise RuntimeError('Build on Windows with 64-bit Python.')
    if version('pyinstaller') != '6.22.3':
        raise RuntimeError('Install requirements-build.txt before building.')
    output = ROOT / 'release' / NAME
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
        '--distpath', str(output), '--workpath', str(ROOT / 'build' / 'pyinstaller'),
        str(ROOT / 'packaging' / 'deskrawl.spec')], cwd=ROOT, check=True)
    exe = output / (NAME + '.exe')
    shutil.copyfile(ROOT / 'packaging' / '使用说明.txt', output / '使用说明.txt')
    setup = ROOT / 'packaging' / 'MicrosoftEdgeWebview2Setup.exe'
    if not setup.is_file(): raise RuntimeError('Missing official WebView2 bootstrapper.')
    shutil.copyfile(setup,output / setup.name)
    python_license = (Path(sys.base_prefix) / 'LICENSE.txt').read_text(encoding='utf-8')
    license_note = ('本程序内置 Python 运行环境及标准库。以下为相应许可；\n'
        'PyInstaller 的引导程序许可包含分发可执行程序的例外条款。\n'
        '游戏装备名称和图标来自 Deskrawl，相关权利归游戏权利人所有。\n\n')
    license_note += '\n===== Python =====\n'+python_license
    license_note += '\n===== PyInstaller =====\n'+license_text('pyinstaller')
    license_note += '\n===== PyInstaller hooks =====\n'+license_text('pyinstaller-hooks-contrib')
    license_note += ('\n===== Microsoft WebView2 =====\n'
        'The optional MicrosoftEdgeWebview2Setup.exe is the official Microsoft bootstrapper.\n'
        'Source: https://go.microsoft.com/fwlink/p/?LinkId=2124703\n'
        'Distribution: https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution\n'
        'The installer displays its applicable Microsoft license terms.\n')
    for package in ('pywebview','pythonnet','clr_loader','cffi','pycparser','bottle','proxy_tools','typing_extensions'):
        info = distribution(package)
        files = [f for f in info.files if 'license' in str(f).lower() or 'copying' in str(f).lower()]
        license_note += '\n===== '+package+' =====\n'
        if files:
            license_note += '\n'.join(info.locate_file(f).read_text(encoding='utf-8') for f in files)
        else:
            license_note += 'License: '+str(info.metadata.get('License',''))+'\n'
    (output / '第三方许可.txt').write_text(license_note, encoding='utf-8-sig')
    profile = json.loads((ROOT / 'data/runtime-type-hints.json').read_text(encoding='utf-8'))
    report = {'name': NAME, 'version': '1.1.4-xp', 'platform': 'Windows x64', 'presentation':'native WebView2 window',
        'python': sys.version.split()[0], 'pyinstaller': version('pyinstaller'),
        'exe_bytes': exe.stat().st_size, 'exe_sha256': hashlib.sha256(exe.read_bytes()).hexdigest(),
        'supported_game_metadata_sha256': profile['metadataSha256'],
        'supported_game_assembly_sha256': profile['gameAssemblySha256'],
        'bundled_personal_rules': False, 'bundled_player_snapshots': False}
    report['webview2_setup_bytes'] = setup.stat().st_size
    report['webview2_setup_sha256'] = hashlib.sha256(setup.read_bytes()).hexdigest()
    (ROOT / 'release' / 'build-report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    archive = ROOT / 'release' / (NAME + '-Windows64.zip')
    # Never archive the whole release directory: test data and user's files
    # must not accidentally become part of a future rebuild.
    with ZipFile(archive, 'w', ZIP_DEFLATED) as zipped:
        for file in (exe, output / '使用说明.txt', output / '第三方许可.txt', output / setup.name):
            zipped.write(file, NAME + '/' + file.name)
    print(json.dumps({'exe': str(exe), 'zip': str(archive), **report}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
