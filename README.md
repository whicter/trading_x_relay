# trading_x_relay — X 博主点位中继

独立小服务（决策来源：`quantrift_index_future/strategy_explore.md` §A，2026-08-02 定稿，
2026-08-06 用户拍板施工）。抓取指定博主的 X 帖子 → 启发式分类（指数/个股/其他）→
SQLite 全量落库 → **只把指数类推送 Telegram**。

## 免登录（2026-08-06 实测确定的路线）

X 未登录时 profile 页是降级渲染，但它带 **schema.org 微数据**
（`itemprop=identifier / datePublished / articleBody / author / ImageObject`）——
比登录后的 React DOM（`data-testid`）**更干净也更稳定**，因为那是给搜索引擎看的
结构化数据，正文完整、时间戳精确到秒、无 UI 噪声。

> 起初走的是「人工登录一次 + 持久化 profile」，但 Playwright 的 Chromium 会被
> Google/X 的反自动化拦住（用户实测连 Gmail 都登不了）。改走免登录后，
> **凭据、账号封禁、session 过期、登录反爬这四个问题一起消失。**

**代价（唯一的）**：每账号每轮只能看到约 5 条最新帖，且下滑不加载更多。
靠 15 分钟轮询覆盖；`saturation` 检测负责在可能漏帖时显式告警。

两个实现细节，改代码时别踩：
- `wait_for_selector` **必须 `state="attached"`**——微数据是隐藏 `<meta>`，
  默认的 `"visible"` 永远等不到（实测把整轮抓取误判成结构失效）。
- 微数据解析**必须排除 `[itemprop="author"]` 子树**——作者块里也有
  `identifier`/`url`/`image` 同名字段，不排除会把作者 id 当帖子 id。

## 定位与铁律

- **信息中继 + 决策支持，不是 alpha**。不做预测、不下单。
- X 内容是**不可信输入**：可推送、可入库打分，**绝不进入任何下单路径**。
  本仓库不 import `ib_insync`、不占 clientId（`tests/test_x_relay.py` AST 扫描强制）。
- **静默失败显性化**：拿不到微数据（X 改版/被拦）→ 带冷却 TG 告警；
  连续 3 轮全账号失败 → 告警；窗口打满可能漏帖 → 告警。
- 定位是 **4-8 周验证探针**（回答「这些博主准不准」，供后续打分系统 A.10），
  不是长期管道。选择器脆弱是已接受的成本。

## 组件

| 文件 | 职责 |
|---|---|
| `x_relay.py` | 主引擎：Playwright 免登录抓取 + 微数据解析、轮询循环、TG 推送、告警 |
| `classifier.py` | 纯函数分类器：指数关键词/个股信号/点位数字抽取 |
| `store.py` | SQLite WAL append-only 台账（`runtime/x_posts.sqlite3`）：帖子原文永不修改 |

分类规则要点（全部有实测样本单测）：
- 混合帖按指数处理（宁多推不漏）；指数 cashtag（`$spx`/`$ndx` 小写）算指数关键词
- `assume_index` 账号（Mancini/Dayu/novicetrader888）纯数字帖也判指数点位；
  但**必须有点位或指数关键词才推送**——纯叙述帖只落库（避免推「我没有微信群」这类噪声）
- 不是点位的数字会被排除：年份、百分比、涨跌幅（"400+ point rally"）、
  指数名自带数字（"S&P 500" 的 500、"Russell 2000" 的 2000）、`< 600`
- 转发不推；发布超 12h 不推（防首轮回填刷屏）
- 微数据里 `&` 是双重编码（`S&amp;P 500`），解析时 `html.unescape`

## 五个账号的实测形态（2026-08-06 首轮 25 条帖）

| 账号 | 实测内容 | 分类分布 |
|---|---|---|
| `@AdamMancini4` | `#ES_F` 完整点位，每 1-2 小时一帖，**不是「see newsletter」钩子** | 4 index_levels + 1 other（2022 置顶）|
| `@Investor_Dayu` | `$spx $ndx` 波浪 + 明确点位与自报进出场 | 4 index_levels + 1 index_view |
| `@novicetrader888` | 中文指数评论，含点位（"ES 8000"）| 2 index_levels + 3 other |
| `@willem82457275` | S&P500 自制指标，**点位全在图里** | 4 index_view + 1 other |
| `@time_and_trade` | **加密代币推广**（Interlink/StarX/PACT）| 3 stock + 2 other，**0 指数** |

`@time_and_trade` 不删——留作分类器**负样本对照**：它的帖子若出现在推送里，
说明分类器漏了。整体「有图无数字」占 24%（A.9 要求的显式统计）。

## 使用

```bash
cd ~/Documents/trading_x_relay
venv/bin/python3.11 x_relay.py --once --dry-run # 抓一轮只打印（验证用）
venv/bin/python3.11 x_relay.py --once           # 抓一轮并推送
venv/bin/python3.11 x_relay.py --loop           # 常驻轮询（15min ± 2min）
venv/bin/python3.11 x_relay.py --stats          # 台账分布（含「有图无数字」占比）
venv/bin/python3.11 -m unittest discover -s tests   # 单测（38 例）
```

## 部署

```bash
# TG_TOKEN 从已有进程环境取，不落盘（同主仓库模式）。
# 可选 X_RELAY_TG_CHAT_ID 指定独立 chat，缺省回落 TG_CHAT_ID。
cd ~/Documents/trading_x_relay
TG_TOKEN=... TG_CHAT_ID=... PATH=/opt/homebrew/bin:$PATH \
  pm2 start venv/bin/python3.11 --name x-levels-relay -- x_relay.py --loop
pm2 save
```

主仓库 CLAUDE.md「规则 2」同样适用：部署后须在
`quantrift_index_future/health_watchdog.py::BOTS` 登记（心跳文件
`runtime/heartbeat.json`，绝对路径），否则进程挂掉无人知道。

## 已知限制

1. **每轮 5 条上限**：高频账号可能漏帖，`saturation` 告警会点名（首轮 bootstrap 不算）。
2. **图片里的点位抓不到**（当前 24%）：推送标 🖼 提示，`--stats` 跟踪占比，
   据此再决定是否上 vision。
3. **标的换算未做**：SPX ≠ ES ≠ SPY×10（fair value basis 漂移）。
   推送的是博主原话数字，**不能直接当 MNQ/MES 挂单价用**。
4. 分类器 v1 是启发式；分错只影响推送不影响台账，历史行可用升级后的
   分类器重打标签回放验证。
5. **打分系统（A.10）尚未做**——这才是「能长期活下去的唯一理由」，
   本服务只是它的数据采集端。
