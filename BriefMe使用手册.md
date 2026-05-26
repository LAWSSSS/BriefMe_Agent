# BriefMe 使用与二次开发手册

> 中冶赛迪（重庆）信息技术有限公司 · 多场景数据统计助手  
> 当前接入：永锋钢铁打包带 / 镔鑫钢铁废钢检判 / 盛隆钢铁废钢检判  
> 读者：使用者、实习生、后续维护开发者

---

## 0. 交接目标

这份文档不是单纯的“怎么点按钮”，而是给实习生接手优化用的交接手册。读完后应该能做到：

- 在本地启动 BriefMe。
- 知道每个场景怎么使用。
- 知道新增/修改功能应该改哪些文件。
- 知道哪些业务规则不能随便动。
- 知道每次改完要跑哪些验证命令。

一句话理解：BriefMe 是一层“自然语言 → 工具调用 → 业务系统 API → 本地计算 → 生成报表”的胶水层。它本身不存业务数据，每次都通过 VPN 到现场系统实时取数。

---

## 1. 快速启动

### 1.1 进入项目目录

```bash
cd /Users/wangyutai/Documents/agent智能体大赛
```

### 1.2 安装依赖

```bash
/opt/anaconda3/bin/pip install -r requirements.txt
/opt/anaconda3/bin/pip install python-pptx
```

主要依赖：

- `gradio`：页面 UI。
- `zhipuai`：自然语言理解和 function calling。
- `httpx`：访问现场业务系统 API。
- `openpyxl`：生成 Excel。
- `python-pptx`：生成镔鑫 PPT。

### 1.3 配置智谱 API Key

不要把真实 Key 写进代码或文档。启动前在终端设置：

```bash
export ZHIPU_API_KEY="<向负责人索取的智谱 API Key>"
```

如果要长期使用，可以写进本机 shell 配置：

```bash
echo 'export ZHIPU_API_KEY="<向负责人索取的智谱 API Key>"' >> ~/.zshrc
source ~/.zshrc
```

### 1.4 连接 VPN

三个场景走不同 VPN，不能混用。

| 场景 | VPN / 网络 | 自检地址 |
|---|---|---|
| 永锋打包带 | 永锋 aTrust，手机 Google Authenticator 验证码 | http://vision.lg.china-yongfeng.com/packing-tape/ |
| 镔鑫废钢 | 镔鑫专网 | http://172.31.1.102:8081/fcs-web/ |
| 盛隆废钢 | 盛隆专网 | http://172.16.16.101:3000/ |

原则：要跑哪个场景，就先用浏览器打开对应地址确认能访问。

### 1.5 启动页面

```bash
/opt/anaconda3/bin/python app.py
```

看到类似输出就成功：

```text
Running on local URL: http://0.0.0.0:7860
```

浏览器打开：

```text
http://localhost:7860
```

关闭服务：

```bash
Ctrl+C
```

---

## 2. 页面怎么用

页面左侧是快捷按钮，点击后只是把文字填到输入框，**不会自动发送**。确认日期和场景后，按回车或点发送。

| 按钮 | 作用 |
|---|---|
| 昨日【镔鑫】废钢检判统计 | 生成镔鑫文本汇总 |
| 近 7 天【镔鑫】报表+错判图 | 生成镔鑫 Excel，并下载错判渲染图 |
| 近 7 天【镔鑫】生成 PPT 汇报页 | 生成镔鑫单页 PPT |
| 昨日【盛隆】废钢检判统计 | 生成盛隆文本汇总 |
| 近 7 天【盛隆】报表 | 生成盛隆单周期 Excel |
| 【盛隆】主表（多周期累积·可改日期） | 生成盛隆普通多周期主表 |
| 【盛隆】重废归一化主表（排除无重废车次） | 生成盛隆新准确率口径主表 |
| 昨日打包带情况 | 生成永锋打包带文本汇总 |
| 下载昨日打包带异常图 | 下载永锋打包带异常原图/渲染图 |

页面下方“最近生成的报表 / PPT / 错判图片”会自动扫描 `downloads/`，展示最新产物。

---

## 3. 三个场景的使用方式

### 3.1 永锋钢铁 · 打包带

前置：连接永锋 VPN，顶部“打包带 VPN”为绿色。

常用指令：

```text
发昨天的打包带情况
发 2026-04-29 的打包带情况
下载昨天打包带的异常图片
```

主要输出：

- 当日生产钢卷总数。
- 正常、异常、未识别数量。
- 已打数与应打数差值为 1 / 大于 1 的异常数量。
- 差值大于 1 的异常图片。

### 3.2 镔鑫钢铁 · 废钢检判

前置：连接镔鑫 VPN，顶部“镔鑫 VPN”为绿色。

常用指令：

```text
发 2026-04-28 的【镔鑫】废钢检判情况
导出 2026-04-22 到 2026-04-28 的【镔鑫】废钢检判报表并下载错判图
按 2026-04-22 到 2026-04-28 的【镔鑫】检判结果生成对应的 PPT 汇报页
```

镔鑫关键规则：

- 人工主料型为“杂摸 / 中废”的车次不计入主料准确率，但保留在表格中。
- 视觉结果为“重废”，人工结果为“重废 / 重废1 / 重废2”，都算主料一致。
- PPT 由 `agent/scrap/ppt_builder.py` 生成，包含趋势图、KPI、错判 Top、建议和数据来源。

输出位置：

```text
downloads/scrap/<日期或区间>/
```

### 3.3 盛隆钢铁 · 废钢检判

前置：连接盛隆 VPN，顶部“盛隆 VPN”为绿色。

常用指令：

```text
发 2026-04-28 的【盛隆】废钢检判情况
导出 2026-04-23 到 2026-04-29 的【盛隆】废钢检判报表
导出 2026-04-23 到 2026-04-29 的【盛隆】报表，上周期是 2026-04-14 到 2026-04-22
```

普通多周期主表：

```text
生成【盛隆】主表，把这几个周期累积到一个 xlsx：
2026-04-14 至 2026-04-22、2026-04-23 至 2026-04-29、
2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13；
其中 2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13 当作一个统计周期进行统计
```

重废归一化主表：

```text
生成【盛隆】重废1/2/3归一化准确率主表，把这几个周期累积到一个 xlsx：
准确率统计时排除人工检判结果中没有任意重废1/2/3料型的车次；
2026-04-14 至 2026-04-22、2026-04-23 至 2026-04-29、
2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13；
其中 2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13 当作一个统计周期进行统计
```

盛隆关键规则：

- 剔除检判员：施宏波、冉星明、周倩、王宇泰、王重阳。
- 剔除后如果没有有效人工检判员，这辆车人工结果视为缺失。
- 人工或 AI 任一方缺失时，该车显示在明细里，但不计入汇总指标。
- 扣重结果如果大于 10 吨，视为人工按 kg 录入但未换算，自动除以 1000。
- 普通主表准确率：主料型一致，且占比差异 ≤10%。
- 重废归一化主表准确率：只看重废1/2/3，先把这三类归一化到 100%，再比较主重废类和差异。

重废归一化例子：

```text
人工：重废1 45%，重废2 35%，厚剪 10%，剪料1 10%
目标料型总占比 = 45 + 35 = 80
重废1归一化 = 45 / 80 * 100 = 56.25%
重废2归一化 = 35 / 80 * 100 = 43.75%
```

注意：如果人工检判结果中没有任何重废1/2/3，这辆车不进入重废归一化准确率分母。

输出位置：

```text
downloads/shenglong/<日期或区间>/
downloads/shenglong/master/
```

---

## 4. CLI 命令

不开页面也可以跑 CLI，适合批量导出或排查问题。

镔鑫导出：

```bash
/opt/anaconda3/bin/python tools/scrap_export.py --start 2026-04-22 --end 2026-04-28
/opt/anaconda3/bin/python tools/scrap_export.py --start 2026-04-22 --end 2026-04-28 --no-images
```

盛隆单周期：

```bash
/opt/anaconda3/bin/python tools/shenglong_export.py --start 2026-04-23 --end 2026-04-29
```

盛隆普通多周期主表：

```bash
/opt/anaconda3/bin/python tools/shenglong_master_export.py \
  2026-04-14:2026-04-22 \
  2026-04-23:2026-04-29 \
  2026-04-30:2026-05-06+ \
  2026-05-07:2026-05-13
```

盛隆重废归一化主表：

```bash
/opt/anaconda3/bin/python tools/shenglong_master_export.py \
  --heavy-normalized \
  2026-04-14:2026-04-22 \
  2026-04-23:2026-04-29 \
  2026-04-30:2026-05-06+ \
  2026-05-07:2026-05-13
```

说明：日期段后面的 `+` 表示“这一段和下一段合并成同一个统计周期”。

---

## 5. 代码结构

```text
agent智能体大赛/
├── app.py                         # Gradio UI 入口
├── requirements.txt
├── BriefMe使用手册.md              # 当前交接文档
│
├── config/
│   └── settings.py                # 三个场景的 URL、账号、目标值
│
├── agent/
│   ├── core.py                    # SteelCoilAgent：LLM 路由、工具调用、返回组织
│   ├── tools.py                   # 给大模型看的 function calling schema
│   ├── vpn_manager.py             # 永锋 VPN 探测
│   ├── data_fetcher.py            # 永锋打包带取数
│   │
│   ├── scrap/                     # 镔鑫废钢，独立子包
│   │   ├── client.py              # 登录、列表、详情、图片下载
│   │   ├── parser.py              # 解析人工/AI 结果
│   │   ├── calculator.py          # 业务统计
│   │   ├── excel_writer.py        # 镔鑫 xlsx
│   │   ├── ppt_builder.py         # 镔鑫自研 PPT
│   │   └── ppt/                   # 同事 skill fallback
│   │
│   └── shenglong/                 # 盛隆废钢，独立子包
│       ├── client.py              # 盛隆登录、列表、详情
│       ├── dict.py                # 盛隆料型字典、黑名单、重废目标料型
│       ├── models.py              # dataclass 数据结构
│       ├── calculator.py          # 盛隆核心业务规则
│       └── excel_writer.py        # 盛隆 xlsx / 多周期主表
│
├── tools/
│   ├── scrap_export.py
│   ├── shenglong_export.py
│   └── shenglong_master_export.py
│
├── tests/
│   ├── test_scrap_*.py
│   └── test_shenglong_unit.py
│
└── downloads/                     # 生成产物，不要手写业务逻辑依赖这里
```

---

## 6. 核心工作流

```text
用户输入自然语言
  ↓
app.py 把消息交给 SteelCoilAgent
  ↓
core.py 的系统提示词 + tools.py schema 告诉大模型可调用哪些工具
  ↓
大模型选择工具并抽取参数
  ↓
core.py dispatch 到对应场景
  ↓
client.py 访问现场 API
  ↓
calculator.py / parser.py 计算业务指标
  ↓
excel_writer.py / ppt_builder.py 生成文件
  ↓
downloads/ 下展示给页面
```

开发时先判断要改哪一层：

- UI 文案 / 快捷按钮：改 `app.py`。
- 大模型能不能选对工具：改 `agent/core.py` 的系统提示词和 `agent/tools.py`。
- API 地址、登录、分页、详情字段：改对应 `client.py`。
- 统计公式、过滤规则：改对应 `calculator.py` / `parser.py`。
- Excel 格式：改对应 `excel_writer.py`。
- PPT 格式：改 `agent/scrap/ppt_builder.py`。

---

## 7. 实习生开发守则

### 7.1 不要混场景

永锋、镔鑫、盛隆是三个不同现场。不要把一个场景的字典、API、统计口径复制到另一个场景里直接用。

特别注意：

- 镔鑫的 `steelType` 编码和盛隆不同。
- 镔鑫“重废不分级”和盛隆“重废1/2/3归一化”不是一回事。
- 盛隆有检判员黑名单，镔鑫没有这套规则。

### 7.2 改统计规则必须补测试

只要动了以下内容，必须改或新增测试：

- 准确率分母。
- 主料型是否正确。
- 扣重是否符合。
- 人员剔除。
- 单位换算。
- Excel Sheet1 统计周期概括。
- 多周期主表合并逻辑。

推荐先改 `tests/test_shenglong_unit.py` 或对应镔鑫测试，再改实现。

### 7.3 不要提交真实密钥

禁止把这些写死进代码：

- `ZHIPU_API_KEY`
- VPN 密码
- 现场系统账号密码的新版本
- cookie / token

本项目历史上 `config/settings.py` 有开发阶段默认值，交付或上传前要检查是否需要脱敏。

### 7.4 不要删除 downloads 里的真实产物

`downloads/` 里可能有演示用报表和截图。调试可以新建 `_unit_test/` 或 `_preview/`，不要随便清空整个目录。

---

## 8. 常见修改任务怎么做

### 8.1 新增一个页面快捷按钮

改 `app.py`：

1. 在 `_quick_prompts()` 加一条 prompt。
2. 在左侧按钮区加一个 `gr.Button`。
3. 在底部绑定 `btn.click(lambda: q["xxx"], outputs=msg)`。
4. 跑 UI 构建冒烟。

```bash
/opt/anaconda3/bin/python -c "import app; app.build_ui(); print('UI OK')"
```

### 8.2 新增一个大模型工具

至少改两个文件：

- `agent/tools.py`：新增 function schema。
- `agent/core.py`：在 `_execute_tool` 里加分支，并实现 `_tool_xxx`。

如果用户很容易说错，还要改 `core.py` 的系统提示词，明确什么话术走新工具。

### 8.3 修改盛隆主表 Sheet1

主要看：

- `agent/shenglong/models.py` 的 `PeriodSummary`。
- `agent/shenglong/calculator.py` 的 `aggregate_period` / `aggregate_period_heavy_normalized`。
- `agent/shenglong/excel_writer.py` 的 `_write_one_period_block()` 和 `write_master_xlsx()`。

Sheet1 是每个周期 14 行，多个周期就是 14 行块往下排。

### 8.4 修改盛隆 Sheet2 明细

主要看：

- `agent/shenglong/excel_writer.py` 的 `_truck_row_values()`。
- 普通口径直接用 `truck.manual_main` / `truck.ai_main`。
- 重废归一化口径会先通过 `to_heavy_normalized_view()` 把单车展示切换为归一化结果。

### 8.5 修改盛隆重废归一化规则

主要看 `agent/shenglong/calculator.py`：

- `HEAVY_STEEL_TYPES` 在 `agent/shenglong/dict.py`，当前是 `{1, 2, 3}`。
- `_normalized_target_rates()` 负责归一化。
- `_judge_heavy_normalized_truck()` 负责单车是否正确。
- `aggregate_period_heavy_normalized()` 负责周期聚合。
- `to_heavy_normalized_view()` 负责 Sheet2 展示切换。

---

## 9. 验证命令

每次改完至少跑：

```bash
/opt/anaconda3/bin/python -m pytest tests/ -x --tb=short -q
```

只看盛隆：

```bash
/opt/anaconda3/bin/python tests/test_shenglong_unit.py
```

只看 UI 能否构建：

```bash
/opt/anaconda3/bin/python -c "import app; app.build_ui(); print('UI OK')"
```

如果连了盛隆 VPN，真实导出普通主表：

```bash
/opt/anaconda3/bin/python tools/shenglong_master_export.py \
  2026-04-14:2026-04-22 \
  2026-04-23:2026-04-29
```

如果连了盛隆 VPN，真实导出重废归一化主表：

```bash
/opt/anaconda3/bin/python tools/shenglong_master_export.py \
  --heavy-normalized \
  2026-04-14:2026-04-22 \
  2026-04-23:2026-04-29 \
  2026-04-30:2026-05-06+ \
  2026-05-07:2026-05-13
```

---

## 10. 常见问题

### Q1. VPN 灯是红的

先用浏览器打开对应业务系统首页。打不开就是 VPN 或网络问题，不是代码问题。打开后再点页面上的“刷新 VPN 状态”。

### Q2. 只说“废钢”，agent 反问

这是故意设计。镔鑫和盛隆都是废钢，但数据、API、字典和规则都不同。指令里带【镔鑫】或【盛隆】即可。

### Q3. `InvalidPathError: Dotfiles ...`

WPS/Excel 打开文件时可能生成 `.~xxx.xlsx` 临时锁文件。`app.py` 的 `_is_visible_file()` 已过滤 `.开头` 和 `~$开头` 文件。如果又出现类似报错，检查 `downloads/` 是否出现新类型隐藏文件，并加到过滤函数。

### Q4. 真实接口报 401 / token 失效

先确认 VPN，再确认账号密码，再看对应 `client.py` 的登录逻辑。

- 镔鑫 token：`satoken`。
- 盛隆 token：`scrape-steel-token`，token 路径是 `data.tokenInfo.tokenValue`。

### Q5. Excel 里出现很大的扣重，比如 26 或 2280

盛隆已加自动修正：人工扣重 >10 吨视为 kg 录入，自动 `/1000` 转吨。日志里会出现：

```text
扣重单位自动修正: 某某 录入 2280.000 → 视为 kg → 2.280 吨
```

### Q6. 普通主表和重废归一化主表有什么区别

普通主表：按原始主料型判断，主料一致且差异 ≤10% 才算正确。

重废归一化主表：只看重废1/2/3，把这三类归一化后再判断。人工没有任意重废1/2/3的车不进入准确率分母。扣重和价格统计不变。

---

## 11. 当前重点功能清单

| 功能 | 状态 | 关键文件 |
|---|---|---|
| 永锋打包带日统计 | 已实现 | `agent/data_fetcher.py` |
| 永锋异常图下载 | 已实现 | `agent/data_fetcher.py` |
| 镔鑫日/区间统计 | 已实现 | `agent/scrap/` |
| 镔鑫错判图下载 | 已实现 | `agent/scrap/client.py` |
| 镔鑫 PPT | 已实现 | `agent/scrap/ppt_builder.py` |
| 盛隆日/区间统计 | 已实现 | `agent/shenglong/` |
| 盛隆单周期报表 | 已实现 | `agent/shenglong/excel_writer.py` |
| 盛隆多周期普通主表 | 已实现 | `write_master_xlsx()` |
| 盛隆多周期重废归一化主表 | 已实现 | `aggregate_period_heavy_normalized()` / `to_heavy_normalized_view()` |

---

## 12. 给实习生的第一天任务建议

1. 跑通 `app.py`，打开页面。
2. 不连 VPN，先跑单测：

```bash
/opt/anaconda3/bin/python -m pytest tests/ -x --tb=short -q
```

3. 阅读这 5 个文件：

```text
app.py
agent/core.py
agent/tools.py
agent/shenglong/calculator.py
agent/shenglong/excel_writer.py
```

4. 用 `tools/shenglong_master_export.py --help` 看 CLI 参数。
5. 找一个小文案改动，比如按钮文字，改完跑 UI 构建冒烟。
6. 再做业务逻辑改动，不要第一天就改统计公式。

---

最后更新：2026-05-21
