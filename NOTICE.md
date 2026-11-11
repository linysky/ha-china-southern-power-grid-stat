# 来源、修改与许可证

- 代码来源：https://github.com/benj-tang/china_southern_power_grid_stat
- 基准提交：`8900a45bd848407d5b343981e29c89d92a8177ca`（v1.4.0，2026-08-04）。
- 二次来源：https://github.com/mintisan/home-assistant-shenzhen-utilities（2026-11-05）；该仓库在 `csg_client/__init__.py` 增加了「日数据查询返回 `sta=02` 时改走日历接口」的回退，月度数据与日用电读取共用该回退，其余文件与基准提交逐字节一致。
- 本仓库 2026-11-11 从上述集合仓库拆分为独立集成仓库，仅补充发布元数据（hacs.json、manifest 元数据、README/NOTICE/许可证），未改动业务逻辑。
- 许可证：GNU General Public License v3.0（见 LICENSE、licenses/CSG-GPL-3.0.txt）。被修改的 `csg_client/__init__.py` 头部带有修改标注（GPL-3.0 §5(a)）；上游作者/维护者信息见上游仓库与 git 历史。

本集成的数据来源为官方小程序/网页接口，接口可能变化，请以官方账单为准。
本仓库不含任何账户凭据、账单数据或个人配置。
