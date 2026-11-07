from html import escape
from deskrawl_assistant.steam_market import APP_ID
from deskrawl_assistant.steam_return_receipts import CANCEL_TEXT


def event(number, *, assetid='2', description=CANCEL_TEXT, classid='42', amount=1,
          steamid='76561198000000001'):
    href = f' href="https://steamcommunity.com/profiles/{steamid}/inventory/#{APP_ID}_2_{assetid}"' if assetid else ''
    return (f'<div class="tradehistoryrow"><div class="tradehistory_date">6 Nov, 2026'
        f'<div class="tradehistory_timestamp">8:59am</div></div><div class="tradehistory_content">'
        f'<div class="tradehistory_event_description">{escape(description)}</div>'
        f'<div class="tradehistory_items"><div class="tradehistory_items_group">'
        f'<a id="history{number:040x}_item0" class="history_item economy_item_hoverable" '
        f'data-appid="{APP_ID}" data-classid="{classid}" data-instanceid="0" data-contextid="2" '
        f'data-amount="{amount}"{href}><img src="test.png"><span class="history_item_name">Gold Bar</span>'
        '</a></div></div></div></div>')


def history_page(*events):
    return '<div id="inventory_history_table">' + ''.join(events) + '</div>'


BASELINE = event(1, assetid=None, description='You listed an item on the Community Market.')
