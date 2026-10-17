# Steam 市场价格

背包与仓库的市场价格来自 Steam 公开市场订单簿。此功能不登录 Steam，不读取登录 Cookie，不请求玩家的 Steam 库存，不下单、不出售、不交易。网络失败只影响报价显示，不参与托管调度或物品处理决策。

## 本地集成

```python
from deskrawl_assistant.steam_market import MarketCatalog, SteamMarketPrices

market = SteamMarketPrices(
    MarketCatalog.load(data_directory / 'steam-market-catalog.json'),
    cache_path=runtime_directory / 'steam-market-prices.json',
)
quote = market.for_item(raw_snapshot_row)  # 立即返回；后台排队读取
# 应用退出时：market.close()
```

每个进程共用一个 `SteamMarketPrices` 实例。仅为用户正在查看的背包/仓库道具请求报价，避免在托管循环里枚举全部物品。可以按选择 ID 向本机接口查询，由本机服务使用已读取的原始物品记录调用此模块；前端不得提交任意市场名称或网址。

- `eligible=false`：不支持、已绑定/穿戴，或绑定/物品身份尚未核实。不排队联网。
- `status=queued`：首次读取等待中。
- `status=ready`：已精确匹配该物品的公开订单簿。
- `lowest_sell`、`highest_buy`：货币主单位的精确十进制字符串，如 `"2.30"`。该侧没有订单时为 `null`，不得显示为零价。
- `SteamMarketPrices.for_item()` 中的 `currency` 是 Steam 实际响应的 ISO 币种，`currency_id` 是 Steam 币种 ID。新版 Steam 页面实测忽略 `currency=23` URL 参数，因此始终保留原价；显示币种换算在独立的 `MarketCurrency` 层完成，不写回 Steam 报价缓存。
- `fetched_at` 是 Unix 秒，`fetched_at_utc` 为 UTC 文本；过期的成功报价保留 `status=ready`，同时 `stale=true`。失败刷新保留原报价日期，附 `refresh_error`。
- `market_url` 只能是已经过字典解析的 Steam 官方物品页面。前端打开应走助手现有的外部链接流程。

价格是该市场物品种类的当前订单总价，包含 Steam 市场手续费；不是对单件随机词条、等级或解封后属性的估价。最高求购不是历史成交中位数，也不是预估出售净收入。

## 显示币种与可交易筛选

背包与仓库工具栏提供显示币种选择，默认“自动（Steam 返回币种）”。人民币、美元、港币、欧元等选择仅用于**参考换算**，不会改变 Steam 钱包、订单或市场实际币种。选择后自动独立保存到配置目录的 `steam-market.json`，不覆盖正在编辑的规则、托管或整理设置。

- `GET /api/market/settings` 返回 `currency_id` 与当前支持的 `currencies`；`0` 表示原币种。`POST /api/market/settings` 仅接受 `{ "currency_id": 23 }` 一类的已知整数设置。两者需要本机会话令牌，客户端不能提供汇率 URL 或市场名称。
- `POST /api/market/quotes` 仍仅接受物品选择 ID。响应附配置 `currency_id`，前端切换币种会清空旧报价并忽略在途旧回执；若其他窗口修改了设置，则重新读取设置。保存请求丢失回执时也先读回设置，不猜测保存是否成功。
- 报价响应缺少或损坏币种配置时显示接口兼容提示，至少间隔 10 秒重试；配置读回成功也不会触发无间隔请求循环。
- 通过 [Frankfurter v2](https://frankfurter.dev/) 的固定 HTTPS 地址获取单一 ECB 来源的 EUR 汇率批次：`/v2/rates?base=EUR&providers=ECB`。参考价 = Steam 原金额 × 目标币种 EUR 汇率 ÷ 原币种 EUR 汇率。只提供当前 ECB 覆盖且有 Steam 币种 ID 的选项，不将历史币种元数据当作可用汇率。
- 共用一个懒加载后台工作线程与本地 `steam-market-rates.json` 缓存，成功缓存 1 小时、失败退避 15 分钟；同一批次日期必须一致，拒绝未来日期、重复币种及非正有限值。周末沿用上一交易日；缓存过期注明“缓存汇率”，汇率日期超过 7 天不再用于换算。无需密钥，不发送玩家、账号或物品数据给汇率服务。
- 换算结果以两位小数四舍五入，显示“参考价”和“≈”；详情保留 `original_currency`、`original_lowest_sell`、`original_highest_buy` 与 `exchange_rate_date`。无对应订单仍是 `null`。汇率不可用时清空目标币种金额，详情仍显示原币种报价，避免把旧金额换一个币种标签。原币种与所选币种相同时不查询汇率。

“可交易”按可上架 Steam 市场判断：本地物品字典有对应市场定义，装备/符文绑定状态明确为未绑定且未穿戴，物品身份无读取问题。筛选不等待网络报价，支持装备与材料，并可与其他条件组合；清除筛选会取消此勾选，但保留已保存的显示币种。绑定/身份未知的物品不当作可交易，具体能否上架仍以游戏和 Steam 为准。

## 候选映射

`tools/extract_steam_market_catalog.py` 从已验证游戏资源的 `SteamItemDefId`、`BlackMistSteamItemDefId` 和英文名称生成 `data/steam-market-catalog.json`，不读取账号或存档。当前 1.0.3 资源有 229 个 ItemData 带有正数 Steam 定义，包含仍可能在旧玩家库存中的 retired 定义。

神圣、黑雾、宏伟/套装符文、金条、开光石和传说缰绳均可由其原生定义成为候选。普通传说装备没有普通市场定义时，不会借用黑雾价格。装备与符文的实例 `bound` 必须明确为 `false`；未知绑定状态不当作可交易。

候选名称仍必须与 Steam 响应中的 `appid=4623570`、`market_hash_name`、`marketable` 逐项匹配，装备额外匹配 `[Divine]` / `[Black Mist]` 类型。名称不匹配、结构变更和缺少订单簿时显示未匹配，不猜测价格。神圣/宏伟/套装名来自游戏英文名称；黑雾名称附加 Steam 页面已验证的 ` (Black Mist)` 后缀；缰绳使用游戏自己的 `Rein of the {slave}` 本地化模板，最终同样接受 Steam 精确核验。

## 只读证据与接口限制

2026-10-17 匿名读取验证：

- [游戏的 Steam 市场](https://steamcommunity.com/market/search?appid=4623570) 对应 Deskrawl: Idle ARPG。
- [Gold Bar](https://steamcommunity.com/market/listings/4623570/Gold%20Bar) 页面 `valve-ssr-data.renderContext.queryData` 包含 `['market','description',4623570,'Gold Bar']` 和 `['market','orderbook',4623570,'Gold Bar']`。实测返回 `eCurrency=29`（HKD）、最低在售 `2.33`、最高求购 `2.27`；价格会随市场变化。
- [Boots of the Windcaller (Black Mist)](https://steamcommunity.com/market/listings/4623570/Boots%20of%20the%20Windcaller%20%28Black%20Mist%29) 匿名同样可读，类型 `[Black Mist]`，实测 HKD 最低在售 `9.92`、最高求购 `6.69`。
- 新版公开页面的订单簿字段为 `amtMinSellOrder` / `amtMaxBuyOrder`，金额以货币单位的百分之一表示；计数来自 `cSellOrders` / `cBuyOrders`。解析只读取这些值，完全不读取 `price_median`。
- 已核对 Steam 自身的[金额格式化源码](https://cdn.fastly.steamstatic.com/steamcommunity/public/ssr/CSYhyKu3.js)：订单簿对所有币种统一使用百分之一单位，JPY / KRW / VND 的整元标记仅影响显示，KWD 也使用两位。不能按 ISO 的默认小数位重新缩放。隔离执行原格式化函数验证 JPY `41000` → `410`、VND `1400000` → `14000`、KWD `123` → `1.23`；这些值只验证单位，不代表当前报价。
- 旧 `/market/search/render/` 在本机只读请求时返回 HTTP 429；本实现不依赖旧搜索或价格概览，也不使用历史价格来补齐求购价。

这些页面数据是 Steam 自己前端使用的公开数据结构，**不是 Valve 承诺稳定的第三方报价 API**。旧界面、区域差异或后续 Steam 更新可能无法返回同一 SSR 结构，此时降级为无报价。

[Steam 官方物品 schema](https://partner.steamgames.com/doc/features/inventory/schema) 区分 `marketable` 与 `tradable`：可市场出售和可直接交易不是同一标记。本功能验证 `marketable`。[官方 IEconMarketService](https://partner.steamgames.com/doc/webapi/IEconMarketService) 是合作伙伴受限 API，需要发行商密钥，因此本客户端不调用此接口，也不要求用户提供密钥。

默认每个助手进程一个 HTTP 工作线程、请求间隔 15 秒，成功报价缓存 5 分钟，未匹配负缓存 30 分钟，普通失败至少等待 60 秒。403/429 使该实例全局至少退避 5 分钟，并遵守数字形式的 `Retry-After`（上限 1 小时）。缓存和排队都有上限；退出立即取消未发出的查询。报价缓存在本地运行目录保存，不影响托管调度。
