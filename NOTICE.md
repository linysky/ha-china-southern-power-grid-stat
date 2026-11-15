# 来源、修改与许可证

- 代码来源：https://github.com/benj-tang/china_southern_power_grid_stat
- 基准提交：`8900a45bd848407d5b343981e29c89d92a8177ca`（v1.4.0，2026-08-04）。
- 二次来源：https://github.com/mintisan/home-assistant-shenzhen-utilities（2026-11-05）；该仓库在 `csg_client/__init__.py` 增加了「日数据查询返回 `sta=02` 时改走日历接口」的回退，月度数据与日用电读取共用该回退，其余文件与基准提交逐字节一致。
- 本仓库 2026-11-11 从上述集合仓库拆分为独立集成仓库，仅补充发布元数据（hacs.json、manifest 元数据、README/NOTICE/许可证），未改动业务逻辑。
- 2026-11-15 合并社区增强版 zerostr-123/ha-csg-enhanced（v1.5.3）的改动，详见下节。
- 许可证：GNU General Public License v3.0（见 LICENSE、licenses/CSG-GPL-3.0.txt）。被修改的 `csg_client/__init__.py` 头部带有修改标注（GPL-3.0 §5(a)）；上游作者/维护者信息见上游仓库与 git 历史。

## 社区增强合并记录（2026-11-15，v1.6.0）

来源：https://github.com/zerostr-123/ha-csg-enhanced（v1.5.3，GPL-3.0，作者 zerostr-123），
为版本 1.4.0.post1 的衍生版本。原作者 linysky 的版权声明与许可在此保留。

相对 1.4.0.post1 的修改：

1. `csg_client/__init__.py`：南网 `charge/queryDayElectricByMPoint` 返回业务码 `02` 时，
   增加参数组合探测与官方月账单 `charge/getAnalyzeFeeDetails` 降级，
   修复 6 个电费/阶梯实体恒为 `unknown`；阶梯三项按本地规则估算。
2. `sensor.py`：新增每日电费历史构建（阶梯电价反解第一档基准价、最大余数法分摊尾差、
   每月合计逐分钱等于官方账单、缓存只增不删、算法版本升级自动重算、
   账单为 0 元的月份保留真实每日电量），新增服务
   `china_southern_power_grid_stat.query_daily_cost`。
3. `__init__.py`：注册随集成分发的静态资源目录 `/csg_stat`。
4. `services.yaml` / `www/csg_daily_cost.html` / `www/csg-power-card.js`：
   服务描述、独立每日电费查询页（含 CSV 导出）与 Lovelace 电费日历卡片。
5. `manifest.json` / `hacs.json` / `README.md` / `NOTICE.md` / `info.md` / `brand/icon.png`：
   发布元数据与文档。

合并时保留本仓库原有文件：`.gitignore`、`csg_client_demo.py`、
`custom_components/china_southern_power_grid_stat/LICENSE`、`docs/electricity.md`、
`docs/security.md`、`licenses/CSG-GPL-3.0.txt`；`codeowners` / `documentation` /
`issue_tracker` 改回本仓库地址，版本定为 1.6.0。

所有修改均以 GPL-3.0 提供；修改后的源码（Corresponding Source）即本仓库全部内容。

## 免责声明

本项目为个人非官方作品，与中国南方电网有限责任公司无关。所依赖的南网接口为
私有接口，随时可能变更或失效；使用者需自行承担风险。仓库中不包含任何账号、
密码、缴费号、账单数据或内网地址。

本集成的数据来源为官方小程序/网页接口，接口可能变化，请以官方账单为准。
