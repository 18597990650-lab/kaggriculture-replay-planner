# 资产与平台说明

仓库中的 Python 源码、配置、文档、研究脚本和测试用于展示方案结构与复现离线检查。比赛运行包还包含针对 Linux x86_64 + CPython 3.11 构建的压缩模型和本地扩展；这些文件按目录保存在 `agent/` 中。

完整比赛运行包 `submission.tar.gz` 作为 [v1.0.0 Release](https://github.com/18597990650-lab/kaggriculture-replay-planner/releases/tag/v1.0.0) 的附件提供。运行 `tools/verify_release.py` 前，请将该附件下载到项目根目录。Linux 下可执行：

```bash
curl -L --fail -o submission.tar.gz https://github.com/18597990650-lab/kaggriculture-replay-planner/releases/download/v1.0.0/submission.tar.gz
python tools/verify_release.py
```

原始运行包 SHA-256：`ba6563fef551ad8091d37cedd37a7864eb224e46ecba7777f0d68d150066101d`。

在 Windows 上可以阅读源码、运行结构检查和发布校验；要直接执行完整运行代理，应使用目标平台和对应 Python 版本。线上提交、完整对局评估和排行榜结果不由本仓库的离线检查自动推断。
