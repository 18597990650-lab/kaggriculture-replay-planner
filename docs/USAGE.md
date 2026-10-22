# 使用说明

## 1. 项目安装

建议使用 Python 3.11 建立虚拟环境，并在项目根目录安装：

```bash
python -m pip install -e .
```

若只查看文档或运行基础测试，不需要额外安装 Kaggle CLI。下载对局回放、重新编译本地扩展或提交代理时，需要根据实际环境自行配置对应工具。

## 2. 项目检查

```bash
python -m kaggriculture_planner.cli info
python -m pytest tests
```

基础测试只检查标准化项目结构、配置文件、入口脚本和方案文档是否齐全，不替代完整对局评估。

## 3. 运行代理入口

比赛提交要求运行包根目录存在 `main.py`，并暴露 `agent(observation, configuration)` 函数。本项目保留在 `agent/main.py`。

## 4. 日程库构建

```bash
python tools/build_library.py --out work/programmes
```

该命令根据 `data/` 中的配置和任务日历生成策略日程库，用于检查日程构建逻辑和候选分支组织方式。

## 5. 对手供给预测检查

```bash
python tools/predict_opponent.py --replay work/episode.json --seat 1 --step 144 --out work/forecast.json
```

`--seat` 表示我方座位，预测对象为另一方。指定步数应按比赛日内节奏选择，并确保输入回放文件完整。

## 6. 成交标签提取

```bash
python tools/replay_sales.py --replay work/episode.json --seat 1 --out work/trade-labels.json
```

输出包括公开日初特征、分桶成交量和逐单位成交记录，可用于检查市场供给预测的标签口径。

## 7. 打包

```bash
python tools/package_agent.py --agent agent --out work/submission.tar.gz
```

打包命令会忽略缓存文件，并生成可提交的 `tar.gz` 运行包。
