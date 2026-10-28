"""Exercise the real frozen exe from an isolated folder; never lock gear."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid

from PyInstaller.archive.readers import CArchiveReader

ROOT = Path(__file__).resolve().parents[1]
NAME = 'Deskrawl装备助手-v1.4.5'
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--connect-game', action='store_true')
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--test-managed', action='store_true', help='Check the explicit test entry without connecting to a game.')
    parser.add_argument('--check-update', action='store_true', help='Also verify a live HTTPS update check against GitHub.')
    parser.add_argument('--exe', type=Path, help='Test a separately packaged executable.')
    parser.add_argument('--report', type=Path, help='Save verification without replacing another build report.')
    args = parser.parse_args()
    source = args.exe.resolve() if args.exe else ROOT / 'release' / NAME / (NAME + '.exe')
    archive = CArchiveReader(str(source))
    names = set(archive.toc)
    assert f'python{sys.version_info.major}{sys.version_info.minor}.dll' in names
    assert 'VCRUNTIME140.dll' in names or 'vcruntime140.dll' in names
    normalized = {n.replace('\\','/'):n for n in names}
    icons = {n for n in normalized if n.startswith('deskrawl_assistant/web/icons/')}
    loot_items = json.loads(archive.extract(normalized['data/loot-item-ui.json']))['items']
    assert len(icons) == len(loot_items) == 588
    assert loot_items['WerewolfMat']['name'] == '狼人毛皮'
    assert '10%' in loot_items['LegendaryBoots4']['description']
    assert all(loot_items[key]['description'] for key in ('DivineHelm1', 'DivineHelm3', 'DivineRing2', 'LegendaryNeck4', 'LegendaryShoulder3'))
    assert all('deskrawl_assistant/web'+i['icon'] in icons for i in loot_items.values())
    item_ui = json.loads(archive.extract(normalized['data/item-ui.json']))
    assert len(item_ui['items']) == 235
    assert all('deskrawl_assistant/web'+i['icon'] in icons for i in item_ui['items'].values())
    assert all(i['effect_groups'] for i in item_ui['items'].values())
    assert not any('lock-rules.json' in n or 'latest-snapshot.json' in n or 'assistant-actions.jsonl' in n for n in names)
    assert not any('activity.sqlite3' in n or 'experience.sqlite3' in n for n in names)
    assert 'data/experience-types.json' in normalized
    assert 'data/recommendation-types.json' in normalized
    assert 'data/loot-plan-types.json' in normalized
    assert 'data/recommendation-catalog.json' in normalized
    assert not any('recommendations.sqlite3' in n for n in names)
    assert any('_sqlite3' in n for n in names)
    assert not any('frida' in n.lower() or 'unitypy' in n.lower() for n in names)
    assert any(n.replace('\\','/').endswith('webview/js/api.js') for n in names)
    assert any(n.endswith('Python.Runtime.dll') for n in names)
    assert 'deskrawl_assistant/web/rule-tools.js' in normalized
    assert 'deskrawl_assistant/web/inventory-tools.js' in normalized
    bundled_modules = archive.open_embedded_archive(next(n for n in names if n.endswith('.pyz')))
    assert all(module in bundled_modules.toc for module in ('truststore', 'truststore._api', 'truststore._windows'))
    assert 'deskrawl_assistant.stargazing' in bundled_modules.toc
    assert 'deskrawl_assistant.affix_pools' in bundled_modules.toc
    for module in ('reconnect', 'reconnect_runtime'):
        assert 'deskrawl_assistant.' + module in bundled_modules.toc
    assert 'data/affix-class-filters.json' in normalized
    assert 'deskrawl_assistant.community_stats' in bundled_modules.toc
    assert 'deskrawl_assistant.statistics_reader' in bundled_modules.toc
    for module in ('combat_rewards', 'game_lifecycle', 'mythic'):
        assert 'deskrawl_assistant.' + module in bundled_modules.toc
    for module in ('synthesis', 'synthesis_runtime', 'steam_market', 'market_currency'):
        assert 'deskrawl_assistant.' + module in bundled_modules.toc
    for module in ('auction', 'auction_runtime', 'auction_service', 'manual_auction',
                   'steam_session', 'steam_trading', 'steam_listing_receipts'):
        assert 'deskrawl_assistant.' + module in bundled_modules.toc
    for asset in ('auction-ui.js', 'auction-ui.css', 'inventory-auction.js', 'inventory-auction.css'):
        assert 'deskrawl_assistant/web/' + asset in normalized
    assert not any(marker in name.lower() for name in names for marker in
                   ('steam-session', 'steam-sale-journal', 'auction-pending', 'auction-upload-intents', '.dpapi'))
    assert not any(n.startswith('deskrawl_assistant.remote_') for n in bundled_modules.toc)
    assert not any(n.startswith(('deskrawl_assistant.account_', 'deskrawl_assistant.sandbox_accounts')) for n in bundled_modules.toc)
    managed_page = archive.extract(normalized['deskrawl_assistant/web/index.html']).decode('utf-8')
    assert managed_page.count('data-managed-fold="') == 11
    assert 'id="managed-reconnect"' in managed_page and 'id="managed-reconnect-index"' in managed_page
    assert 'deskrawl_assistant/update_replace.ps1' in normalized
    assert 'data/compatibility-baseline.json' in normalized
    actions_profile = json.loads(archive.extract(normalized['data/game-actions-profile.json']))
    assert set(actions_profile['verified_kinds']) == {
        'return_town','enter_map','switch_difficulty','equip','sell','salvage','open_chest','collect_ground',
        'allocate_talent','merchant_open','merchant_buy','merchant_close','merchant_refresh',
        'synthesize_gems','synthesize_runes','auction_open','auction_upload'}
    growth = json.loads(archive.extract(normalized['data/progression-catalog.json']))
    assert set(actions_profile['enabled_kinds']) == {'learn_life_skill', 'set_skill'}
    assert set(growth['heroes']) == {'1','2','4','8'}
    growth_icons = {n for n in normalized if n.startswith('deskrawl_assistant/web/progression-icons/')}
    assert len(growth_icons) == 333
    assert all('deskrawl_assistant/web'+node['icon'] in growth_icons for hero in growth['heroes'].values()
        for node in hero['talents'] + hero['abilities'])
    assert all('deskrawl_assistant/web'+node['icon'] in growth_icons for node in growth['life_skills'] + growth['shop'])
    assert any('capstone' in n.lower() and n.lower().endswith('.dll') for n in names)
    folder = ROOT / 'build' / ('exe独立测试 '+uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    exe = folder / '仅此一个文件.exe'
    shutil.copyfile(source, exe)
    working = folder / '空白工作目录'
    working.mkdir()
    persistent = folder / '用户数据'
    # Keep packaging checks away from the real community statistics service.
    # Seed only this test's isolated database with a local receipt endpoint.
    class ReceiptHandler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/v1/register':
                result = {'schema': 1, 'client_id': payload['client_id'], 'token': 'a' * 64}
            elif self.path == '/v1/report':
                assert self.headers.get('Authorization') == 'Bearer ' + 'a' * 64
                assert payload['version'] == '1.4.5'
                result = {'accepted': True, 'report_id': payload['report_id'],
                          'sequence': payload['sequence'], 'accepted_at': datetime.now(timezone.utc).isoformat()}
            else:
                self.send_error(404)
                return
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    receipt_server = ThreadingHTTPServer(('127.0.0.1', 0), ReceiptHandler)
    receipt_thread = threading.Thread(target=receipt_server.serve_forever, daemon=True)
    receipt_thread.start()
    receipt_url = f'http://127.0.0.1:{receipt_server.server_port}'
    statistics_database = persistent / 'data/runtime/community-stats.sqlite3'
    statistics_database.parent.mkdir(parents=True)
    with sqlite3.connect(statistics_database) as db:
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
        db.execute('INSERT INTO settings VALUES (?,?)', ('url', json.dumps(receipt_url)))
    env = os.environ.copy()
    for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'CONDA_PREFIX'):
        env.pop(key, None)
    system_root = os.environ.get('SystemRoot', 'C:/Windows')
    env['PATH'] = str(Path(system_root) / 'System32') + os.pathsep + system_root
    env['DESKRAWL_ASSISTANT_DATA_DIR'] = str(persistent)
    env['DESKRAWL_ASSISTANT_CACHE_DIR'] = str(folder / '缓存')
    temp = folder / '临时目录'
    temp.mkdir()
    env['TEMP'] = env['TMP'] = str(temp)
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
        if session or token:
            headers['X-Assistant-Token'] = session or token
        if payload is not None:
            headers['Content-Type'] = 'application/json'
        value = None if payload is None else json.dumps(payload).encode('utf-8')
        with OPENER.open(urllib.request.Request(base+path, data=value, headers=headers), timeout=15) as response:
            raw = response.read()
            return json.loads(raw) if response.headers.get_content_type() == 'application/json' else raw

    def start():
        nonlocal process, token
        log = persistent / 'data/runtime/assistant-actions.jsonl'
        previous_loads = log.read_text(encoding='utf-8').count('desktop_window_loaded') if log.is_file() else 0
        command = [str(exe), '--port', str(port)]
        if args.test_managed: command.append('--test-managed')
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
                assert request('/api/state')['app_version'] == '1.4.5'
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

    def check_reporting_disabled():
        for path, payload in (
            ('/api/recommendations', None),
            ('/api/recommendations/settings', {'minutes': 30}),
            ('/api/experience', None),
            ('/api/experience/sample', {'stage': '隔离验证', 'xp': 100, 'seconds': 20}),
        ):
            try:
                request(path, payload)
            except urllib.error.HTTPError as exc:
                result = json.load(exc)
                assert exc.code == 410 and result['disabled'] is True, result
            else:
                raise AssertionError('Retired reporting endpoint accepted a request: ' + path)
        assert not list(persistent.rglob('experience.sqlite3*'))
        assert not list(persistent.rglob('recommendations.sqlite3*'))

    try:
        start()
        if args.check_update:
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                update = request('/api/update')
                if update['phase'] in ('up_to_date', 'available'):
                    assert not update['error']
                    report['update_check'] = {key:update[key] for key in
                        ('phase', 'current_version', 'latest_version', 'checked_at')}
                    report['checks'].append('冻结程序使用 Windows 原生证书验证成功读取 GitHub 更新信息')
                    break
                assert update['phase'] in ('idle', 'checking'), update
                time.sleep(.2)
            else:
                raise AssertionError('Frozen updater did not finish its HTTPS check')
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            community = request('/api/community')
            if community['last_ack']:
                break
            time.sleep(.1)
        assert community['enabled'] is True and community['url'] == receipt_url
        assert community['acknowledged'] >= 1 and community['last_ack']
        assert community['interval_seconds'] == 30 and community['source'] == 'game_statistics'
        report['checks'].append('匿名统计默认开启，以匹配回执确认成功；隔离检查只连接本机模拟服务，不上报公网测试身份')
        assert not request('/api/state')['rules']
        assert not request('/api/state')['monitoring']
        assert not request('/api/state')['automation']['running']
        managed = request('/api/managed')
        assert not managed['running'] and managed['phase'] == 'idle'
        assert managed['settings']['auction'] == {
            'upload_enabled': False, 'sell_enabled': False, 'price_rule': 'lowest_sell',
            'categories': [], 'item_keys': [], 'excluded_keys': []}
        assert request('/api/state')['manual_auction_availability']['available'] is False
        steam = request('/api/auction/state')
        assert not steam.get('steam', {}).get('authenticated')
        assert not request('/api/auction/manual/state')['running']
        for asset in ('auction-ui.js', 'auction-ui.css', 'inventory-auction.js', 'inventory-auction.css'):
            assert request('/' + asset)
        report['checks'].append('上架及出售默认关闭、类别为空；Steam 未登录，断连禁止手动上架，交易模块与四个页面资源完整内置且无个人凭证')
        stargazing = managed['settings']['farming']['stargazing']
        assert not stargazing['enabled'] and all(stargazing['rewards'].values())
        assert 'id="managed-stargazing"' in request('/').decode('utf-8')
        assert not any('remote-control' in n for n in names)
        cleanup = managed['settings']['cleanup']
        assert [cleanup[k+'_pages'] for k in ('equipment','rune','gem')] == [[1,2,3],[4],[5]]
        assert [cleanup[k+'_pages'] for k in ('gold_bar', 'legendary_rein')] == [[5], [5]]
        assert all(cleanup[k+'_action'] == 'store' for k in ('gold_bar', 'legendary_rein'))
        auto = request('/api/state')['automation']['settings']
        assert [auto[k]['pages'] for k in ('pressure','runes','gems')] == [[1,2,3],[4],[5]]
        assert not managed['warehouse_ready']
        assert 'data/game-actions-profile.json' in normalized
        assert request('/automation-tools.js') and request('/managed-mode.js')
        assert '托管模式' in request('/').decode('utf-8')
        report['checks'].append('托管页面与动作配置内置；新用户装备1/2/3、符文4、宝石5；断线不误报解锁，托管默认停止')
        report['checks'].append('全部托管、商人、天赋、生活技能与技能切换入口正常开放；实机验证状态单独记录，未连接时不执行动作')
        assert managed['settings']['merchant']['trigger_percent'] == 85
        assert managed['settings']['merchant']['spend_to_percent'] == 0
        assert not any(managed['settings']['merchant']['weights'].values())
        assert request('/managed-growth.js')
        assert managed['progression_catalog'] == growth
        report['checks'].append('四职业成长字典及319张天赋、技能、生活技能和商人图标及14张界面纹理内置；新用户85%触发、保留0%、购买件数全0')
        report['checks'].append('空规则、监控默认关闭；独立 exe 在中文和空格路径正常启动')
        assert '__SESSION_TOKEN__' not in request('/').decode('utf-8')
        assert b'async function api' in request('/app.js')
        assert b'openCombinationManager' in request('/rule-tools.js')
        assert request('/style.css')
        page = request('/').decode('utf-8')
        assert 'class="brand" href="https://deskrawl.hhyan.me/"' in page
        assert 'community-notice' in page and '启动后每 30 秒自动匿名上报' in page
        assert 'community-report-now' not in page and 'community-dialog' not in page
        assert 'remote-dialog' not in page and 'remote-open' not in page
        assert all('id="'+element+'"' in page for element in ('game-state','game-start','game-close'))
        assert page.count('<option value="Mythic">神话裂隙</option>') == 2
        assert 'community-stats.js' not in page and 'community-stats.css' in page
        report['checks'].append('官网品牌入口和一行统计告知已内置，未包含上报开关、详情及暂停的微信远控入口')
        tabs = re.findall(r'data-tab="([^"]+)"', page)
        assert tabs == ['rules', 'inventory', 'managed', 'loot-preview', 'logs', 'feedback']
        assert 'id="managed-tab-status"' in page
        assert all(page.count('id="'+tab+'-page"') == 1 for tab in tabs)
        assert 'src="/managed-mode.js"' in page
        assert b"$('managed-page').hidden=tab!=='managed'" in request('/app.js')
        assert b"$('loot-preview-page').hidden=tab!=='loot-preview'" in request('/app.js')
        assert 'recommendations-page' not in page and 'experience-page' not in page
        report['checks'].append('六个页签完整且没有重复；交流与反馈位于日志之后，托管与掉落预览导航保留')
        assert '欢迎进群交流游戏心得以及反馈使用问题。' in page
        assert '1062040183' in page and request('/images/qq-group.png').startswith(b'\x89PNG')
        assert 'id="equipped-dialog"' in page and page.count('data-column="') == 7
        assert '单体 Boss 基准战力' in page
        assert 'id="loot-preview-page"' in page
        preview = request('/api/loot-preview')
        assert preview['phase'] == 'waiting' and preview['run'] is None and not preview['waves']
        synth = request('/api/managed')['settings']['synthesis']
        assert synth['gems']['enabled'] is False and synth['runes']['enabled'] is False
        assert request('/api/market/settings')['currency_id'] == 0
        assert request('/api/market/settings', {'currency_id':23})['currency_id'] == 23
        assert request('/api/market/quotes', {'items':[]}) == {'items':[], 'currency_id':23}
        for unpublished in ('/api/accounts', '/api/remote'):
            try:
                request(unpublished)
            except urllib.error.HTTPError as exc:
                assert exc.code == 404
            else:
                raise AssertionError('Unpublished feature endpoint is exposed: '+unpublished)
        report['checks'].append('自动合成默认关闭；市场币种保存及断连报价接口正常，多开和远控接口不存在')
        report['checks'].append('掉落预览界面、独立类型配置及只读接口内置，断连时无旧计划')
        assert b'InventoryTools' in request('/inventory-tools.js')
        report['checks'].append('角色装备独立弹窗、七个表头菜单及固定 Boss 场景说明已随 exe 内置')
        check_reporting_disabled()
        report['checks'].append('停用的报告接口返回410，不创建经验或推荐数据库')
        catalog = request('/api/catalog')
        assert len(catalog['equipment']) == 223
        assert set(catalog['class_pools']) == {'1', '2', '4', '8'}
        assert [len(catalog['class_pools'][c]['Weapon']['primary']) for c in ('1','2','4','8')] == [17,19,19,15]
        report['checks'].append('四职业传说随机词条池配置与模块已内置，打包版接口返回对应候选')
        assert sum(i['legendary'] for i in catalog['equipment']) == 64
        for item in catalog['equipment']:
            assert request(item['icon']).startswith(b'\x89PNG')
        report['checks'].append('页面脚本、样式、223 张装备图标与 64 件传说装备词典正常读取')
        for item in item_ui['items'].values():
            assert request(item['icon']).startswith(b'\x89PNG')
        report['checks'].append('36 张宝石、199 张符文图标及效果说明全部内置并可读取')
        settings = {'carriage':{'enabled':False,'types':['gem']},'gems':{'enabled':True,'pages':[3,5]},
                    'runes':{'enabled':False,'pages':[1,4]},
                    'pressure':{'enabled':False,'threshold':4,'target_free':12,'equipment_filter':'all','pages':[1,2,3]}}
        request('/api/automation/settings', {'settings':settings})
        assert request('/api/state')['automation']['settings'] == settings
        shared = request('/api/managed')['settings']['cleanup']
        assert shared['gem_pages'] == [3,5] and shared['rune_pages'] == [1,4]
        assert not request('/api/state')['automation']['running']
        managed_settings = managed['settings']
        assert managed_settings['reconnect'] == {'enabled': False, 'character_index': 1}
        managed_settings['reconnect'] = {'enabled': True, 'character_index': 2}
        managed_settings['farming']['stargazing']['rewards']['gem'] = False
        managed_settings['farming']['stargazing']['rewards']['gold_bar'] = False
        managed_settings['farming']['stargazing']['rewards']['divine_equipment'] = False
        managed_settings['cleanup'].update(equipment_pages=[5],rune_pages=[2,4],gem_pages=[1])
        managed_settings['cleanup'].update(gold_bar_pages=[2],legendary_rein_pages=[4])
        managed_settings['progression']['talent_targets'] = {'4': {'HunterTalent0': 10, 'HunterTalent2': 2}}
        managed_settings['progression']['skill_stages'] = {'4': [{'level': 1, 'slots': ['HunterBasicAttack1', None, None, None]}]}
        managed_settings['life_skills']['priorities'] = [growth['life_skills'][0]['id']]
        managed_settings['merchant'].update(trigger_percent=93, spend_to_percent=17)
        managed_settings['merchant']['weights']['2'] = 1
        managed_settings['merchant']['weights']['7'] = 10
        managed_settings['auction'].update(upload_enabled=True, categories=['grand_rune'],
            excluded_keys=['LegendaryRune_HunterSpecial1'], price_rule='highest_buy')
        saved_managed = request('/api/managed/settings', {'settings':managed_settings})['settings']
        assert saved_managed['cleanup']['rune_pages'] == [2,4]
        settings['gems']['pages'] = [1]
        settings['runes']['pages'] = [2,4]
        assert request('/api/state')['automation']['settings'] == settings
        report['checks'].append('符文和宝石分区从自动整理与托管双向保存立即同步，其他自动整理设置保留')
        assert not saved_managed['farming']['stargazing']['rewards']['gem']
        assert not saved_managed['farming']['stargazing']['rewards']['gold_bar']
        assert saved_managed['cleanup']['gold_bar_pages'] == [2]
        assert saved_managed['cleanup']['legendary_rein_pages'] == [4]
        assert saved_managed['farming']['stargazing']['rewards']['divine_equipment']
        assert not request('/api/managed')['running']
        rule = {'id':'packaging-disabled-rule','name':'打包验证（停用）', 'equipment_keys':['LegendaryBelt3'], 'enabled':False,
            'groups':{'primary':{'selected_stats':['Stats.CooldownReduction'],'operator':'>=','count':1}}, 'group_mode':'all'}
        request('/api/rule/save', rule)
        second_rule = {**rule,'id':'packaging-second-rule','name':'第二个流派组合（停用）'}
        request('/api/rule/save',second_rule)
        assert len(request('/api/export/rules')['rules']) == 2
        originals = request('/api/export/rules')['rules']
        # All mutations use this test's isolated data directory; monitoring
        # and automation stay paused even when an imported rule is enabled.
        target = next(i['key'] for i in catalog['equipment']
            if i['legendary'] and i['slot'] == 'Belt' and i['key'] != 'LegendaryBelt3')
        copied = request('/api/rule/copy', {'source_key':'LegendaryBelt3','target_key':target})
        assert copied['copied'] == 2 and copied['replaced'] == 0
        duplicate = request('/api/rule/copy', {'source_key':'LegendaryBelt3','target_key':target})
        assert duplicate['copied'] == 0 and duplicate['skipped'] == 2
        added = [r['id'] for r in request('/api/export/rules')['rules'] if target in r['equipment_keys']]
        assert request('/api/rule/delete', {'ids':added,'equipment_key':target})['deleted'] == 2
        imported = request('/api/rule/import', {'payload':{'version':2,'rules':[rule]},'enabled':True})
        assert imported['imported'] == 1
        incoming = [r for r in request('/api/export/rules')['rules']
            if r['id'] not in {rule['id'],second_rule['id']}]
        assert len(incoming) == 1 and incoming[0]['enabled'] is True
        request('/api/rule/delete', {'ids':[incoming[0]['id']],'equipment_key':'LegendaryBelt3'})
        assert request('/api/export/rules')['rules'] == originals
        assert not request('/api/state')['monitoring'] and not request('/api/state')['automation']['running']
        report['checks'].append('新版组合脚本已内置；跨装备复制、去重、批量删除及启用导入通过隔离配置验证')
        logs_before = request('/api/logs?category=settings')['total']
        assert logs_before >= 3
        assert request('/api/state')['rules'][0]['enabled'] is False
        report['checks'].append('同件装备的两个可命名组合成功保存，导出保留全部组合；配置操作进入持久日志')
        # A second launch must attach to the same service and exit, leaving
        # the first instance's session and rule data intact.
        second_command = [str(exe), '--port', str(port)]
        if args.test_managed: second_command.append('--test-managed')
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
            preview = request('/api/loot-preview')
            assert preview['phase'] in {'active','waiting'}, preview['reason']
            if preview['available']:
                assert preview['run']['key'] and preview['waves']
                assert preview['totals']['items'] == sum(len(w['items']) for w in preview['waves'])
                assert preview['totals']['gold'] == sum(w['gold'] for w in preview['waves'])
            report['checks'].append('打包版只读取得当前在线计划或明确等待状态，物品与金币合计一致')
            report['game_pid'] = state['pid']
            report['equipment_count'] = len(state['items'])
            character = state['character']
            assert character['available'], character.get('reason')
            assert 0 < len(character['equipment']) <= 12
            assert character['comparison_distance'] == (7 if character['class_mask'] in (2,4) else 2)
            assert len(character['attributes']) >= 65 and character['attack'] > 0
            comparable = [item for item in state['items'] if item.get('power',{}).get('available')]
            assert comparable and all(item['power']['before'] == character['attack'] for item in comparable)
            assert all('Boss' in item['power']['note'] for item in comparable)
            assert all(isinstance(item['upgrade'],int) and isinstance(item['sockets'],int)
                and isinstance(item['gems'],list) and isinstance(item['tags'],list) for item in character['equipment'])
            report['checks'].append('单文件 exe 实机读取穿戴装备、强化、孔位、宝石及角色属性；所有换装比较使用同一固定 Boss 基准')
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
            request('/api/action', {'kind':'disconnect'})
            deadline = time.monotonic()+10
            while request('/api/state')['busy'] and time.monotonic()<deadline:
                time.sleep(.1)
            preview = request('/api/loot-preview')
            assert preview['run'] is None and not preview['waves'] and not any(preview['totals'].values())
            report['checks'].append('断开实际游戏后整份计划清空，无残留掉落')
        old_token = token
        finish()
        start()
        state = request('/api/state')
        assert len(state['rules']) == 2 and all(r['enabled'] is False for r in state['rules'])
        assert [r['name'] for r in state['rules']] == [rule['name'],second_rule['name']]
        assert request('/api/logs?category=settings')['total'] >= logs_before
        assert state['connected'] is False and state['monitoring'] is False
        assert state['automation']['settings'] == settings and not state['automation']['running']
        restored_managed = request('/api/managed')
        assert restored_managed['settings'] == saved_managed and not restored_managed['running']
        assert restored_managed['settings']['reconnect'] == {'enabled': True, 'character_index': 2}
        assert restored_managed['settings']['auction'] == saved_managed['auction']
        assert not restored_managed['settings']['auction']['sell_enabled']
        report['checks'].append('上架类别、强制保留名单和定价规则保存重启后保留；未登录不自动出售，不启动托管')
        report['checks'].append('断线重连模块与控件已内置，默认关闭；角色序号与开关保存重启后保留且不自动托管')
        assert request('/api/community')['enabled'] is True
        assert request('/api/community')['url'] == receipt_url
        assert request('/api/market/settings')['currency_id'] == 23
        report['checks'].append('战斗天赋、生活技能路径、技能组合及自定义93%/17%商人比例与购买件数，保存后重启完整保留')
        report['checks'].append('自定义托管分区、未解锁页选择与自动整理偏好重启后保留，默认方案不覆盖本地配置')
        check_reporting_disabled()
        report['checks'].append('重启后报告接口仍停用，未生成报告数据库')
        assert token != old_token
        report['checks'].append('退出后重启仍保留规则；不会自动连接或开启监控')
        report['checks'].append('自动整理选择与偏好重启后保留，仍默认暂停')
        report['checks'].append('多组合同名装备配置与 SQLite 日志重启后保留；日志无需外部 Python 或数据库程序')
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
        receipt_server.shutdown()
        receipt_server.server_close()
        receipt_thread.join(2)
    report['native_window'] = args.desktop
    report['managed_test_entry'] = args.test_managed
    report_path = args.report or ROOT / 'release' / ('native-exe-verification.json' if args.desktop else 'exe-verification.json')
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
