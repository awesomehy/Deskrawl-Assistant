"""Exercise the real frozen exe from an isolated folder; never lock gear."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Deskrawl装备助手-v1.1.2'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--connect-game', action='store_true')
    parser.add_argument('--desktop', action='store_true')
    args = parser.parse_args()
    source = ROOT / 'release' / NAME / (NAME + '.exe')
    archive = CArchiveReader(str(source))
    names = set(archive.toc)
    assert f'python{sys.version_info.major}{sys.version_info.minor}.dll' in names
    assert 'VCRUNTIME140.dll' in names or 'vcruntime140.dll' in names
    assert len([n for n in names if n.startswith('deskrawl_assistant/web/icons/') or n.startswith('deskrawl_assistant\\web\\icons\\')]) == 209
    assert not any('lock-rules.json' in n or 'latest-snapshot.json' in n or 'assistant-actions.jsonl' in n for n in names)
    assert not any('frida' in n.lower() or 'unitypy' in n.lower() for n in names)
    assert any(n.replace('\\','/').endswith('webview/js/api.js') for n in names)
    assert any(n.endswith('Python.Runtime.dll') for n in names)
    folder = ROOT / 'build' / ('exe独立测试 '+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    exe = folder / '仅此一个文件.exe'
    shutil.copyfile(source, exe)
    working = folder / '空白工作目录'
    working.mkdir()
    persistent = folder / '用户数据'
    env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX'):
        env.pop(key, None)
    system_root = os.environ.get('SystemRoot', 'C:/Windows')
    env['PATH'] = str(Path(system_root) / 'System32') + os.pathsep + system_root
    env['DESKRAWL_ASSISTANT_DATA_DIR'] = str(persistent)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    report = {'exe_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'test_directory': str(folder), 'port': port, 'checks': [], 'external_python_on_path': False}
    process = None
    token = None

    def request(path, payload=None, session=None):
        headers = {'Origin': base}
        if payload is not None:
            headers['Content-Type'] = 'application/json'
            headers['X-Assistant-Token'] = session or token
        value = None if payload is None else json.dumps(payload).encode('utf-8')
        with OPENER.open(urllib.request.Request(base+path, data=value, headers=headers), timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw

    def start():
        nonlocal process, token
        log = persistent / 'data/runtime/assistant-actions.jsonl'
        previous_loads = log.read_text(encoding='utf-8').count('desktop_window_loaded') if log.is_file() else 0
        command = [str(exe), '--port', str(port)]
        if not args.desktop: command.append('--no-browser')
        process = subprocess.Popen(command, cwd=working,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        deadline = time.monotonic()+30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise AssertionError('Frozen process exited: '+str(process.returncode))
            try:
                assert request('/api/ping')['app'] == 'deskrawl-local-assistant'
                assert request('/api/ping')['desktop'] is args.desktop
                token = request('/api/session')['token']
                if args.desktop:
                    while time.monotonic()<deadline:
                        if log.is_file() and log.read_text(encoding='utf-8').count('desktop_window_loaded') > previous_loads:
                            break
                        time.sleep(.1)
                    else: raise AssertionError('Native window page did not finish loading')
                return
            except (OSError, urllib.error.URLError):
                time.sleep(.1)
        raise AssertionError('Frozen service failed to start')

    def finish():
        nonlocal process
        if process is None:
            return
        request('/api/shutdown', {})
        assert process.wait(15) == 0
        process = None

    try:
        start()
        assert not request('/api/state')['rules']
        assert not request('/api/state')['monitoring']
        report['checks'].append('空规则、监控默认关闭；独立 exe 在中文和空格路径正常启动')
        assert '__SESSION_TOKEN__' not in request('/').decode('utf-8')
        assert b'async function api' in request('/app.js')
        assert request('/style.css')
        catalog = request('/api/catalog')
        assert len(catalog['equipment']) == 209
        assert sum(i['legendary'] for i in catalog['equipment']) == 52
        for item in catalog['equipment']:
            assert request(item['icon']).startswith(b'\x89PNG')
        report['checks'].append('页面脚本、样式、209 张装备图标与 52 件传说装备词典正常读取')
        rule = {'id':'packaging-disabled-rule','name':'打包验证（停用）', 'equipment_keys':['LegendaryBelt3'], 'enabled':False,
            'groups':{'primary':{'selected_stats':['Stats.CooldownReduction'],'operator':'>=','count':1}}, 'group_mode':'all'}
        request('/api/rule/save', rule)
        assert request('/api/state')['rules'][0]['enabled'] is False
        report['checks'].append('停用的测试规则成功保存到持久目录')
        # A second launch must attach to the same service and exit, leaving
        # the first instance's session and rule data intact.
        second_command = [str(exe), '--port', str(port)]
        if not args.desktop: second_command.append('--no-browser')
        second = subprocess.run(second_command, cwd=working,
            env=env, creationflags=subprocess.CREATE_NO_WINDOW, timeout=20)
        assert second.returncode == 0
        assert request('/api/session')['token'] == token
        report['checks'].append('重复启动复用已有服务，不创建第二个装备写入服务')
        if args.connect_game:
            request('/api/action', {'kind':'connect'})
            deadline = time.monotonic()+35
            while time.monotonic() < deadline:
                state = request('/api/state')
                if not state['busy']:
                    break
                time.sleep(.15)
            assert state['connected'] and state['complete'] and not state['error'], state['message']
            assert state['items']
            assert not state['monitoring']
            report['game_pid'] = state['pid']
            report['equipment_count'] = len(state['items'])
            assert all('base' in item['groups'] for item in state['items'])
            for item in state['items']:
                for values in item['groups'].values():
                    for stat in values:
                        assert isinstance(stat['display_value'], str)
                        assert isinstance(stat['matched'], bool)
                assert not any(stat['matched'] for stat in item['groups']['base'])
                random_types = {s['key'] for group in ('primary', 'secondary') for s in item['groups'][group]}
                assert not random_types & {'Stats.WeaponDamage', 'Stats.WeaponSpeed'}
                if item['slot'] in {'Helm', 'Chest', 'Pants', 'Boots', 'Gloves', 'Shoulder'}:
                    assert 'Stats.Armor' not in random_types
            report['checks'].append('实际装备的基础属性独立显示，不混入主副词条')
            report['checks'].append('装备预览包含数值与命中状态；基础属性不高亮')
            report['checks'].append('打包后的 exe 成功只读连接实际游戏并取得完整背包和仓库清单')
        old_token = token
        finish()
        start()
        state = request('/api/state')
        assert len(state['rules']) == 1 and state['rules'][0]['enabled'] is False
        assert state['connected'] is False and state['monitoring'] is False
        assert token != old_token
        report['checks'].append('退出后重启仍保留规则；不会自动连接或开启监控')
        assert (persistent / 'config/lock-rules.json').is_file()
        if args.connect_game:
            assert (persistent / 'data/runtime/latest-snapshot.json').is_file()
        finish()
        report['success'] = True
    finally:
        if process and process.poll() is None:
            # Terminate only this test's process tree if it could not shut down.
            subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)
    report['native_window'] = args.desktop
    (ROOT / 'release' / ('native-exe-verification.json' if args.desktop else 'exe-verification.json')).write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
