# 南方电网电费统计 · Home Assistant 自定义集成

把南方电网账户数据接入 Home Assistant。非官方社区集成，不隶属于供水/供电/燃气公司；接口可能变化，请以官方账单为准。

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=linysky&repository=ha-china-southern-power-grid-stat&category=integration)

> **v1.6.0 起主线已合并社区增强版**：修复南网接口业务码 `02` 导致的电费实体 `unknown`，
> 新增历史每日电费查询、阶梯计价、独立查询页与 Lovelace 日历卡片。
> 增强版来源与 GPL 修改记录见 [NOTICE.md](NOTICE.md)。
> 静态资源注册要求 HA 2024.6 及以上（已在 `hacs.json` 声明最低版本）。

## 功能

| 组件 | 说明 |
| --- | --- |
| 集成 | 沿用原集成的扫码登录与 16 个实体；修复南网接口业务码 `02` 导致的电费实体 `unknown` |
| 每日电费历史 | 真实每日电量 + 阶梯电价，按官方月账单总额校准，每月合计精确等于账单；历史只增不删 |
| 服务 | `china_southern_power_grid_stat.query_daily_cost`，可按日期区间查询每日电量/电费 |
| 查询页 | `/csg_stat/csg_daily_cost.html`，支持账户、日期区间、来源标记、CSV 导出 |
| 卡片 | `/csg_stat/csg-power-card.js`，日历每格同时显示电费与电量（1 位小数）；南网同款 6 档用电量配色；底部「详细信息」按钮直达查询页 |

## 安装

[![Open your Home Assistant instance and open a repository inside the Home Assistant Community Store.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=linysky&repository=ha-china-southern-power-grid-stat&category=integration)

点击徽章可一键把本仓库添加到 HA 的市场（旧版 HACS 同理），或按下面的步骤手动添加。

- **HA 2026.11+ 内置市场（Marketplace）**：设置 → 市场 → 右上角「从链接添加」→ 粘贴 `https://github.com/linysky/ha-china-southern-power-grid-stat` → 安装。首次使用需连接 GitHub（设备登录，只读公开信息），并接受市场的社区项目风险提示。
- **HACS 自定义存储库**：HACS → 右上角菜单 → 自定义存储库 → 填仓库 URL、类别选 Integration → 添加 → 搜索「南方电网电费统计」→ 下载 → **重启 Home Assistant**。
- **手动安装**：把 `custom_components/china_southern_power_grid_stat/` 复制到 HA 的 `/config/custom_components/`（Docker 对应自己的持久化挂载目录，注意目录层级不要多嵌套一层），重启 HA。

装完重启后：设置 → 设备与服务 → 添加集成 → 搜索「南方电网电费统计」→ 按集成流程扫码登录。
**只下载文件、不登录，是不会有实体和数据的。**

> 本集成 domain 为 `china_southern_power_grid_stat`，是旧版/社区增强版的替换版；
> 请勿同时安装两份，否则会互相覆盖。

## 配置与凭据获取

见 [docs/electricity.md](docs/electricity.md) 与 [docs/security.md](docs/security.md)。凭据只能从本人已授权的官方小程序/App 获取；本仓库不含任何账户凭据或账单数据。

## 给使用者：怎么把日历卡片加到仪表盘

> 这一步是在**下载并安装集成的那台 HA** 上操作，全程不用命令行，约 3 分钟。
> 前提已完成：HACS 下载 → **重启 HA** → 设置 → 设备与服务 → 添加集成 → 扫码登录。
> 只下载文件、不登录，是不会有实体和数据的。

### 第 1 步：登记卡片资源（每台 HA 只需做一次）

设置 → 仪表盘 → 右上角 ⋮ → **资源**（Resources）→ **添加资源**：

| 字段 | 填什么 |
| --- | --- |
| URL | `/csg_stat/csg-power-card.js` |
| 资源类型 | **JavaScript 模块**（module） |

保存后按 `Ctrl` + `F5` 强制刷新浏览器。若仪表盘是 YAML 模式
（不是「由 Home Assistant 管理」），则在 `ui-lovelace.yaml` 顶层加：

```yaml
resources:
  - url: /csg_stat/csg-power-card.js
    type: module
```

> 资源用相对路径，换 HA 地址、HTTPS、手机 App 都不用改。
> 若打开 `/csg_stat/csg-power-card.js` 是 404：多半是装完**没重启 HA**，或 HA 版本低于 2024.6。

### 第 2 步：查自己的实体 ID

开发者工具 → **状态** → 搜索框输入 `this_month_total_usage`，
会看到形如 `sensor.1234567890123456_this_month_total_usage` 的实体（本月用电量），
**把整串复制下来**。每个人的缴费号不同，不能直接照抄示例。

> 注意：不要复制 `_this_year_total_usage`（本年累计）。填了它卡片会切成「年度视图」，
> 日历变成 12 个月的格子、翻月按钮变灰，看不到每天的用电量。

### 第 3 步：用「手动卡片」添加（最容易卡住的一步）

这张卡片**没有图形配置界面，不会出现在「添加卡片」的卡片选择器里**，
必须走 YAML 编辑器：

1. 打开目标仪表盘 → 右上角 **编辑仪表盘** → **添加卡片**。
2. 拉到列表最底部，选 **手动 / Manual**（部分版本叫「YAML 卡片编辑器」或「显示代码编辑器」）。
3. 把下面整段粘进去，只把 `entity` 换成第 2 步复制的实体 ID：

```yaml
type: custom:csg-power-card
entity: sensor.1234567890123456_this_month_total_usage
name: 南方电网电费日历
```

4. 点 **保存**，再点右上角 **完成**。

> `entity` **必填**：不写或写错，卡片会一直空白。卡片会自动从实体 ID 推出户号，
> 配置里不需要写账号、密码或缴费号。
> 兼容旧名称 `custom:custom-power-card-v47` / `custom:custom-power-card-v48`。
> v1.5.3 起，实体 ID 被改成拼音 / 中文、或配置里的 ID 已失效时，卡片会自动定位数据源；
> 但为兼容自动化与历史统计，仍建议保持集成生成的英文实体 ID，只改「名称」显示中文。

### 第 4 步：等数据生成

卡片读的是集成自动写入的 `/config/www/csg_daily_cost_history.json`。
首次登录后会在后台逐月抓取历史（几分钟到十几分钟），期间显示「暂无数据」属正常；
也可在开发者工具 → 操作里调用
`china_southern_power_grid_stat.query_daily_cost` 触发一次抓取。

查询页：`http://<你的HA地址>/csg_stat/csg_daily_cost.html`；
卡片底部「详细信息」按钮默认就跳到这里，不用另外配置。

### 可选参数

```yaml
type: custom:csg-power-card
entity: sensor.1234567890123456_this_month_total_usage
account: 1234567890123456                        # 多账户时指定，单账户不用写
detail_url: /csg_stat/csg_daily_cost.html        # 「详细信息」按钮目标
tier_thresholds: [5, 10, 15, 20, 25]            # 日用电量 6 档色阶阈值（度）
entity_prefix: sensor.1234567890123456_          # 实体命名特殊时手工指定前缀
```

### 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| 添加卡片里搜不到这张卡 | 正常，卡片无图形编辑器；按第 3 步选「手动 / Manual」 |
| 卡片空白、只有标题 | `entity` 没填或填错；按第 2 步核对实体 ID |
| 日历是 12 个月格子、看不到每天用电、翻月按钮是灰的 | `entity` 填成了 `_this_year_total_usage`；改成 `_this_month_total_usage` |
| 查询页有每天耗电、卡片当月空白 | 当月日历读集成实体的本月每日明细，历史月份才读缓存 JSON；点「◀ 上月」能显示历史月即属当月尚未同步，等一次刷新或重启 |
| 资源 404 | 装完没重启 HA，或 HA < 2024.6；重启后再试 |
| 实体 ID 被改成中文 / 拼音后「本月」空白 | v1.5.3 起卡片会自动识别（含配置里旧 ID 失效的情况）；仍建议在 设置 → 设备与服务 → 实体 里把「实体 ID」改回英文，只改「名称」显示中文即可 |
| 一直「暂无数据」 | 未完成扫码登录，或历史仍在后台抓取；等几分钟或调用 query_daily_cost |
| 某些历史月份没数据 | 该月电网未返回每日日历，属数据源限制 |
| 夏天整月偏橙红 | 南网按**绝对度数**分档，用电高属正常；可用 tier_thresholds 调整 |

日历配色对齐南方电网小程序「智慧用电」，按**绝对日用电量**分 6 档固定色
（不随当月最大/最小值浮动），色值取自官方 App 截图：

| 日用电量 | 颜色 | 色值 |
| --- | --- | --- |
| < 5 度 | 亮绿 | `#7AEB81` |
| 5–10 度 | 黄绿 | `#A6E065` |
| 10–15 度 | 金黄 | `#F7D04F` |
| 15–20 度 | 橙 | `#EA8D3D` |
| 20–25 度 | 橙红 | `#E46435` |
| ≥ 25 度 | 红 | `#DE472C` |

> 静态资源随集成一起安装，由集成在启动时注册到 `/csg_stat`，**无需手动往 `/config/www` 搬文件**。
> 若 `/csg_stat` 无法访问（极旧版本 HA），可把 `custom_components/china_southern_power_grid_stat/www/`
> 下两个文件复制到 `/config/www/`，然后改用 `/local/csg_daily_cost.html`
> 与 `/local/csg-power-card.js`，并在卡片里加 `detail_url: /local/csg_daily_cost.html`。

## 数据在哪

- 缓存文件：`/config/www/csg_daily_cost_history.json`，由集成自动生成、原子写入。
  首次启动后会后台逐月抓取历史（可能几分钟），期间查询页会显示加载失败，稍后刷新即可。
- `sensor.py` 顶部 `KEEP_ALL_HISTORY = True`：历史只增不删。
  设为 `False` 则只保留近 `DAILY_HISTORY_YEARS` 年。
- 阶梯参数默认按广东居民（夏季 260/600、非夏季 200/400 度，加价 0/0.05/0.30 元）。
  其他地区请修改 `TIER_THRESHOLDS_*` / `TIER_SURCHARGES`，并把 `ALGO_VERSION` **加 1**
  以触发旧缓存全量重算（会复用已抓到的每日电量，不重复请求南网）。

> 缓存 JSON 写在 `/config/www/`，通过 HA 的 `/local/` 静态路径提供。**`/local/` 默认不要求登录**，
> 缓存内容包含真实缴费号与每日用电量/电费。若 HA 有公网映射或远程访问入口，请避免直接暴露，
> 或在 `sensor.py` 顶部把 `WRITE_DAILY_HISTORY_JSON` 设为 `False`（查询页/卡片将无数据）。

## 隐私

本仓库**不含**任何账号、密码、缴费号、电费账单数据、Token、内网 IP 或本机路径；
所有个人数据都在使用者自己的 Home Assistant 内生成并保存，不会上传到任何地方。
示例中的户号 `1234567890123456` 是占位符。

> 但请注意：使用者 HA 内生成的缓存文件位于 `/local/`，该静态路径默认不要求登录；
> 若 HA 有公网映射或远程访问入口，请勿直接暴露（详见「数据在哪」）。

## 更新记录

- **v1.6.0**：主线合并社区增强版 zerostr-123/ha-csg-enhanced（v1.5.3）的修复与卡片/查询页；
  保留原版文档与安全须知，元数据改回本仓库，版本跳 1.6.0 以区分对方独立仓库的版本号。

- **v1.5.3**：卡片不再依赖「英文实体 ID」—— 实体 ID 被改成中文 / 拼音，
  或卡片配置里填的旧 ID 已失效时，会按实体注册表原名与集成独有属性自动定位数据源；
  找不到电费实体时，用每日明细里的真实分摊电费求和。英文 ID 的原有行为不变。

- **v1.5.2**：电费日历卡片在手机窄屏下改为按宽度自适应，7 列日历不再溢出/被裁切；
  「本年累计」跟随翻页年份变化（翻到 2025 年显示 2025 年累计，标签同步）；
  修正文档中 `entity` 示例为「本月用电量」`this_month_total_usage`，
  并补全「卡片怎么加」的分步说明与常见问题。

## 注意

- `unknown`/`unavailable` 不等于 0，不要用补零掩盖认证或网络故障；月度账单不是连续累计电表读数，不要配成 HA Energy 的累计传感器。
- 凭据（token / CodeId / 会话 JSON）明文存于 HA `.storage`，请勿公开配置备份；分享日志前必须脱敏。

- 本仓库最初从 https://github.com/mintisan/home-assistant-shenzhen-utilities 集合仓库拆分为单集成，**包含该仓库的补丁**；v1.6.0 起另合并了社区增强版补丁，上游单集成版本不一定含这些补丁。完整来源与修改范围见 [NOTICE.md](NOTICE.md)，许可证：GPL-3.0。

## 许可

GPL-3.0，见 [LICENSE](LICENSE) 与 [NOTICE.md](NOTICE.md)。
