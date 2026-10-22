# Kaggriculture Replay Planner

> 面向 Kaggriculture 农场经营模拟赛的规划型智能体：把策略日程、市场估值、对手供给预测和规则执行器组合成一套可检查、可复现、可继续迭代的工程方案。

[![Python 3.11](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Tests](https://github.com/18597990650-lab/kaggriculture-replay-planner/actions/workflows/tests.yml/badge.svg)](https://github.com/18597990650-lab/kaggriculture-replay-planner/actions/workflows/tests.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)

## 项目亮点

- **长期规划**：用经过真实对局约束整理的生产日程库，将高维动作空间压缩为可比较的候选计划。
- **市场感知**：把市场库存、价格变化、商店需求和对手未来供给纳入统一估值，而不是只追逐当前价格。
- **运行时修正**：执行器根据资金、地块、库存、工人位置和剩余时间修正高层计划，降低固定脚本的脆弱性。
- **工程化交付**：运行代理、离线回放工具、研究脚本、配置、文档、测试和发布校验集中管理。

## 可验证成果

以下数字来自仓库中的 [`VALIDATION.json`](VALIDATION.json)，对应完整性和重建检查，不等同于新的线上成绩：

| 指标 | 结果 |
| --- | ---: |
| 已校验代理文件 | 275 |
| 重建策略日程 | 41 |
| 字节级一致的库文件 | 44 |
| 已验证本地扩展产物 | 22 |
| 成功解析的 Python 文件 | 159 |
| 回放中验证的原生状态转移 | 719 |
| 回放中提取的真实成交单位 | 8,724 |

验证边界、提交编号、哈希和未执行项目见 [`VALIDATION.json`](VALIDATION.json) 与 [`RELEASE.json`](RELEASE.json)。仓库不把未完成的线上评估描述成已获得的比赛排名。

## 方法概览

```text
公开状态 / 历史回放
          │
          ├── 策略日程库 ──┐
          ├── 对手供给预测 ─┼── 市场估值 ── 候选计划选择
          └── 规则与资源约束 ─┘                 │
                                               ▼
                                      运行时执行器 → 当前回合动作
```

核心方案说明见 [`docs/方案说明.md`](docs/方案说明.md)。如果只想快速理解目录和入口，可先看 [`docs/USAGE.md`](docs/USAGE.md)。

## 目录结构

| 路径 | 内容 |
| --- | --- |
| `agent/` | 比赛运行代理、策略规划器、对手模型、策略日程和目标平台的运行资产 |
| `tools/` | 日程构建、回放解析、成交标签、预测检查、打包与发布校验 |
| `data/` | 日程库构建输入、路线配置和任务日历 |
| `reference/` | 规则重演、种子工具和环境解释代码 |
| `research_sources/` | 研究阶段脚本，用于说明预测数据集、指标学习和模型导出 |
| `src/kaggriculture_planner/` | 标准化项目封装层与 CLI 入口 |
| `configs/` | 平台、路径、校验范围和提交命令配置 |
| `docs/` | 方案、项目结构和使用说明 |
| `tests/` | 项目结构与元数据测试 |

## 快速开始

项目默认面向 Linux x86_64、CPython 3.11；纯 Python 的结构检查可在其他平台执行。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
python -m pip install -e ".[dev]"

# 查看项目元信息
python -m kaggriculture_planner.cli info

# 运行基础测试
python -m pytest tests

# 下载原始运行包并校验发布包结构
curl -L --fail -o submission.tar.gz https://github.com/18597990650-lab/kaggriculture-replay-planner/releases/download/v1.0.0/submission.tar.gz
python tools/verify_release.py
```

构建提交包：

```bash
python tools/package_agent.py --agent agent --out work/submission.tar.gz
```

回放、对手供给预测和成交标签的用法见 [`docs/USAGE.md`](docs/USAGE.md)。二进制模型与平台差异说明见 [`docs/ASSETS.md`](docs/ASSETS.md)。

## 复现边界与资产说明

- `agent/main.py` 是比赛提交要求的入口，并暴露 `agent(observation, configuration)`。
- `agent/` 中部分扩展和压缩模型资产针对 Linux x86_64 + CPython 3.11 构建；在 Windows 上可阅读源代码并运行结构检查，但不能据此推断线上运行结果。
- `research_sources/` 保留历史研究脚本，作为方法参考，不宣称它们构成独立的最终模型训练流水线。
- `VALIDATION.json` 明确区分了完整性检查、离线回放验证、原生重建和线上评估，避免把不同证据混为一谈。

## 许可与致谢

项目采用 Apache License 2.0，第三方来源的许可和署名信息保留在相应目录与 [`NOTICE`](NOTICE) 中。使用或改编前请同时阅读相关源文件中的许可说明。

