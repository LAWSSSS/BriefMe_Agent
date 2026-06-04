# 🏭 BriefMe · 多场景工业智能决策助手



## 📖 项目简介

BriefMe 是一个为工业现场量身定制的数据统计交互智能体。
它本身**不持久化存储任何业务数据**，而是通过解析用户的自然语言指令，智能路由并调用对应的现场业务系统 API 实时取数。数据经过本地核心算法层清洗与计算后，自动生成并交付标准化的 Excel 报表、PPT 汇报以及异常监控图片。

## 🛠️ 技术栈与依赖

* **核心语言**: Python 3.12+
* **前端交互**: `gradio` (构建 Web UI 界面)
* **大语言模型**: `zhipuai` (负责自然语言理解与 Function Calling)
* **网络请求**: `httpx` (对接各类现场专网 API)
* **自动化办公**: `openpyxl` (生成数据核对表), `python-pptx` (生成自动化汇报 PPT), `tencent docs upload` (生成报表后自动上传腾讯文档在线表格)
* **质量保证**: `pytest`, `flake8` (自动化测试与静态检查)

---

## 🏭 接入场景与核心业务规则

本项目严禁跨场景复用业务逻辑，各现场规则完全独立：

### 1. 永锋钢铁 · 烧结矿颗粒度 📦
* **功能**：生成人工筛分 vs 视觉准确率报表，按日对齐并计算各粒径误差 / MAE，导出 Excel 结果，并可自动上传到腾讯文档在线表格。
* **数据规则**：人工数据按 `inspectResult=Y` 保留；视觉 1# / 2# 对应指定站点与页面路径，取 `(T-4h, T]` 的视觉窗口后计算均值。
* **输出目录**：JSON 中间结果写入 `agent/yongfeng/output/`，Excel 报表默认输出到 `downloads/yongfeng/`。
* **网络前置**：需连接永锋专网并保证视觉 / 人工系统可访问。

### 2. 镔鑫钢铁 · 废钢检判 ♻️
* **功能**：生成单日/区间文本汇总、报表，下载错判图，**自动生成包含趋势图与 KPI 的汇报 PPT**。
* **业务规则**：主料型为“杂摸 / 中废”的不计入主料准确率（但保留在明细中）；AI 视觉报“重废”与人工报“重废1/2/3”视为一致。

### 3. 盛隆钢铁 · 废钢检判 ⚙️
* **功能**：生成单周期报表、多周期普通主表、**重废归一化多周期主表**。
* **业务规则**：
  * **黑名单过滤**：必须严格过滤指定的测试检判员（如王某某等），剔除后若无有效人员，全车人工结果视为缺失。
  * **单位防御**：扣重结果若 >10，系统启发式判定为“人工按 kg 录入但忘换算”，自动 `/1000` 转吨。
  * **重废归一化**：准确率仅统计重废1/2/3，人工没有这三类的车次**不进入准确率分母**。

---

## 🚀 快速启动 (Quick Start)

### 1. 克隆项目与配置环境
强烈建议使用纯净的虚拟环境隔离依赖：
```bash
git clone https://github.com/LAWSSSS/BriefMe_Agent.git
cd BriefMe_Agent

# 创建并激活虚拟环境
python -m venv venv
source venv/bin/activate  # Windows 用户请使用 venv\Scripts\activate

# 安装核心依赖
pip install -r requirements.txt
pip install python-pptx
```

### 2. 配置密钥与网络联通性自检
**⚠️ 严禁将真实 Key 或密码硬编码写入代码库！**
启动前，请根据你当前使用的终端类型，临时设置环境变量：

```bash
# 【如果你使用 Windows CMD 命令提示符】
set ZHIPU_API_KEY="<向负责人索取的智谱 API Key>"

# 【如果你使用 Windows PowerShell】
$env:ZHIPU_API_KEY="<向负责人索取的智谱 API Key>"

# 【如果你使用 Mac / Linux / Git Bash】
export ZHIPU_API_KEY="<向负责人索取的智谱 API Key>"
```
启动前，必须连接对应现场的专网 VPN。请通过浏览器访问以下地址验证连通性：

永锋打包带: http://vision.lg.china-yongfeng.com/packing-tape/

镔鑫废钢: http://172.31.1.102:8081/fcs-web/

盛隆废钢: http://172.16.16.101:3000/

### 3. 启动交互界面
```bash
python app.py
```

终端输出 Running on local URL: http://0.0.0.0:7860 后，在浏览器中打开该地址即可开始对话。左侧提供快捷指令按钮，点击填入后按回车发送。

## 💻 命令行批量导出模式 (CLI)

不开页面也可以运行 CLI，适合后台批量导出或排查问题：

```bash
# 导出永锋烧结矿准确率报表
python tools/yongfeng_export.py --start 2026-05-15 00:00:00 --end 2026-05-21 23:59:59

# 导出镔鑫区间报表 (不带错判图)
python tools/scrap_export.py --start 2026-04-22 --end 2026-04-28 --no-images

# 导出盛隆单周期报表
python tools/shenglong_export.py --start 2026-04-23 --end 2026-04-29

# 导出盛隆多周期重废归一化主表 (+ 号表示前后两段日期合并为一个统计周期)
python tools/shenglong_master_export.py --heavy-normalized \
  2026-04-14:2026-04-22 \
  2026-04-23:2026-04-29 \
  2026-04-30:2026-05-06+ \
  2026-05-07:2026-05-13
```

## 📁 核心代码结构

```text
agent智能体大赛/
├── app.py                         # Web UI 入口
├── config/settings.py             # 三个场景的 URL 配置与目标阈值
├── agent/                         # Agent 核心逻辑层
│   ├── core.py                    # LLM 路由、工具调用分发
│   ├── tools.py                   # 给大模型看的 Function Calling Schema
│   ├── data_fetcher.py            # 永锋打包带取数与异常处理
│   ├── scrap/                     # 📦 镔鑫废钢子包 (含 API 解析、业务统计、PPT 生成)
│   ├── shenglong/                 # 📦 盛隆废钢子包 (含 API 解析、黑名单过滤、复杂口径聚合)
│   └── yongfeng/                  # 📦 永锋烧结矿子包（含API解析、数据计算、报表输出）
├── tools/                         # CLI 批量导出工具脚本
├── tests/                         # 单元测试与业务逻辑断言
└── downloads/                     # 自动生成的报表、图片产物目录
```

## 🧑‍💻 开发者交接与协同规范 (Git Workflow)

为了保证工业级代码的绝对稳定，后续接手维护的工程师/实习生，请严格遵守以下开发规范：

### 1. 核心铁律
* **业务隔离**：不同现场的数据结构不同，切勿生搬硬套（如镔鑫与盛隆的料型 ID 完全不同）。
* **脱敏原则**：**严禁提交 `venv` 文件夹**，严禁提交真实 Token 或将 `downloads/` 里的客户真实报表 Push 到云端。

### 2. 测试驱动开发 (TDD)
任何涉及 **准确率计算、扣重口径、多周期合并逻辑、人员黑名单调整** 的代码变动，**必须**同步更新 `tests/` 目录下的测试用例。
提交代码前，必须在本地跑通验证命令：

```bash
# 运行全部业务逻辑断言 (必须全绿 PASSED)
python -m pytest tests/ -x --tb=short -q

# UI 构建冒烟测试
python -c "import app; app.build_ui(); print('UI OK')"
```

### 3. 标准化提交与自动合并流水线
本项目已配置 GitHub Actions 强管控，严禁在 `master` 分支直接修改代码。请遵循以下标准流转：

```bash
# 1. 获取最新主干代码
git checkout master
git pull origin master

# 2. 切出新分支进行开发 (不要直接在 master 上改代码)
git checkout -b fix-xxx-bug

# 3. 本地自测通过后，提交你的修改
git add .
git commit -m "feat/fix: 简要说明你的修改内容"

# 4. 推送到云端的对应分支
git push -u origin fix-xxx-bug
```

*推送到云端后，请在 GitHub 网页端发起 **Pull Request (PR)**。此时云端机器会自动进行测试，当流水线全部通过（变绿）后，系统将自动把你的代码安全合入主干。*

---
