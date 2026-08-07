# quantrift_x_relay — X 博主点位中继

独立小服务（决策来源：`quantrift_index_future/strategy_explore.md` §A，2026-08-02 定稿，
2026-08-06 用户拍板施工）。抓取指定博主的 X 帖子 → 启发式分类（指数/个股/其他）→
SQLite 全量落库 → **只把指数类推送 Telegram**。

## 定位与铁律

- **信息中继 + 决策支持，不是 alpha**。不做预测、不下单。
- X 内容是**不可信输入**：可推送、可入库打分，**绝不进入任何下单路径**。
  本仓库不 import `ib_insync`、不占 clientId（`tests/test_x_relay.py` AST 扫描强制）。
- **登录由用户人工完成一次**（Claude 不代输密码），凭据存本地 `runtime/x_profile/`（不入库）。
- **静默失败显性化**：登录失效 → 带冷却 TG 告警（附恢复命令）；连续 3 轮全账号失败 → 告警。
- 定位是 **4-8 周验证探针**（回答「这些博主准不准」，供后续打分系统 A.10），不是长期管道。
  选择器脆弱是已接受的成本；X ToS 风险已知（个人用途、低频轮询、只读）。

## 组件

| 文件 | 职责 |
|---|---|
| `x_relay.py` | 主引擎：Playwright 抓取（持久化 profile）、登录墙检测、轮询循环、TG 推送 |
| `classifier.py` | 纯函数分类器：指数关键词/个股信号/点位数字抽取（v1 启发式，够用再升级 LLM）|
| `store.py` | SQLite WAL append-only 台账（`runtime/x_posts.sqlite3`）：帖子原文永不修改 |

分类规则要点：混合帖按指数处理（宁多推不漏）；`assume_index` 账号
（Mancini/Dayu）纯数字帖也判指数点位；未知账号纯数字帖只落库不推送；
转发不推送；发布超 12h 不推送（防首轮回填刷屏）。

## 使用

```bash
cd ~/Documents/quantrift_x_relay
venv/bin/python3.11 x_relay.py --login          # 一次性：开有头浏览器人工登录
venv/bin/python3.11 x_relay.py --once --dry-run # 抓一轮只打印（验证用）
venv/bin/python3.11 x_relay.py --loop           # 常驻轮询（15min ± 2min）
venv/bin/python3.11 x_relay.py --stats          # 台账分布（含「有图无数字」占比）
venv/bin/python3.11 -m unittest discover -s tests   # 单测
```

## 部署（登录完成后）

```bash
# TG_TOKEN 从已有进程环境取，不落盘（同主仓库模式）。
# 可选 X_RELAY_TG_CHAT_ID 指定独立 chat，缺省回落 TG_CHAT_ID。
cd ~/Documents/quantrift_x_relay
TG_TOKEN=... TG_CHAT_ID=... PATH=/opt/homebrew/bin:$PATH \
  pm2 start venv/bin/python3.11 --name x-levels-relay -- x_relay.py --loop
pm2 save
```

主仓库 CLAUDE.md「规则 2」同样适用：部署后须在
`quantrift_index_future/health_watchdog.py::BOTS` 登记（心跳文件
`runtime/heartbeat.json`，绝对路径），否则进程挂掉无人知道。

## 已知限制（v1，均记录于 strategy_explore.md §A.9）

1. **图片**：点位在图里、文本无数字的帖会漏。推送里会标 🖼 提示；
   `--stats` 显式统计占比，据此再决定是否上 vision。
2. **标的换算未做**：SPX ≠ ES ≠ SPY×10（fair value basis 漂移）。
   推送的是博主原话数字，**不能直接当 MNQ/MES 挂单价用**。
3. 分类器 v1 是启发式；分错只影响推送不影响台账，历史行可用升级后的
   分类器重打标签回放验证。
