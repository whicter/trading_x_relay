# trading_x_relay — X 博主点位中继

独立小服务（决策来源：`quantrift_index_future/strategy_explore.md` §A，2026-08-02 定稿，
2026-08-06 用户拍板施工）。抓取指定博主的 X 帖子 → 启发式分类（指数/个股/其他）→
SQLite 全量落库 → **除噪音外全部推送 Telegram**（2026-08-08 口径反转，见下）。

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
  **2026-08-08 起改为黑名单**：默认全推，只拦 `promo`（代币项目推广）/
  `chatter`（鸡汤、免责声明、账号杂事）/ `other`。标签仍细分
  `index_levels｜index_view｜commodity｜crypto｜stock｜macro`，只为消息头可读，
  不再决定去留
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

~~`@time_and_trade` 不删——留作分类器负样本对照~~ **2026-08-08 已剔除**
（14 条实抓里 12 条是代币推广，占用抓取预算且稀释信噪比。负样本对照改由
`tests/test_x_relay.py::BlacklistPushPolicyTest` 用真实原文固化，不必留活账号）。

原注：它的帖子若出现在推送里，
说明分类器漏了。整体「有图无数字」占 24%（A.9 要求的显式统计）。

## 使用

```bash
cd ~/Documents/trading_x_relay
venv/bin/python3.11 x_relay.py --once --dry-run # 抓一轮只打印（验证用）
venv/bin/python3.11 x_relay.py --once           # 抓一轮并推送
venv/bin/python3.11 x_relay.py --loop           # 常驻轮询（15min ± 2min）
venv/bin/python3.11 x_relay.py --stats          # 台账分布（含「有图无数字」占比）
venv/bin/python3.11 -m unittest discover -s tests   # 单测
```

### 常驻进程跑着的时候能不能跑 `--once`？

**能，不用先 `pm2 stop`。** 结构失效告警的文案就是让人跑 `--once --dry-run`
去诊断的——故障时人最需要它，而那恰恰是常驻进程还在跑的时候，所以这条
必须成立。2026-08-09 实测确认过两件事：

**① Chromium profile 不冲突。** `run_once` 和 `do_login` 都用同一个
`runtime/x_profile` 开 `launch_persistent_context`，直觉上会撞 Chromium 的
单例锁。实测不会：Playwright 的 Chromium **不创建 `SingletonLock`**，两个
上下文同时开在这个目录上都正常工作（无竞争基线 32s，故意占住锁的对照 39s，
均 4/4 账号成功、退出码 0）。而且锁本来也只在每轮那几十秒里才可能被持有——
`launch_persistent_context` 在 `run_once` 内部开、结束即 `ctx.close()`，
900 秒周期里占空比约 4%，两轮之间连浏览器进程都没有。

理论上两个 Chromium 共用一个 profile 仍不是好习惯（单例锁存在就是为了防这个），
但这里的爆炸半径是零：该 profile **只存 guest cookie、无任何凭据**，
中继本来就跑在未登录态（心跳里 `logged_in: false`），坏了删掉重建即可。

**② 但 `--once` 绝不能写心跳**（已修）。诊断跑一次会把主仓库的
`data/runtime/heartbeat/x-levels-relay.json` 刷成新鲜的 `connected=true`——
而 watchdog 对「pm2 显示 online、进程其实卡死」的唯一探测手段就是心跳新鲜度
（`health_watchdog.py::bot_health_failure`）。于是：进程卡死 → 心跳变旧 →
watchdog 正要告警 → 人按提示跑一次诊断 → 心跳被刷新 → 告警消失 → 人以为
「跑一下就好了」，卡死的进程继续没人管。现在 `--once` 传 `write_hb=False`，
只有 `--loop` 写心跳；心跳的语义是「**守护进程**还活着并在循环」，
一次性诊断不算。三条测试锁住这个约束（含反向的：`run_loop` 不许关心跳）。

> 注意 `--once`（不带 `--dry-run`）会**真的推送**并导出简报文件；
> `--dry-run` 则绝不写 posts 表——它若把帖子标成"已见过"，真正的循环之后
> 就再也不会推它们（2026-08-07 实测被一次 dry-run 吃掉过 10 条新帖）。

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
`quantrift_index_future/health_watchdog.py::BOTS` 登记，否则进程挂掉无人知道。

心跳写的是**主仓库**的 `data/runtime/heartbeat/x-levels-relay.json`
（纯 JSON 约定，双方不互相 import；路径可用 `QR_HEARTBEAT_DIR` 覆盖）。
本仓库的 `runtime/heartbeat.json` 是 2026-08-06 改走主仓库目录之前的遗留文件，
**早已不再更新，排查时别拿它当依据**。

## 2026-08-08：推送口径反转 + 修静默丢失

### 一、白名单 → 黑名单（用户决定）

原设计只推 `index_*`。实测 38 小时后代价显形：Dayu 14 条里 8 条是个股
（`$spcx` `$arqq` `$aaoi` `$gdx`）——**正是用户认为他准的那部分，全被丢弃**。

用户口径：「不论 stock / index 还是其他期货、黄金、白银、原油，包括对政策的
评论，只要和交易有关的都要」。BTC 行情观点要推，代币项目推广不要。

真实 54 条回放验证：**推 39 / 拦 15，拦下的全是噪音，零误杀**。

回放还救回一条会被漏掉的：novicetrader888 的「多头不用太紧张…连续短时间超买…
继续持多设好止损」是实打实持仓观点，但无标的、无数字，旧口径掉进 `other`。
已为 `assume_index` 账号加「有行情语义即算观点」的路径。

**加密必须分两类**（真实回放发现）：「Pact 现已支持 BTC、ETH 兑换」提到主流币
却没有 TGE/KYC 硬词，只靠 `_PROMO` 拦不住会被当行情推出去。现要求
主流币 **+ 行情语义**（支撑/目标/做多/浪…）才算观点，配 `_PRODUCT_NEWS` 兜底。

### 二、静默丢失（真 bug）

```python
if not store.insert_post(post):
    continue                  # 已见过 → 永不重试
if should_push(...):
    if tg_send(msg):          # 失败返回 False
        store.mark_pushed()   # 只有成功才标记
```

**帖子先入库、再发送；发送失败什么都不做。** 下一轮 `insert_post` 返回 False
直接跳过——那条帖永久丢失，**无重试、无告警**。实测吃掉 2 条 `index_levels`
（Mancini 08-06 15:55、Dayu 08-06 18:08），大概率是 bootstrap 突发推送撞上
Telegram 单 chat 限流（约 20 条/分钟）。

修法与主仓库 `alert_outbox` 同源：新增 `push_attempts` 列 + 轮末重试队列，
只有 2xx 才 `mark_pushed`，失败留队列；5 轮仍失败发告警（不再静默）；
补发放慢到 1 秒/条避开限流。

> 注意：先前排查中「漏推 8 条」里有 6 条其实是 `PUSH_MAX_AGE_HOURS=12`
> 时效门挡的（防首轮回填刷屏），属合理设计。真 bug 只有 2 条。

### 三、两个过程教训

- **19 条测试从来没跑过**：`if __name__ == "__main__"` 卡在文件中间，
  后面追加的测试类全被跳过。移到末尾后 49 → **68**。
- **漏改 `run_loop` 启动日志**（仍引用已删的 `PUSH_CLASSES`），重启即崩、
  pm2 反复重启 17 次。根因：单测全是纯函数，**没有一条覆盖启动路径**。
  已补 `StartupPathTest` 用 AST 符号检查兜住同类遗漏。

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
