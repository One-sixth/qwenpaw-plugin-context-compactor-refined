# AGENTS.md — 项目约定

- 本项目目录内一切文件（README/MEMORY/代码/注释）不出现真实称呼，一律写「**作者**」（项目纪律，2026-09-05 确立）。
- 禁止擅自 `git commit`——提交由作者自行决定。
- 设计稿 `DESIGN.md` 是唯一权威设计来源；拍板记录（Q1-Q7 / D1-D7）改动需作者确认。
- 硬约束不可变（设计稿 §0）：prefix cache 全量命中至上 / 失败即中止压缩 / 中文摘要 / 点位精准注入（patch 3 仅 shadow 安装，D5+D7）。
- 修改宿主行为假设时，先重读对应源码段核实行号与签名（宿主是 editable 安装，可能升级）。

## 宿主关键锚点（2.3.0）

- `qwenpaw/agents/context/scroll/manager.py`：`ScrollContextManager`（patch 点 1 源）、`_update_continuation_summary`（1420）、流消费范式（1168-1198）、失败语义（1547-1562）、`to_dict`/`load_state`（2268/2315）
- `qwenpaw/agents/command_handler.py`：`CommandHandler._stored_scroll_summary_text`（561-572，patch 点 2 源）、`_process_compact`（384 起，patch 点 3 源，args→HintBlock 在 427）、构造点（659）
- `qwenpaw/agents/context/__init__.py`：构造点（187）
- agentscope：`ChatModelBase.__call__` 签名（tool_choice: ToolChoice）、流式协议（增量 chunks + 累计收尾 chunk is_last=True，`model/_base.py` `_stream()`）
