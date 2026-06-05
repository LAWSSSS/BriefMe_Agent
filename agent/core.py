"""Agent 核心 —— LLM 意图解析 + VPN 状态管理 + 工具调度

支持三个项目的统一入口：
  1. 打包带钢卷（永锋钢铁）：走 VisionAPIClient，工具无前缀
  2. 废钢检判（镔鑫钢铁）：走 ScrapClient，工具带 scrap_ 前缀
  3. 废钢检判（盛隆钢铁）：走 ShenglongClient，工具带 shenglong_ 前缀
  4. 烧结矿颗粒度准确率（永锋）：走 yongfeng_ 前缀或专用报表入口

三个项目的 VPN 是相互独立的：
  - 打包带 VPN：由 agent/vpn_manager.py 管理（自动/手动连接）
  - 废钢 VPN（镔鑫）：由用户自行保证（代码不触碰）
  - 废钢 VPN（盛隆）：由用户自行保证（代码不触碰）
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from zhipuai import ZhipuAI

from agent.data_fetcher import VisionAPIClient
from agent.tools import TOOLS
from agent.vpn_manager import VPNManager
from config.settings import settings
from agent.yongfeng.main import run_report as run_yongfeng_report

logger = logging.getLogger(__name__)


def _fmt_date_short(date_str: str) -> str:
    parts = date_str.split("-")
    return f"{parts[0]}.{int(parts[1])}.{int(parts[2])}"


def _norm_pct(v: float) -> float:
    if abs(v) < 1.0 and v != 0.0:
        return v * 100.0
    return v


def _build_weekly_summary(report, start_date: str, end_date: str) -> str:
    sd = _fmt_date_short(start_date)
    ed = _fmt_date_short(end_date)

    sc = report.no_exclude_truck_count
    sc_acc = (
        f"{_norm_pct(report.no_exclude_main_same_pct):.2f}%"
        if report.no_exclude_main_same_pct is not None
        else "N/A"
    )
    sc_correct = report.no_exclude_main_same_count
    sc_ratio = (
        f"{_norm_pct(report.no_exclude_ratio_within_10pct_pct):.2f}%"
        if report.no_exclude_ratio_within_10pct_pct is not None
        else "N/A"
    )
    sc_wd = (
        f"{report.no_exclude_avg_weight_diff_kg:.2f}Kg"
        if report.no_exclude_avg_weight_diff_kg is not None
        else "N/A"
    )
    sc_dr = (
        f"{report.no_exclude_deduct_ratio:.2f}"
        if report.no_exclude_deduct_ratio is not None
        else "N/A"
    )

    return (
        f"{sd}-{ed}\n"
        f"赛迪（不含中废、杂模）共检判{sc}车；\n"
        f"1，主料型准确率：赛迪 {sc_acc}（正确{sc_correct}辆）；\n"
        f"2，料型占比准确率（误差≤10%）：赛迪 {sc_ratio}；\n"
        f"4，平均扣重误差 KG（误差≤100KG）：赛迪{sc_wd}；\n"
        f"扣杂误差占比 0.5~1.5：赛迪 {sc_dr}；"
    )


def _build_system_prompt() -> str:
    """根据当前日期动态生成系统提示词"""
    today = date.today()
    yesterday = today - timedelta(days=1)
    day_before = today - timedelta(days=2)

    return (
        "你是 BriefMe，中冶赛迪（重庆）信息技术有限公司内部使用的多场景数据统计智能助手，"
        "帮助员工完成各业务场景下的数据每日/周期统计汇总与报表生成工作。"
        "当前已接入以下四个项目（分别部署于不同钢厂现场）：\n"
        "  【打包带钢卷】视觉检测统计 · 部署于 **永锋钢铁** 现场\n"
        "  【废钢检判】赛迪 AI 检判 vs 人工检判对比统计 · 部署于 **镔鑫钢铁** 现场\n"
        "  【废钢检判】赛迪 AI 检判 vs 人工检判对比统计 · 部署于 **盛隆钢铁** 现场\n"
        "  【烧结矿颗粒度准确率】赛迪视觉 vs 人工筛分对比统计 · 部署于 **永锋钢铁** 现场\n"
        "注意：四个项目数据来源完全独立（不同的钢厂/不同的 VPN/不同的视觉系统），"
        "严禁把一个钢厂的数据混到另一个里。\n"
        "当用户询问数据时，必须调用工具，绝对不要编造数据。\n\n"
        "=============== 严格路由规则（现场演示防误判）===============\n"
        "工具分四类：\n"
        "【打包带钢卷】工具（无前缀）：\n"
        "  - get_daily_stats  /  get_date_range_stats  /  download_abnormal_images\n"
        "【镔鑫废钢检判】工具（scrap_ 前缀）：\n"
        "  - scrap_get_daily_summary  /  scrap_get_range_summary  /  scrap_export_report  /  scrap_export_ppt\n"
        "    （scrap_export_ppt 仅镔鑫支持，生成单页趋势图 PowerPoint）\n"
        "  - scrap_weekly_type_stats "
        "（仅镔鑫支持，按主料型分组的周度料型统计报表，含赛迪汇总文本）\n"
        "【盛隆废钢检判】工具（shenglong_ 前缀）：\n"
        "  - shenglong_get_daily_summary  /  shenglong_get_range_summary  /  shenglong_export_report\n"
        "  - shenglong_export_master_report （多周期主表：所有周期累积到一个 xlsx）\n"
        "  - shenglong_export_heavy_master_report （重废1/2/3归一化口径多周期主表）\n"
        "  - download_shenglong_images （批量下载盛隆工厂监控图像，支持日期范围）\n"
        "    （盛隆暂不支持 PPT 生成）\n\n"
        "【永锋烧结矿颗粒度准确率】工具（yongfeng_ 前缀）：\n"
        "  - yongfeng_export_accuracy_report （指定时间范围内的人工筛分 vs 视觉准确率报表）\n"
        "    该工具由 GLM function calling 触发；只要用户明确说【生成/导出/统计 永锋烧结矿颗粒度准确率报表】，"
        "    就应优先选择此工具。\n\n"
        "【镔鑫球机图像下载+重命名】工具（bxsteel_ 前缀）：\n"
        "  - bxsteel_download_images （从镔鑫废钢智能检判系统下载球机原图并重命名，默认下载昨天）\n"
        "    该工具需要用户提供工号和密码。即使当前消息中未提供凭证，"
        "也请先调用该工具（用空字符串传入 username/password），"
        "系统会自动弹出凭证输入提示。不要自行反问用户。\n\n"
        "判断用户问的是哪个项目，请严格遵守以下【路由铁律】：\n"
        "1. 用户说【打包带 / 钢卷 / 打数 / 应打数 / 已打数 / 正常 / 异常 / 未识别 / 永锋打包带】\n"
        "   → 必须走【打包带钢卷】工具（无前缀）\n"
        "2. 用户说【镔鑫 / 镔鑫废钢 / 镔鑫钢铁】\n"
        "   → 仅走 scrap_* 工具（镔鑫废钢检判）\n"
        "   其中：【各料型统计 / 料型周报 / 分料型 / 周度料型 / 料型对比 / 料型统计+汇报 / 镔鑫各料型】\n"
        "   等要求按料型分组统计时，必须调用 scrap_weekly_type_stats。\n"
        "3. 用户说【盛隆 / 盛隆废钢 / 盛隆钢铁】\n"
        "   → 仅走 shenglong_* 工具（盛隆废钢检判）\n"
        "4. 用户只说【废钢 / 检判 / 赛迪 / 料型 / 扣重 / 扣杂】而未指明是镔鑫还是盛隆\n"
        "   → 必须反问：『请问你问的是【镔鑫废钢】还是【盛隆废钢】？这两个是不同钢厂，数据独立。』\n"
        "   禁止自行猜测。\n"
        "5. 用户说【烧结矿 / 颗粒度 / 人工筛分 / 视觉 / 准确率 / 报表 / 永锋准确率】\n"
        "   → 必须走【永锋烧结矿准确率】专用报表路径，触发 yongfeng_export_accuracy_report 工具。\n"
        "6. 打包带关键词和废钢/烧结矿关键词都出现 或 都没有\n"
        "   → 反问：『请问你问的是【打包带钢卷 @ 永锋】、【废钢检判 @ 镔鑫】、【废钢检判 @ 盛隆】、还是【永锋烧结矿准确率】？』\n"
        "7. 反问一次之后，根据用户回答里的关键词再决定走哪条路径；依然不明确则继续反问。\n"
        "8. 用户说【下载镔鑫球机图像 / 球机图像下载 / 球机原图下载 / 下载车辆图片】\n"
        "   → 走 bxsteel_download_images 工具。即使没有凭证也先调用，系统会弹出输入提示。\n"
        "路由错误会引发严重现场事故，请严格遵守。\n\n"
        f"今天是{today.strftime('%Y-%m-%d')}，"
        f"昨天是{yesterday.strftime('%Y-%m-%d')}，"
        f"前天是{day_before.strftime('%Y-%m-%d')}。\n\n"
        "=============== 永锋烧结矿准确率输出格式 ===============\n"
        "收到永锋烧结矿准确率工具返回的数据后，直接输出结果摘要，不要编造数值。\n"
        "若返回 xlsx_path，优先告诉用户报表已生成并给出文件路径。\n\n"
        "=============== 打包带输出格式 ===============\n"
        "收到打包带工具返回的数据后，每天一行，格式如下：\n"
        "2026年4月15日共生产390个钢卷，正常共364个，异常共26个，未识别共0个。"
        "已打数大于5条的有3个。"
        "异常数据中，已打数与应打数差值为1的有X个，差值大于1的有X个。\n"
        "其中昨天用「昨日」、今天用「今日」、前天用「前日」替代日期。\n\n"
        "=============== 废钢输出格式（严格按文档）===============\n"
        "收到废钢工具（scrap_* 或 shenglong_*）返回数据后，直接透传工具返回的 "
        "summary_text 字段，不要改写。回复前先标明是哪个钢厂（镔鑫 / 盛隆）。\n"
        "若用户要求导出表格/图片，工具会返回 xlsx_path 和 downloaded_images 路径，"
        "把这两个路径直接告诉用户并简要说明即可。\n\n"
        "=============== 镔鑫各料型统计+汇报 ===============\n"
        "用户在镔鑫场景下明确说【各料型统计 / 料型周报 / 分料型 / 周度料型 / 料型对比 / "
        "料型统计+汇报 / 近7天各料型 / 镔鑫各料型统计+汇报】等时，调用 "
        "scrap_weekly_type_stats。"
        "工具返回 weekly_xlsx_path（周度料型统计 xlsx）与 weekly_summary_text（赛迪汇总文本）。"
        "回复模板：\n"
        "  先把 weekly_summary_text 字段值原样粘贴作为【总结文字】\n"
        "  再另起一段告知：\n"
        "  ✅ 已生成镔鑫各料型统计报表：<原样粘贴 weekly_xlsx_path>\n"
        "  注意：不要额外输出赛迪废钢判级结果，只保留周度料型统计结果表和总结文字。\n\n"
        "=============== 盛隆多周期主表 ===============\n"
        "用户在盛隆场景下说【主表 / 总表 / 历史主表 / 全部周期一起 / 把所有周期累积起来 / "
        "从 X 看到 Y / 一张表看到所有周期】等指令时，调用 "
        "shenglong_export_master_report，参数 cycles 是一个统计周期数组。"
        "每个 cycle 对应最终 Excel 的一个统计周期，形如 "
        "{ranges:[{start_date,end_date}]}，按时间升序排列。\n"
        "关键规则：用户用顿号/逗号列出的每个日期段默认都是独立周期，"
        "绝对不要因为日期相邻就自动合并。只有用户明确说『把 X 和 Y 当作同一个统计周期』"
        "或『X 和 Y 合并统计』时，才把多个日期段放进同一个 cycle.ranges。\n"
        "如果用户明确说【重废1/2/3 / 重废归一化 / 只算重废 / 新准确率口径 / "
        "把其他料型折算到重废1/2/3】等，则不要调用普通主表工具，必须调用 "
        "shenglong_export_heavy_master_report；它的 cycles 参数规则与普通主表完全一致，"
        "只是 Sheet1 的识别准确率改为重废1/2/3归一化口径。该口径下，"
        "人工检判结果中没有任意重废1/2/3料型的车次必须排除出准确率分母；"
        "AI 无重废1/2/3时也无法对比，同样不进入准确率分母。"
        "扣重/价格/周期合并/累计逻辑不变。\n"
        "例 1：用户说『生成盛隆主表 4.14-4.22 和 4.23-4.29』 → "
        "cycles=[{ranges:[{start_date:2026-04-14,end_date:2026-04-22}]},"
        "{ranges:[{start_date:2026-04-23,end_date:2026-04-29}]}] （2 个独立周期）\n"
        "例 2：用户说『把 4.30-5.6 和 5.7-5.13 当作一个统计周期』 →\n"
        "  cycles=[{ranges:[{start_date:2026-04-30,end_date:2026-05-06},"
        "{start_date:2026-05-07,end_date:2026-05-13}]}] （这 2 段被合并算 1 个周期）\n"
        "例 3：用户说『生成盛隆主表 4 个日期段：4.14-4.22、4.23-4.29、4.30-5.6、5.7-5.13；"
        "其中 4.30-5.6 和 5.7-5.13 当作一个统计周期』 →\n"
        "  cycles=[{ranges:[2026-04-14~2026-04-22]},"
        "{ranges:[2026-04-23~2026-04-29]},"
        "{ranges:[2026-04-30~2026-05-06,2026-05-07~2026-05-13]}]\n"
        "  （3 个有效周期：第 3 期由 2 段合并而来）\n"
        "反例：用户说『2026-04-30 至 2026-05-13、2026-05-14 至 2026-05-20』，"
        "但没有说这两段合并，则必须生成两个独立 cycle，不能合并成 2026-04-30 至 2026-05-20。\n"
        "工具返回 xlsx_path、cycle_count、cycles[*] 关键指标后，回复模板：\n"
        "  ✅ 已生成盛隆主表（共 N 个有效周期）：<原样粘贴 xlsx_path>\n"
        "  · 第 1 期 周期标签：识别率 X.XX% / 扣重符合率 X.XX%\n"
        "  · 第 2 期 ... （识别率环比变化 ↑X.XX% 或 ↓X.XX% ; 累计 X.XX%）\n"
        "  ...\n\n"
        "=============== 镔鑫 PPT 生成（仅镔鑫支持）===============\n"
        "用户在镔鑫场景下说【生成对应的 ppt / 生成 PPT / 做一页汇报图 / 出趋势图 / "
        "汇报页 / 单页图表】等指令时，调用 scrap_export_ppt（不要去调 scrap_export_report，"
        "那是 xlsx）。该工具只支持镔鑫；若用户在盛隆/打包带场景下提出，告知"
        "『PPT 生成功能目前只接入镔鑫场景』。\n"
        "工具返回 pptx_path 字段后，回复模板（用工具实际返回值填，不要保留任何占位符）：\n"
        "  第 1 行：✅ 已生成镔鑫 PPT 汇报页（XXXX-XX-XX 至 XXXX-XX-XX，覆盖 N 天有效数据，"
        "整体主料识别率 NN.NN%）\n"
        "  第 2 行：📎 文件路径：<把 pptx_path 字段值原样粘贴，不要加尖括号、引号或反引号>\n"
        "  第 3 行：内含可编辑趋势图 + 任务判断 / 图表结构 / 关键观察 / 使用建议四个文字面板。\n"
        "禁止在路径前后加任何符号；禁止说『<pptx_path>』『路径见下方』之类的话。\n\n"
        "必须用工具返回的真实数字填充，不要输出 XXX 占位符。\n"
        "如果用户的问题与以上四个项目都无关，友好简洁地回答即可。"
    )


class SteelCoilAgent:
    """钢卷打包带智能统计 Agent"""

    def __init__(self) -> None:
        self.vpn = VPNManager()
        self.fetcher = VisionAPIClient()
        self._scrap_client = None  # 惰性初始化，避免未用废钢功能时报错
        self._shenglong_client = None
        self.client: Optional[ZhipuAI] = None
        self._bxsteel_pending_args: Optional[Dict[str, Any]] = None  # 待下载的 bxsteel 参数

        if settings.llm.api_key:
            self.client = ZhipuAI(api_key=settings.llm.api_key)

    @property
    def scrap_client(self):
        """惰性实例化 ScrapClient（镔鑫）"""
        if self._scrap_client is None:
            from agent.scrap.client import ScrapClient
            self._scrap_client = ScrapClient()
        return self._scrap_client

    @property
    def shenglong_client(self):
        """惰性实例化 ShenglongClient（盛隆）"""
        if self._shenglong_client is None:
            from agent.shenglong.client import ShenglongClient
            self._shenglong_client = ShenglongClient()
        return self._shenglong_client

    # ------------------------------------------------------------------
    #  主入口
    # ------------------------------------------------------------------
    def chat(
        self,
        user_message: str,
        session: Dict[str, Any],
    ) -> Tuple[str, Dict[str, Any]]:
        """
        处理一条用户消息。

        Args:
            user_message: 用户输入文本
            session: 会话状态字典，由 Gradio gr.State 管理

        Returns:
            (回复文本, 更新后的 session)
        """
        if not self.client:
            return (
                "未配置智谱 API Key。请设置环境变量：\n"
                "`export ZHIPU_API_KEY=你的密钥`\n"
                "然后重启程序。",
                session,
            )

        if "messages" not in session:
            session["messages"] = []

        vpn_state = session.get("vpn_state", "unknown")

        if vpn_state == "waiting_code":
            return self._handle_vpn_input(user_message, session)

        if vpn_state == "waiting_confirm":
            return self._handle_vpn_confirm(user_message, session)

        if vpn_state == "waiting_bxsteel_creds":
            return self._handle_bxsteel_creds(user_message, session)

        return self._process_message(user_message, session)

    # ------------------------------------------------------------------
    #  VPN 交互处理
    # ------------------------------------------------------------------
    def _handle_vpn_input(
        self, user_input: str, session: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """处理 VPN 等待验证码阶段的用户输入"""
        stripped = user_input.strip()

        if stripped.isdigit() and len(stripped) == 6:
            success, msg = self.vpn.try_connect(stripped)
            if success:
                session["vpn_state"] = "connected"
                pending = session.pop("pending_query", None)
                if pending:
                    reply, session = self._process_message(pending, session)
                    return f"VPN 连接成功！\n\n{reply}", session
                return "VPN 连接成功！请问需要查询什么？", session
            else:
                session["vpn_state"] = "waiting_confirm"
                return msg, session

        if self._is_confirm(stripped):
            return self._handle_vpn_confirm(stripped, session)

        session["vpn_state"] = "unknown"
        session.pop("pending_query", None)
        return self._process_message(user_input, session)

    def _handle_vpn_confirm(
        self, user_input: str, session: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """处理用户回复"已连接"的确认"""
        if self._is_confirm(user_input.strip()):
            success, msg = self.vpn.verify_manual_connect()
            if success:
                session["vpn_state"] = "connected"
                pending = session.pop("pending_query", None)
                if pending:
                    reply, session = self._process_message(pending, session)
                    return f"VPN 已连通！\n\n{reply}", session
                return "VPN 已连通！请问需要查询什么？", session
            else:
                return msg, session

        session["vpn_state"] = "unknown"
        session.pop("pending_query", None)
        return self._process_message(user_input, session)

    @staticmethod
    def _is_confirm(text: str) -> bool:
        keywords = ["已连接", "连好了", "连接了", "连上了", "ok", "OK", "好了", "done"]
        return any(kw in text for kw in keywords)

    def _handle_bxsteel_creds(
        self, user_input: str, session: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        """解析用户回复的镔鑫凭证信息并执行下载"""
        import re

        pending = self._bxsteel_pending_args
        if not pending:
            session["vpn_state"] = "unknown"
            return self._process_message(user_input, session)

        text = user_input.strip()

        username = ""
        password = ""
        output_dir = ""

        m_uid = re.search(r"工号[：:]\s*(\S+)", text)
        if m_uid:
            username = m_uid.group(1).strip()

        m_pwd = re.search(r"密码[：:]\s*(\S+)", text)
        if m_pwd:
            password = m_pwd.group(1).strip()

        m_dir = re.search(r"保存地址[（(]?可选.*?[）)]?\s*[：:]\s*(.+)", text)
        if m_dir:
            output_dir = m_dir.group(1).strip()
        if not output_dir:
            m_dir2 = re.search(r"保存地址[（(]?可选.*?[）)]?\s*[：:]\s*$", text, re.MULTILINE)
            if m_dir2:
                output_dir = ""

        if not username or not password:
            return (
                "工号和密码是必填的，请按以下格式回复：\n\n"
                "```\n"
                "工号：022499\n"
                "密码：your_password\n"
                "保存地址（可选，留空=默认保存地址）：\n"
                "```",
                session,
            )

        pending["username"] = username
        pending["password"] = password
        if output_dir:
            pending["output_dir"] = output_dir

        session["vpn_state"] = "unknown"
        self._bxsteel_pending_args = None

        result = self._tool_bxsteel_download_images(**pending)
        session["messages"].append({
            "role": "assistant",
            "content": result.get("summary_text", "下载完成"),
        })
        return result.get("summary_text", "下载完成"), session

    # ------------------------------------------------------------------
    #  核心消息处理：LLM 意图解析 → 工具调用 → 格式化回复
    # ------------------------------------------------------------------
    def _process_message(
        self, message: str, session: Dict[str, Any]
    ) -> Tuple[str, Dict[str, Any]]:
        messages: List[Dict[str, Any]] = session["messages"]
        messages.append({"role": "user", "content": message})

        try:
            response = self.client.chat.completions.create(
                model=settings.llm.model,
                messages=[{"role": "system", "content": _build_system_prompt()}]
                + messages[-10:],
                tools=TOOLS,
            )
        except Exception as e:
            logger.error("GLM 调用失败: %s", e)
            messages.pop()
            return f"LLM 调用失败: {e}", session

        choice = response.choices[0]
        assistant_msg = choice.message

        if not assistant_msg.tool_calls:
            reply = assistant_msg.content or ""
            messages.append({"role": "assistant", "content": reply})
            return reply, session

        tool_names = [tc.function.name for tc in assistant_msg.tool_calls]
        # 盛隆图像下载、镔鑫球机图像下载工具不需要检查永锋 VPN
        any_packing_tool = any(
            not n.startswith("scrap_") and not n.startswith("shenglong_") and not n.startswith("bxsteel_") and n != "download_shenglong_images"
            for n in tool_names
        )

        # 只有当调用的工具涉及打包带时才检查打包带 VPN；
        # 镔鑫/盛隆废钢 VPN 由用户自行保证，代码不触碰
        if any_packing_tool and not self.vpn.check_connectivity():
            session["vpn_state"] = "waiting_code"
            session["pending_query"] = message
            messages.pop()
            return self.vpn.get_connection_prompt(), session

        if any_packing_tool:
            config_issues = settings.validate()
            api_issues = [i for i in config_issues if "接口" in i]
            if api_issues:
                messages.pop()
                return (
                    "视觉系统 API 尚未配置：\n- "
                    + "\n- ".join(api_issues)
                    + "\n\n请先完成 F12 抓包并填写 config/settings.py 中的 TODO 项。"
                ), session

        messages.append(self._msg_to_dict(assistant_msg))

        for tool_call in assistant_msg.tool_calls:
            func_name = tool_call.function.name

            # 镔鑫球机下载：如果没有凭证，拦截并弹出输入提示
            if func_name == "bxsteel_download_images":
                try:
                    args = json.loads(tool_call.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                if not args.get("username") or not args.get("password"):
                    self._bxsteel_pending_args = args
                    session["vpn_state"] = "waiting_bxsteel_creds"
                    messages.pop()
                    return (
                        "请提供镔鑫系统的登录凭证：\n\n"
                        "```\n"
                        "工号：\n"
                        "密码：\n"
                        "保存地址（可选，留空=默认保存地址）：\n"
                        "```",
                        session,
                    )

            result = self._execute_tool(tool_call)
            messages.append(
                {
                    "role": "tool",
                    "content": json.dumps(result, ensure_ascii=False),
                    "tool_call_id": tool_call.id,
                }
            )

        try:
            final = self.client.chat.completions.create(
                model=settings.llm.model,
                messages=[{"role": "system", "content": _build_system_prompt()}]
                + messages[-12:],
                tools=TOOLS,
            )
        except Exception as e:
            logger.error("GLM 二次调用失败: %s", e)
            return f"LLM 调用失败: {e}", session

        reply = final.choices[0].message.content or ""
        messages.append({"role": "assistant", "content": reply})
        return reply, session

    # ------------------------------------------------------------------
    #  工具执行
    # ------------------------------------------------------------------
    def _execute_tool(self, tool_call: Any) -> Dict[str, Any]:
        func_name = tool_call.function.name
        try:
            args = json.loads(tool_call.function.arguments)
        except json.JSONDecodeError:
            return {"error": f"参数解析失败: {tool_call.function.arguments}"}

        if func_name == "get_daily_stats":
            return self._tool_get_daily_stats(args.get("date", ""))

        if func_name == "get_date_range_stats":
            return self._tool_get_date_range_stats(
                args.get("start_date", ""), args.get("end_date", "")
            )

                # 盛隆图像下载工具
        if func_name == "download_shenglong_images":
            result_str = self._tool_download_shenglong_images(
                start_date=args.get("start_date", ""),
                end_date=args.get("end_date", ""),
                output_dir=args.get("output_dir", None)
            )
            return {"summary_text": result_str}

        # =====================================================
        # 废钢工具分支
        # =====================================================
        if func_name == "scrap_get_daily_summary":
            return self._tool_scrap_daily(args.get("date", ""))

        if func_name == "scrap_get_range_summary":
            return self._tool_scrap_range(
                args.get("start_date", ""), args.get("end_date", "")
            )

        if func_name == "scrap_export_report":
            return self._tool_scrap_export(
                args.get("start_date", ""),
                args.get("end_date", ""),
                bool(args.get("download_error_images", True)),
            )

        if func_name == "scrap_export_ppt":
            return self._tool_scrap_export_ppt(
                args.get("start_date", ""),
                args.get("end_date", ""),
            )

        if func_name == "scrap_weekly_type_stats":
            return self._tool_scrap_weekly_type(
                args.get("start_date", ""),
                args.get("end_date", ""),
            )

        # =====================================================
        # 盛隆废钢工具分支
        # =====================================================
        if func_name == "shenglong_get_daily_summary":
            return self._tool_shenglong_daily(args.get("date", ""))

        if func_name == "shenglong_get_range_summary":
            return self._tool_shenglong_range(
                args.get("start_date", ""), args.get("end_date", "")
            )

        if func_name == "shenglong_export_report":
            return self._tool_shenglong_export(
                args.get("start_date", ""),
                args.get("end_date", ""),
                prev_start_date=args.get("prev_start_date") or None,
                prev_end_date=args.get("prev_end_date") or None,
            )

        if func_name == "shenglong_export_master_report":
            return self._tool_shenglong_export_master(
                args.get("cycles") or []
            )

        if func_name == "shenglong_export_heavy_master_report":
            return self._tool_shenglong_export_master(
                args.get("cycles") or [],
                heavy_normalized=True,
            )

        if func_name == "yongfeng_export_accuracy_report":
            return self._tool_yongfeng_export_accuracy_report(args)

        if func_name == "yongfeng_export_report":
            return self._tool_yongfeng_export_report(args)

        if func_name == "bxsteel_download_images":
            return self._tool_bxsteel_download_images(
                args.get("start_date", ""),
                args.get("end_date", ""),
                args.get("username", ""),
                args.get("password", ""),
                args.get("output_dir") or None,
            )

        return {"error": f"未知工具: {func_name}"}

    def _tool_get_daily_stats(self, date_str: str) -> Dict[str, Any]:
        try:
            stats = self.fetcher.get_daily_stats(date_str)
        except Exception as e:
            logger.error("获取统计数据失败: %s", e)
            return {"error": f"数据获取失败: {e}"}

        if stats is None:
            return {"error": f"{date_str} 暂无数据"}

        return {
            "date": stats.date,
            "total": stats.total,
            "normal": stats.normal,
            "abnormal": stats.abnormal,
            "unrecognized": stats.unrecognized,
            "over_5_strips": stats.over_5_strips,
            "abnormal_diff_1": stats.abnormal_diff_1,
            "abnormal_diff_gt1": stats.abnormal_diff_gt1,
        }

    def _tool_get_date_range_stats(
        self, start_date: str, end_date: str
    ) -> Any:
        try:
            stats_list = self.fetcher.get_date_range_stats(start_date, end_date)
        except Exception as e:
            logger.error("获取日期范围统计失败: %s", e)
            return {"error": f"数据获取失败: {e}"}

        results = []
        for stats in stats_list:
            if stats is None:
                continue
            results.append(
                {
                    "date": stats.date,
                    "total": stats.total,
                    "normal": stats.normal,
                    "abnormal": stats.abnormal,
                    "unrecognized": stats.unrecognized,
                    "over_5_strips": stats.over_5_strips,
                    "abnormal_diff_1": stats.abnormal_diff_1,
                    "abnormal_diff_gt1": stats.abnormal_diff_gt1,
                }
            )

        if not results:
            return {"error": f"{start_date} 至 {end_date} 暂无数据"}
        return results

    def _tool_download_abnormal_images(self, date_str: str) -> Dict[str, Any]:
        try:
            result = self.fetcher.download_abnormal_images(date_str)
        except Exception as e:
            logger.error("下载异常图片失败: %s", e)
            return {"error": f"下载失败: {e}"}
        return result

    # ------------------------------------------------------------------
    #  废钢工具实现
    # ------------------------------------------------------------------
    def _tool_scrap_daily(self, date_str: str) -> Dict[str, Any]:
        try:
            stats = self.scrap_client.build_daily_stats(date_str)
        except Exception as e:
            logger.error("废钢日统计失败 %s: %s", date_str, e)
            return {"error": f"废钢数据获取失败: {e}"}
        cfg = settings.scrap
        return {
            "date": stats.date,
            "total_trucks": stats.total_trucks,
            "eligible_trucks": stats.eligible_trucks,
            "main_same_count": stats.main_same_count,
            "accuracy_rate_pct": stats.accuracy_rate,
            "avg_error_rate_pct": stats.avg_error_rate,
            "avg_weight_diff_kg": stats.avg_weight_diff,
            "avg_weight_ratio": stats.avg_weight_ratio,
            "summary_text": stats.summary_text(
                target_accuracy=cfg.target_accuracy,
                target_avg_error_rate=cfg.target_avg_error_rate,
                target_weight_diff_kg=cfg.target_weight_diff_kg,
                target_weight_ratio_lower=cfg.target_weight_ratio_lower,
                target_weight_ratio_upper=cfg.target_weight_ratio_upper,
            ),
        }

    def _tool_scrap_range(
        self, start_date: str, end_date: str
    ) -> Dict[str, Any]:
        try:
            stats_list = self.scrap_client.build_range_stats(start_date, end_date)
        except Exception as e:
            logger.error("废钢范围统计失败 %s~%s: %s", start_date, end_date, e)
            return {"error": f"废钢数据获取失败: {e}"}

        cfg = settings.scrap
        daily = []
        summary_blocks: List[str] = []
        for stats in stats_list:
            text = stats.summary_text(
                target_accuracy=cfg.target_accuracy,
                target_avg_error_rate=cfg.target_avg_error_rate,
                target_weight_diff_kg=cfg.target_weight_diff_kg,
                target_weight_ratio_lower=cfg.target_weight_ratio_lower,
                target_weight_ratio_upper=cfg.target_weight_ratio_upper,
            )
            summary_blocks.append(text)
            daily.append(
                {
                    "date": stats.date,
                    "total_trucks": stats.total_trucks,
                    "eligible_trucks": stats.eligible_trucks,
                    "main_same_count": stats.main_same_count,
                    "accuracy_rate_pct": stats.accuracy_rate,
                    "avg_error_rate_pct": stats.avg_error_rate,
                    "avg_weight_diff_kg": stats.avg_weight_diff,
                    "avg_weight_ratio": stats.avg_weight_ratio,
                }
            )
        return {
            "start_date": start_date,
            "end_date": end_date,
            "daily": daily,
            "summary_text": "\n\n---\n\n".join(summary_blocks),
        }

    def _tool_scrap_export(
        self, start_date: str, end_date: str, download_error_images: bool
    ) -> Dict[str, Any]:
        from agent.scrap.excel_writer import write_stats_xlsx

        try:
            stats_list = self.scrap_client.build_range_stats(start_date, end_date)
        except Exception as e:
            logger.error("废钢导出取数失败: %s", e)
            return {"error": f"废钢数据获取失败: {e}"}

        if start_date == end_date:
            out_root = Path("downloads/scrap") / start_date
            xlsx_name = f"赛迪废钢判级_{start_date}.xlsx"
        else:
            out_root = Path("downloads/scrap") / f"{start_date}_{end_date}"
            xlsx_name = f"赛迪废钢判级_{start_date}_{end_date}.xlsx"
        out_root.mkdir(parents=True, exist_ok=True)
        xlsx_path = out_root / xlsx_name
        try:
            write_stats_xlsx(stats_list, xlsx_path)
        except Exception as e:
            logger.error("xlsx 生成失败: %s", e)
            return {"error": f"报表生成失败: {e}"}

        image_report = []
        total_imgs = 0
        if download_error_images:
            for stats in stats_list:
                day_dir = out_root / stats.date
                day_downloaded: List[str] = []
                for truck in stats.trucks:
                    if truck.main_same is False and truck.error_render_images:
                        for i, url in enumerate(truck.error_render_images, start=1):
                            suffix = Path(url.split("?")[0]).suffix or ".jpg"
                            fname = (
                                f"{truck.car_number}_工位{truck.station_number}"
                                f"_{i}{suffix}"
                            )
                            fpath = day_dir / fname
                            ok = self.scrap_client.download_image(url, fpath)
                            if ok:
                                day_downloaded.append(str(fpath))
                                total_imgs += 1
                image_report.append(
                    {
                        "date": stats.date,
                        "count": len(day_downloaded),
                        "dir": str(day_dir) if day_downloaded else "",
                    }
                )

        cfg = settings.scrap
        summary_blocks = [
            s.summary_text(
                target_accuracy=cfg.target_accuracy,
                target_avg_error_rate=cfg.target_avg_error_rate,
                target_weight_diff_kg=cfg.target_weight_diff_kg,
                target_weight_ratio_lower=cfg.target_weight_ratio_lower,
                target_weight_ratio_upper=cfg.target_weight_ratio_upper,
            )
            for s in stats_list
        ]

        return {
            "xlsx_path": str(xlsx_path),
            "downloaded_images": image_report,
            "total_images": total_imgs,
            "summary_text": "\n\n---\n\n".join(summary_blocks),
        }

    def _tool_scrap_export_ppt(
        self, start_date: str, end_date: str
    ) -> Dict[str, Any]:
        """镔鑫专用：生成单页 PPT 汇报页（自研 builder，含主料识别率趋势 +
        目标线 + KPI 卡 + 错判 Top5 + 改进建议；fallback 同事 skill）。
        """
        from agent.scrap.ppt_writer import PPTGenerationError, write_stats_pptx

        try:
            stats_list = self.scrap_client.build_range_stats(start_date, end_date)
        except Exception as e:
            logger.error("镔鑫 PPT 取数失败: %s", e)
            return {"error": f"镔鑫废钢数据获取失败: {e}"}

        if start_date == end_date:
            out_root = Path("downloads/scrap") / start_date
            pptx_name = f"镔鑫废钢汇报_{start_date}.pptx"
        else:
            out_root = Path("downloads/scrap") / f"{start_date}_{end_date}"
            pptx_name = f"镔鑫废钢汇报_{start_date}_{end_date}.pptx"
        out_root.mkdir(parents=True, exist_ok=True)
        pptx_path = out_root / pptx_name

        cfg = settings.scrap
        try:
            write_stats_pptx(
                stats_list,
                pptx_path,
                start_date=start_date,
                end_date=end_date,
                target_pct=cfg.target_accuracy * 100.0,
                source_label="镔鑫废钢检判系统（赛迪 AI vs 人工质检）",
            )
        except PPTGenerationError as e:
            logger.error("PPT 生成失败: %s", e)
            return {"error": f"PPT 生成失败: {e}"}
        except Exception as e:
            logger.exception("PPT 生成意外异常")
            return {"error": f"PPT 生成异常: {e}"}

        eligible_days = sum(1 for s in stats_list if s.eligible_trucks > 0)
        total_eligible = sum(s.eligible_trucks for s in stats_list)
        total_correct = sum(s.main_same_count for s in stats_list)
        overall_acc = (
            (total_correct / total_eligible * 100.0) if total_eligible > 0 else None
        )
        return {
            "site": "镔鑫钢铁",
            "pptx_path": str(pptx_path),
            "date_range": (
                start_date if start_date == end_date else f"{start_date} ～ {end_date}"
            ),
            "days_with_data": eligible_days,
            "total_eligible_trucks": total_eligible,
            "overall_accuracy_pct": overall_acc,
            "metric": "主料识别率",
            "note": (
                "已生成单页 PowerPoint 趋势图，含可编辑图表 + 任务判断 / 图表结构 / "
                "关键观察 / 使用建议四个文字面板。直接打开 .pptx 文件即可演示。"
            ),
        }

    def _tool_scrap_weekly_type(self, start_date: str, end_date: str) -> Dict[str, Any]:
        from agent.scrap.calculator import calc_weekly_type_stats
        from agent.scrap.excel_writer import write_weekly_type_xlsx

        try:
            stats_list = self.scrap_client.build_range_stats(start_date, end_date)
        except Exception as e:
            logger.error("废钢周报料型统计取数失败: %s", e)
            return {"error": f"废钢数据获取失败: {e}"}

        all_trucks = []
        for day in stats_list:
            all_trucks.extend(day.trucks)

        saidi_report = calc_weekly_type_stats(
            all_trucks, start_date, end_date, source_filter=1,
        )

        summary = _build_weekly_summary(saidi_report, start_date, end_date)

        if start_date == end_date:
            out_root = Path("downloads/scrap") / start_date
            weekly_xlsx_name = f"镔鑫料型统计_{start_date}.xlsx"
        else:
            out_root = Path("downloads/scrap") / f"{start_date}_{end_date}"
            weekly_xlsx_name = f"镔鑫料型统计_{start_date}_{end_date}.xlsx"
        out_root.mkdir(parents=True, exist_ok=True)
        weekly_xlsx_path = out_root / weekly_xlsx_name
        try:
            write_weekly_type_xlsx(saidi_report, weekly_xlsx_path)
        except Exception as e:
            logger.exception("镔鑫周度料型统计 xlsx 生成失败")
            return {"error": f"料型统计报表生成失败: {e}"}

        logger.info(
            "[镔鑫周度料型] %s~%s 赛迪 %d车 准确率 %.2f%%",
            start_date, end_date,
            saidi_report.no_exclude_truck_count,
            saidi_report.no_exclude_main_same_pct or 0,
        )

        return {
            "site": "镔鑫钢铁",
            "weekly_xlsx_path": str(weekly_xlsx_path),
            "date_range": (
                start_date if start_date == end_date else f"{start_date} ～ {end_date}"
            ),
            "weekly_summary_text": summary,
        }

    # ------------------------------------------------------------------
    #  盛隆废钢工具实现
    # ------------------------------------------------------------------
    def _tool_shenglong_daily(self, date_str: str) -> Dict[str, Any]:
        try:
            stats = self.shenglong_client.build_daily_stats(date_str)
        except Exception as e:
            logger.error("盛隆废钢日统计失败 %s: %s", date_str, e)
            return {"error": f"盛隆废钢数据获取失败: {e}"}
        cfg = settings.shenglong
        return {
            "site": "盛隆钢铁",
            "date": stats.date,
            "total_trucks": stats.total_trucks,
            "judgable_trucks": stats.judgable_trucks,
            "main_same_count": stats.main_same_count,
            "recognition_rate_pct": stats.recognition_rate,
            "deduction_evaluable": stats.deduction_evaluable,
            "deduction_compliant_count": stats.deduction_compliant_count,
            "deduction_compliance_rate_pct": stats.deduction_compliance_rate,
            "summary_text": stats.summary_text(
                target_recognition_rate=cfg.target_recognition_rate,
                target_deduction_compliance_rate=cfg.target_deduction_compliance_rate,
            ),
        }

    def _tool_shenglong_range(
        self, start_date: str, end_date: str
    ) -> Dict[str, Any]:
        try:
            stats_list = self.shenglong_client.build_range_stats(
                start_date, end_date
            )
        except Exception as e:
            logger.error(
                "盛隆废钢范围统计失败 %s~%s: %s", start_date, end_date, e
            )
            return {"error": f"盛隆废钢数据获取失败: {e}"}

        cfg = settings.shenglong
        daily: List[Dict[str, Any]] = []
        summary_blocks: List[str] = []
        for stats in stats_list:
            text = stats.summary_text(
                target_recognition_rate=cfg.target_recognition_rate,
                target_deduction_compliance_rate=cfg.target_deduction_compliance_rate,
            )
            summary_blocks.append(text)
            daily.append(
                {
                    "date": stats.date,
                    "total_trucks": stats.total_trucks,
                    "judgable_trucks": stats.judgable_trucks,
                    "main_same_count": stats.main_same_count,
                    "recognition_rate_pct": stats.recognition_rate,
                    "deduction_evaluable": stats.deduction_evaluable,
                    "deduction_compliant_count": stats.deduction_compliant_count,
                    "deduction_compliance_rate_pct": stats.deduction_compliance_rate,
                }
            )
        return {
            "site": "盛隆钢铁",
            "start_date": start_date,
            "end_date": end_date,
            "daily": daily,
            "summary_text": "\n\n---\n\n".join(summary_blocks),
        }

    def _tool_shenglong_export(
        self,
        start_date: str,
        end_date: str,
        prev_start_date: Optional[str] = None,
        prev_end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """盛隆废钢导出 xlsx。可选传入上周期日期范围，用于在 Sheet1 写入"环比"。

        Args:
            start_date / end_date: 本周期日期范围
            prev_start_date / prev_end_date: 上周期范围；同时给出才生效
        """
        from agent.shenglong.calculator import aggregate_period
        from agent.shenglong.excel_writer import write_stats_xlsx

        try:
            stats_list = self.shenglong_client.build_range_stats(
                start_date, end_date
            )
        except Exception as e:
            logger.error("盛隆废钢导出取数失败: %s", e)
            return {"error": f"盛隆废钢数据获取失败: {e}"}

        if start_date == end_date:
            out_root = Path("downloads/shenglong") / start_date
            xlsx_name = f"盛隆赛迪废钢判级_{start_date}.xlsx"
        else:
            out_root = Path("downloads/shenglong") / f"{start_date}_{end_date}"
            xlsx_name = f"盛隆赛迪废钢判级_{start_date}_{end_date}.xlsx"
        out_root.mkdir(parents=True, exist_ok=True)
        xlsx_path = out_root / xlsx_name

        cfg = settings.shenglong
        period = aggregate_period(stats_list, start_date, end_date)

        # 上周期对比：如果提供，再取一次数算上周期识别率/扣重符合率，挂到 period 上
        if prev_start_date and prev_end_date:
            try:
                prev_stats_list = self.shenglong_client.build_range_stats(
                    prev_start_date, prev_end_date
                )
                prev_period = aggregate_period(
                    prev_stats_list, prev_start_date, prev_end_date
                )
                pr = prev_period.recognition_rate_pct
                pc = prev_period.deduction_compliance_rate_pct
                period.prev_recognition_rate = (
                    pr / 100.0 if pr is not None else None
                )
                period.prev_deduction_compliance_rate = (
                    pc / 100.0 if pc is not None else None
                )
                period.prev_cycle_label = (
                    f"{prev_start_date} ~ {prev_end_date}"
                )
                logger.info(
                    "盛隆环比：本周期 R=%.2f%% / 扣重=%.2f%%；上周期 R=%s / 扣重=%s",
                    period.recognition_rate_pct or 0,
                    period.deduction_compliance_rate_pct or 0,
                    f"{pr:.2f}%" if pr is not None else "N/A",
                    f"{pc:.2f}%" if pc is not None else "N/A",
                )
            except Exception as e:
                logger.warning("上周期对比数据获取失败（仅影响 F/G 列）: %s", e)

        try:
            write_stats_xlsx(
                stats_list,
                xlsx_path,
                target_recognition_rate=cfg.target_recognition_rate,
                target_deduction_compliance_rate=cfg.target_deduction_compliance_rate,
                period_summary=period,
            )
        except Exception as e:
            logger.error("盛隆 xlsx 生成失败: %s", e)
            return {"error": f"盛隆报表生成失败: {e}"}

        summary_blocks = [
            s.summary_text(
                target_recognition_rate=cfg.target_recognition_rate,
                target_deduction_compliance_rate=cfg.target_deduction_compliance_rate,
            )
            for s in stats_list
        ]
        return {
            "site": "盛隆钢铁",
            "xlsx_path": str(xlsx_path),
            "note": (
                "已生成三 sheet 报表：Sheet1「统计周期概括」(识别准确率 + 扣杂符合率 + "
                "价格差异分布) / Sheet2「累计统计」(总览 + Tol 合计) / "
                "Sheet3「检判统计详情」(单车明细)。试运行阶段未下载错判图像。"
            ),
            "period": {
                "cycle_label": period.cycle_label,
                "judgable_trucks": period.judgable_trucks,
                "main_name_match_count": period.main_name_match_count,
                "main_within_10pct_count": period.main_within_10pct_count,
                "recognition_rate_pct": period.recognition_rate_pct,
                "deduction_compliant_count": period.deduction_compliant_count,
                "deduction_evaluable": period.deduction_evaluable,
                "deduction_compliance_rate_pct": period.deduction_compliance_rate_pct,
                "price_diff_lt30": period.price_diff_lt30,
                "price_diff_30_50": period.price_diff_30_50,
                "price_diff_50_100": period.price_diff_50_100,
                "price_diff_gt100": period.price_diff_gt100,
            },
            "summary_text": "\n\n---\n\n".join(summary_blocks),
        }

    def _tool_shenglong_export_master(
        self,
        cycles_input: List[Dict[str, Any]],
        *,
        heavy_normalized: bool = False,
    ) -> Dict[str, Any]:
        """盛隆废钢多周期主表：把多个周期累积到一个 xlsx。

        Args:
            cycles_input: 统计周期列表。推荐结构：
                [{ranges:[{start_date,end_date}, ...]}, ...]
                每个 cycle 就是 Excel 中的一个统计周期；只有显式合并时
                一个 cycle.ranges 才会包含多个日期段。

        Sheet1 多个 14 行块依次往下；Sheet2 是累计统计；Sheet3 每个有效周期一段；
        环比自动链；累计准确率/符合率从首期累计到当期。
        """
        from agent.shenglong.calculator import (
            aggregate_period,
            aggregate_period_heavy_normalized,
            to_heavy_normalized_view,
        )
        aggregate_func = (
            aggregate_period_heavy_normalized if heavy_normalized else aggregate_period
        )
        metric_label = "重废1/2/3归一化准确率" if heavy_normalized else "主料识别准确率"

        if not cycles_input:
            return {"error": "cycles 为空，至少需要 1 个周期"}

        # ---- 1. 解析 effective cycles ----
        # 新结构：cycles=[{ranges:[{start_date,end_date}, ...]}, ...]
        # 旧结构：cycles=[{start_date,end_date,merge_with_next?}, ...]
        # 为避免 LLM 误传 merge_with_next 导致相邻周期被错误合并，旧结构一律按独立周期处理。
        groups: List[List[Dict[str, str]]] = []
        for c in cycles_input:
            ranges = c.get("ranges")
            if isinstance(ranges, list) and ranges:
                group: List[Dict[str, str]] = []
                for r in ranges:
                    if not isinstance(r, dict):
                        return {"error": f"周期 ranges 参数非法：{c}"}
                    sd = str(r.get("start_date") or "")
                    ed = str(r.get("end_date") or "")
                    if not sd or not ed:
                        return {"error": f"周期参数不完整：{c}"}
                    group.append({"start_date": sd, "end_date": ed})
                groups.append(group)
                continue

            # 兼容旧输入，但不再支持通过 merge_with_next 合并，防止误合并。
            sd = str(c.get("start_date") or "")
            ed = str(c.get("end_date") or "")
            if not sd or not ed:
                return {"error": f"周期参数不完整：{c}"}
            groups.append([{"start_date": sd, "end_date": ed}])

        # ---- 2. 每组取数 → 合并 stats_list（按日期去重）→ 算 period ----
        cycles_data = []
        for group in groups:
            merged_by_date: Dict[str, Any] = {}
            for seg in group:
                sd = seg["start_date"]
                ed = seg["end_date"]
                try:
                    stats_list = self.shenglong_client.build_range_stats(sd, ed)
                except Exception as e:
                    logger.error("盛隆主表取数失败 %s~%s: %s", sd, ed, e)
                    return {"error": f"段 {sd}~{ed} 数据获取失败: {e}"}
                for day in stats_list:
                    # 同一日期重复出现取后者（理论上不会重复，因为段不应重叠）
                    merged_by_date[day.date] = day

            merged_stats = [merged_by_date[d] for d in sorted(merged_by_date)]
            group_start = group[0]["start_date"]
            group_end = group[-1]["end_date"]
            period = aggregate_func(merged_stats, group_start, group_end)
            report_stats = (
                to_heavy_normalized_view(merged_stats)
                if heavy_normalized
                else merged_stats
            )

            # 多段合并时改写 cycle_label 标明组合关系
            if len(group) > 1:
                seg_labels = " + ".join(
                    f"{s['start_date']}~{s['end_date']}" for s in group
                )
                period.cycle_label = (
                    f"{group_start.replace('-', '.')} 至 {group_end.replace('-', '.')}"
                    f"（合并：{seg_labels}）"
                )
            cycles_data.append((report_stats, period))

        # ---- 3. 写主表 ----
        first_start = groups[0][0]["start_date"]
        last_end = groups[-1][-1]["end_date"]
        out_root = Path("downloads/shenglong/master")
        out_root.mkdir(parents=True, exist_ok=True)
        suffix = "_重废归一化" if heavy_normalized else ""
        xlsx_name = f"盛隆赛迪废钢判级_主表{suffix}_{first_start}_{last_end}.xlsx"
        xlsx_path = out_root / xlsx_name

        cfg = settings.shenglong
        try:
            from agent.shenglong.excel_writer import write_master_xlsx
            write_master_xlsx(
                cycles_data,
                xlsx_path,
                target_recognition_rate=cfg.target_recognition_rate,
                target_deduction_compliance_rate=cfg.target_deduction_compliance_rate,
            )
        except Exception as e:
            logger.exception("盛隆主表生成失败")
            return {"error": f"盛隆主表生成失败: {e}"}

        # ---- 4. 拼回包：每周期关键指标（含累计指标）----
        cycle_briefs = []
        for _stats_list, period in cycles_data:
            cycle_briefs.append({
                "cycle_label": period.cycle_label,
                "metric_label": metric_label,
                "judgable_trucks": period.judgable_trucks,
                "main_within_10pct_count": period.main_within_10pct_count,
                "recognition_rate_pct": period.recognition_rate_pct,
                "deduction_compliant_count": period.deduction_compliant_count,
                "deduction_evaluable": period.deduction_evaluable,
                "deduction_compliance_rate_pct": period.deduction_compliance_rate_pct,
      
                "prev_recognition_rate": period.prev_recognition_rate,
                "prev_deduction_compliance_rate": period.prev_deduction_compliance_rate,
                "cumulative_recognition_rate": period.cumulative_recognition_rate,
                "cumulative_deduction_compliance_rate": period.cumulative_deduction_compliance_rate,
            })

        merged_count = sum(1 for g in groups if len(g) > 1)
        merge_hint = (
            f"，其中 {merged_count} 个周期由多段日期合并而成"
            if merged_count else ""
        )
        return {
            "site": "盛隆钢铁",
            "xlsx_path": str(xlsx_path),
            "cycle_count": len(cycles_data),
            "metric_label": metric_label,
            "cycles": cycle_briefs,
            "note": (
                f"已生成包含 {len(cycles_data)} 个有效周期的盛隆主表{merge_hint}："
                f"识别率口径为「{metric_label}」；"
                f"Sheet1 含 {len(cycles_data)} 个 14 行块；"
                "Sheet2 为「累计统计」总览和 Tol 合计；"
                "Sheet3 每周期一段；Sheet1 仍保留「累计准确率/符合率」列（首期累计到当期）。"
            ),
        }

    def _tool_download_shenglong_images(self, start_date: str, end_date: str, output_dir: str = None) -> Dict[str, Any]:
        """下载盛隆工厂监控图像并打包为 ZIP"""
        from agent.shenglong.minio_downloader import download_and_pack
        from datetime import datetime, timedelta
        from minio import Minio
        from pathlib import Path
        
        try:
            # 获取文件总数（用于显示）
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
            
            from agent.shenglong.minio_downloader import MINIO_HOST, MINIO_API_PORT, BUCKET, PREFIX_BASE, ACCESS_KEY, SECRET_KEY
            
            client = Minio(
                f"{MINIO_HOST}:{MINIO_API_PORT}",
                access_key=ACCESS_KEY,
                secret_key=SECRET_KEY,
                secure=False,
            )
            
            total_files = 0
            current = start
            while current <= end:
                prefix = f"{PREFIX_BASE}/{current.isoformat()}/"
                try:
                    objects = list(client.list_objects(BUCKET, prefix=prefix, recursive=True))
                    files = [o for o in objects if o.size and o.size > 0]
                    total_files += len(files)
                except:
                    pass
                current += timedelta(days=1)
            
            start_msg = f"📥 正在下载 {start_date} 到 {end_date} 的监控图像，共 {total_files} 个文件\n\n点击「📋 下载实时日志」面板中的「🔄 手动刷新日志」按钮查看下载进度。\n\n下载完成后我会通知你，并提供 ZIP 下载链接。"
            
            zip_path, success, failed = download_and_pack(start_date, end_date)
            
            if zip_path is None:
                return {"summary_text": f"❌ 下载失败：没有找到文件"}
            
            final_msg = f"\n\n✅ 下载完成！成功 {success} 个文件，失败 {failed} 个\n\n点击下方按钮下载 ZIP 文件到本地。"
            
            return {
                "summary_text": start_msg + final_msg,
                "zip_file": zip_path
            }
        except Exception as e:
            logger.error(f"下载图像失败: {e}")
            return {"summary_text": f"❌ 下载失败: {e}"}


    def _tool_yongfeng_export_accuracy_report(self, args: Dict[str, Any]) -> Dict[str, Any]:
        required_fields = ["start_time", "end_time"]
        missing = [name for name in required_fields if not str(args.get(name) or "").strip()]
        if missing:
            return {"error": f"永锋准确率报表参数缺失: {', '.join(missing)}"}

        try:
            return run_yongfeng_report(
                analysis_base_url=str(settings.yongfeng.analysis_base_url).strip(),
                visual_1_base_url=str(settings.yongfeng.visual_1_base_url).strip(),
                visual_2_base_url=str(settings.yongfeng.visual_2_base_url).strip(),
                start_time=str(args.get("start_time")).strip(),
                end_time=str(args.get("end_time")).strip(),
                mat_code_1=str(args.get("mat_code_1") or "12031001").strip(),
                mat_code_2=str(args.get("mat_code_2") or "12031002").strip(),
                analysis_token=settings.yongfeng.analysis_token,
                api_code=settings.yongfeng.api_code,
                output=str(args.get("output") or "").strip() or None,
                verbose=bool(args.get("verbose", False)),
            )
        except Exception as e:
            logger.error("永锋准确率报表生成失败: %s", e)
            return {"error": f"永锋准确率报表生成失败: {e}"}

    def _tool_yongfeng_export_report(self, args: Dict[str, Any]) -> Dict[str, Any]:
        required_fields = ["analysis_base_url", "visual_1_base_url", "visual_2_base_url", "start_time", "end_time"]
        missing = [name for name in required_fields if not str(args.get(name) or "").strip()]
        if missing:
            return {"error": f"永锋报表参数缺失: {', '.join(missing)}"}

        try:
            return run_yongfeng_report(
                analysis_base_url=str(args.get("analysis_base_url")).strip(),
                visual_1_base_url=str(args.get("visual_1_base_url")).strip(),
                visual_2_base_url=str(args.get("visual_2_base_url")).strip(),
                start_time=str(args.get("start_time")).strip(),
                end_time=str(args.get("end_time")).strip(),
                mat_code_1=str(args.get("mat_code_1") or "12031001").strip(),
                mat_code_2=str(args.get("mat_code_2") or "12031002").strip(),
                analysis_token=settings.yongfeng.analysis_token,
                api_code=settings.yongfeng.api_code,
                output=str(args.get("output") or "").strip() or None,
                verbose=bool(args.get("verbose", False)),
            )
        except Exception as e:
            logger.error("永锋报表生成失败: %s", e)
            return {"error": f"永锋报表生成失败: {e}"}

    def _tool_bxsteel_download_images(
        self, start_date: str, end_date: str, username: str, password: str,
        output_dir: str = None,
    ) -> Dict[str, Any]:
        try:
            from agent.bxsteel.config import create_settings
            from agent.bxsteel.pipeline import run as run_bxsteel

            dl_dir = Path(output_dir) if output_dir else None
            settings_bx = create_settings(
                username=username.strip(),
                password=password.strip(),
                base_url="http://172.31.1.102:8081",
                download_dir=dl_dir,
            )

            reports = run_bxsteel(
                settings=settings_bx,
                start_date=start_date,
                end_date=end_date,
            )

            lines: list[str] = []
            lines.append("==================== 汇总 ====================")
            total_saved = 0
            total_failed = 0
            total_skipped_no_manual = 0
            total_skipped_no_images = 0
            total_skipped_existing = 0
            for r in reports:
                lines.append(
                    f"{r.date}: 车辆 {r.processed}/{r.total_trucks} 处理，"
                    f"保存 {r.saved_files} 张，"
                    f"跳过已有 {r.skipped_existing} 张，"
                    f"失败 {r.failed_files} 张；"
                    f"无人工判级 {r.skipped_no_manual}，无原图 {r.skipped_no_images}"
                )
                total_saved += r.saved_files
                total_failed += r.failed_files
                total_skipped_no_manual += r.skipped_no_manual
                total_skipped_no_images += r.skipped_no_images
                total_skipped_existing += r.skipped_existing
            lines.append("==============================================")
            lines.append(f"\n图片保存目录: {settings_bx.download_dir}")
            info = "\n".join(lines)

            return {
                "site": "镔鑫钢铁",
                "summary_text": (
                    f"下载完成！共 {total_saved} 张图片保存成功。\n{info}"
                ),
            }
        except Exception as e:
            logger.exception("bxsteel download failed")
            return {
                "site": "镔鑫钢铁",
                "summary_text": f"下载失败：{e}",
            }


    # ------------------------------------------------------------------
    #  辅助
    # ------------------------------------------------------------------
    @staticmethod
    def _msg_to_dict(msg: Any) -> Dict[str, Any]:
        """将 GLM 返回的 assistant message 转为可序列化的 dict"""
        d: Dict[str, Any] = {"role": "assistant", "content": msg.content or ""}
        if msg.tool_calls:
            d["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        return d
