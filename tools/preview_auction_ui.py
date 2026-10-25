"""Offline auction layout fixture; no AssistantService, game or Steam requests."""
import argparse
from http.server import ThreadingHTTPServer

import preview_growth_ui as growth


SCRIPT = growth.FIXTURE_JS.replace(
    "catalog:{equipment:[],equipped_slot_options:[]}",
    """catalog:{equipment:[],equipped_slot_options:[],auction_items:[
      {key:'DivineBow1',name:'神圣长弓',kind:'equipment',quality:'divine'},
      {key:'LegendaryBoots1',name:'黑雾传说靴子',kind:'equipment',quality:'black_mist'},
      {key:'HunterSetRune2',name:'二连矢',kind:'rune',quality:'set'},
      {key:'GrandRune_A',name:'宏伟符文',kind:'rune',quality:'grand'},
      {key:'Rein_Legendary',name:'传奇缰绳',kind:'rein',quality:'legendary'},
      {key:'GoldBar',name:'金条',kind:'material',quality:'legendary',
       support_reason:'无实例 ID 的金条／开光石暂需手动上传。'}]}"""
).replace(
    "if(route==='managed/settings')packet.settings=payload.settings;",
    """if(route.startsWith('auction/'))return {login_available:false,
      login_reason:'离线布局预览，不提供 Steam 登录或交易接口。',
      steam:{authenticated:false,bound:false},
      upload:{message:'离线布局预览 · 未执行游戏上传',listing_count:2,listing_limit:8},
      sell:{message:'离线布局预览 · 未提交 Steam 卖单',confirmation_count:1},error:''};
    if(route==='managed/settings')packet.settings=payload.settings;"""
).replace(
    "const controls=document.createElement('nav');",
    """const fold=document.querySelector('[data-managed-fold="auction"]');
    fold.open=true;fold.scrollIntoView({block:'start'});
    const controls=document.createElement('nav');"""
).replace(
    "controls.innerHTML='<button",
    "controls.hidden=true;controls.innerHTML='<button"
)


class Handler(growth.Handler):
    def do_GET(self):
        if self.path == '/fixture.js':
            if not self.valid_host():
                return self.reply({'error': '仅限本机访问。'}, 403)
            return self.reply(SCRIPT, content_type='text/javascript; charset=utf-8')
        return super().do_GET()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=18937)
    args = parser.parse_args()
    ThreadingHTTPServer(('127.0.0.1', args.port), Handler).serve_forever()
