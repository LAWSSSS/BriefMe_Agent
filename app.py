"""BriefMe · 多场景数据统计助手（by 中冶赛迪） —— Gradio 入口

UI 五档一次到位：
  档位 1: 主题 (Soft / 深蓝-橙色 / Inter 字体)
  档位 2: 顶部品牌栏 + VPN 状态灯
  档位 3: 左右分栏（侧边栏+聊天）
  档位 4: 预设 prompt 快捷按钮
  档位 5: 产物下载面板（xlsx + 错判图 Gallery）

Agent 业务逻辑完全不改，仅重写 app.py 布局。
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import gradio as gr
import httpx

from agent.core import SteelCoilAgent
from config.settings import settings

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)

agent = SteelCoilAgent()

DOWNLOADS_ROOT = Path("downloads")
SCRAP_ROOT = DOWNLOADS_ROOT / "scrap"
SHENGLONG_ROOT = DOWNLOADS_ROOT / "shenglong"
YONGFENG_ROOT = DOWNLOADS_ROOT / "yongfeng"
BXSTEEL_ROOT = DOWNLOADS_ROOT / "bxsteel"


# =====================================================================
# VPN 状态探测
# =====================================================================
def _check_packing_vpn() -> bool:
    """打包带 VPN 是否连通（简单探测业务域名）"""
    try:
        return agent.vpn.check_connectivity()
    except Exception:
        return False


def _check_scrap_vpn() -> bool:
    """镔鑫废钢 VPN 是否连通（3s 内能访问前端页）"""
    try:
        r = httpx.get(
            f"{settings.scrap.base_url}/fcs-web/",
            timeout=3.0,
            follow_redirects=True,
        )
        return r.status_code < 500
    except Exception:
        return False


def _check_shenglong_vpn() -> bool:
    """盛隆废钢 VPN 是否连通（3s 内能访问前端根页）"""
    try:
        r = httpx.get(
            f"{settings.shenglong.base_url}/",
            timeout=3.0,
            follow_redirects=True,
        )
        return r.status_code < 500
    except Exception:
        return False


def _status_html() -> str:
    today_str = date.today().strftime("%Y-%m-%d")
    pt_ok = _check_packing_vpn()
    sc_ok = _check_scrap_vpn()
    sl_ok = _check_shenglong_vpn()
    pt_badge = _badge("打包带 VPN", pt_ok)
    sc_badge = _badge("镔鑫 VPN", sc_ok)
    sl_badge = _badge("盛隆 VPN", sl_ok)
    return (
        f'<div class="status-bar">'
        f'<span class="today-chip">📅 {today_str}</span>'
        f"{pt_badge}{sc_badge}{sl_badge}"
        f"</div>"
    )


def _badge(label: str, ok: bool) -> str:
    cls = "ok" if ok else "fail"
    text = "已连接" if ok else "未连接"
    return (
        f'<span class="status-badge">'
        f'<span class="status-dot {cls}"></span>'
        f"{label}：{text}"
        f"</span>"
    )


# =====================================================================
# 产物扫描（档位 5）
# =====================================================================
IMG_EXTS = {".jpg", ".jpeg", ".png"}


def _is_visible_file(p: Path) -> bool:
    name = p.name
    if name.startswith(".") or name.startswith("~$"):
        return False
    return True


def _scan_latest_artifacts() -> Tuple[Optional[str], Optional[str], List[str]]:
    roots = [r for r in (SCRAP_ROOT, SHENGLONG_ROOT, YONGFENG_ROOT, BXSTEEL_ROOT) if r.exists()]
    if not roots:
        return None, None, []

    xlsxs: List[Path] = []
    pptxs: List[Path] = []
    imgs: List[Path] = []
    for root in roots:
        xlsxs.extend(p for p in root.rglob("*.xlsx") if _is_visible_file(p))
        pptxs.extend(p for p in root.rglob("*.pptx") if _is_visible_file(p))
        for ext in IMG_EXTS:
            imgs.extend(p for p in root.rglob(f"*{ext}") if _is_visible_file(p))

    xlsxs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_xlsx = str(xlsxs[0]) if xlsxs else None

    pptxs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    latest_pptx = str(pptxs[0]) if pptxs else None

    imgs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    img_paths = [str(p) for p in imgs[:20]]

    return latest_xlsx, latest_pptx, img_paths


# =====================================================================
# 聊天主回调
# =====================================================================
def _normalize_chat_history(history: list):
    normalized = []
    for item in history or []:
        if isinstance(item, dict) and "role" in item and "content" in item:
            normalized.append({"role": item["role"], "content": item.get("content") or ""})
        elif isinstance(item, (tuple, list)) and len(item) >= 2:
            user_text = (item[0] or "").strip()
            assistant_text = (item[1] or "").strip()
            if user_text:
                normalized.append({"role": "user", "content": user_text})
            if assistant_text:
                normalized.append({"role": "assistant", "content": assistant_text})
    return normalized


def append_message(message: str, history: list):
    history = _normalize_chat_history(history)
    message = (message or "").strip()
    if not message:
        return "", history, ""
    return "", history + [{"role": "user", "content": message}], message


def clear_chat():
    xlsx, pptx, imgs = _scan_latest_artifacts()
    return "", [], {"vpn_state": "unknown", "messages": []}, xlsx, pptx, imgs


# =====================================================================
# 快捷 prompt（档位 4）—— 点按钮填入输入框，由用户二次回车发送
# =====================================================================
def _quick_prompts() -> Dict[str, Dict[str, Dict[str, str]]]:
    yesterday = (date.today() - timedelta(days=1)).strftime("%Y-%m-%d")
    week_start = (date.today() - timedelta(days=7)).strftime("%Y-%m-%d")
    today_s = date.today().strftime("%Y-%m-%d")
    return {
        "永锋钢铁": {
            "打包带钢卷": {
                "昨日打包带情况": "发昨天的打包带情况",
                "下载昨日打包带异常图": "下载昨天打包带的异常图片",
            },
            "烧结矿颗粒度": {
                "昨日准确率报表": f"生成 {yesterday} 的烧结矿颗粒度人工筛分 vs 视觉准确率报表",
                "指定区间准确率报表": "生成 2026-04-01 到 2026-04-07 的烧结矿颗粒度人工筛分 vs 视觉准确率报表",
                "支持哪些指令": (
                    "请列出你支持的所有功能和典型用法示例。"
                    "分【打包带钢卷 @ 永锋】【烧结矿颗粒度 @ 永锋】【废钢检判 @ 镔鑫】【废钢检判 @ 盛隆】四节回答。"
                ),
            },
        },
        "镔鑫钢铁": {
            "废钢检判": {
                "昨日废钢检判情况": f"发 {yesterday} 的【镔鑫】废钢检判情况",
                "近 7 天报表 + 错判图": (
                    f"导出 {week_start} 到 {today_s} 的【镔鑫】废钢检判报表并下载错判图"
                ),
                "近 7 天 PPT 汇报页": (
                    f"按 {week_start} 到 {today_s} 的【镔鑫】检判结果生成对应的 PPT 汇报页"
                ),
                "近 7 天各料型统计+汇报": (
                    f"统计 {week_start} 到 {today_s} 的【镔鑫】各料型周度数据并导出报表"
                ),
                "支持哪些指令": (
                    "请列出你支持的所有功能和典型用法示例。"
                    "分【打包带钢卷 @ 永锋】【废钢检判 @ 镔鑫】【废钢检判 @ 盛隆】三节回答。"
                ),
            },
            "球机图像下载+重命名": {
                "下载昨日球机图像": f"下载 {yesterday} 的镔鑫球机图像",
                "下载球机图像": f"下载 {week_start} 到 {today_s} 的镔鑫球机图像",
            },
        },
        "盛隆钢铁": {
            "MINIO图像下载": {
                "MINIO图像下载": "MINIO图像下载 2026-05-01 到 2026-05-07",
            },
            "3000网站图像下载": {
                "3000网站图像下载": "3000网站图像下载 2026-05-01 到 2026-05-07",
            },
            "废钢检判": {
                "昨日废钢检判情况": f"发 {yesterday} 的【盛隆】废钢检判情况",
                "近 7 天报表": f"导出 {week_start} 到 {today_s} 的【盛隆】废钢检判报表",
                "主表（多周期累积）": (
                    "生成【盛隆】主表，把这两个周期累积到一个 xlsx："
                    "2026-04-14 至 2026-04-22、2026-04-23 至 2026-04-29"
                ),
                "重废归一化主表": (
                    "生成【盛隆】重废1/2/3归一化准确率主表，把这几个周期累积到一个 xlsx："
                    "准确率统计时排除人工检判结果中没有任意重废1/2/3料型的车次；"
                    "2026-04-14 至 2026-04-22、2026-04-23 至 2026-04-29、"
                    "2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13；"
                    "其中 2026-04-30 至 2026-05-06、2026-05-07 至 2026-05-13 "
                    "当作一个统计周期进行统计"
                ),
            }
        },
    }


# =====================================================================
# 主题 + CSS（档位 1、2）
# =====================================================================
THEME = gr.themes.Soft(
    primary_hue="blue",
    secondary_hue="orange",
    neutral_hue="slate",
    font=[gr.themes.GoogleFont("Inter"), "system-ui", "sans-serif"],
)

CUSTOM_CSS = """
/* 顶部品牌栏 */
.brand-wrap { padding: 4px 0 16px; }
.brand-title {
    font-size: 28px;
    font-weight: 800;
    background: linear-gradient(135deg, #1e3a8a 0%, #3b82f6 60%, #f59e0b 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    letter-spacing: 1.5px;
    line-height: 1.2;
}
.brand-org {
    display: inline-block;
    margin: 4px 0 2px;
    padding: 2px 10px;
    border-radius: 4px;
    background: rgba(59, 130, 246, 0.08);
    border-left: 3px solid #3b82f6;
    color: #1e3a8a;
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.3px;
}
.brand-subtitle {
    color: #64748b;
    font-size: 14px;
    margin-top: 4px;
}
.proj-chip {
    display: inline-block;
    margin: 4px 6px 0 0;
    padding: 3px 10px;
    border-radius: 999px;
    font-size: 12.5px;
    font-weight: 600;
    letter-spacing: 0.2px;
}
.proj-chip.pt-chip {
    background: #eff6ff;
    color: #1d4ed8;
    border: 1px solid #bfdbfe;
}
.proj-chip.sc-chip {
    background: #fff7ed;
    color: #c2410c;
    border: 1px solid #fed7aa;
}
.proj-chip.sl-chip {
    background: #f0fdf4;
    color: #15803d;
    border: 1px solid #bbf7d0;
}

/* 状态栏 */
.status-bar {
    display: flex;
    align-items: center;
    justify-content: flex-end;
    gap: 8px;
    flex-wrap: wrap;
    padding-top: 8px;
}
.today-chip {
    padding: 4px 10px;
    border-radius: 999px;
    background: #eef2ff;
    color: #3730a3;
    font-size: 13px;
    font-weight: 500;
}
.status-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    background: #f1f5f9;
    color: #334155;
    font-size: 13px;
}
.status-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}
.status-dot.ok { background: #22c55e; box-shadow: 0 0 6px #22c55e; }
.status-dot.fail { background: #ef4444; box-shadow: 0 0 6px #ef4444; }

/* 侧边栏标题 */
.side-title {
    font-size: 13px;
    font-weight: 600;
    color: #475569;
    letter-spacing: 0.6px;
    margin: 10px 0 6px;
    text-transform: uppercase;
}

/* 侧边栏项目归属卡片 */
.proj-card {
    background: #f8fafc;
    border: 1px solid #e2e8f0;
    border-radius: 8px;
    padding: 8px 10px;
    margin-bottom: 8px;
}
.proj-row {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 4px 0;
    font-size: 13px;
}
.proj-row + .proj-row {
    border-top: 1px dashed #e2e8f0;
}
.proj-tag {
    padding: 2px 8px;
    border-radius: 4px;
    font-weight: 600;
    font-size: 12px;
}
.proj-tag.pt {
    background: #eff6ff;
    color: #1d4ed8;
}
.proj-tag.sc {
    background: #fff7ed;
    color: #c2410c;
}
.proj-tag.sl {
    background: #f0fdf4;
    color: #15803d;
}
.proj-arrow {
    color: #94a3b8;
    font-weight: 600;
}
.proj-site {
    color: #0f172a;
    font-weight: 500;
}

/* 让聊天气泡更紧凑 */
.chatbot .message-wrap { padding: 8px 12px; }
.chatbot .message.user { justify-content: flex-end; }
.chatbot .message.user .bubble {
    background: linear-gradient(135deg, #2563eb 0%, #1d4ed8 100%);
    color: #fff;
}
.chatbot .message.assistant .bubble {
    background: #eff6ff;
    color: #0f172a;
}
"""


# =====================================================================
# UI 组装
# =====================================================================
def build_ui() -> gr.Blocks:
    q = _quick_prompts()

    def _update_businesses(project):
        first_business = next(iter(q[project]))
        first_command = next(iter(q[project][first_business]))
        return (
            gr.update(choices=list(q[project].keys()), value=first_business),
            gr.update(choices=list(q[project][first_business].keys()), value=first_command),
            q[project][first_business][first_command],
        )

    def _update_commands(project, business):
        first_command = next(iter(q[project][business]))
        return (
            gr.update(choices=list(q[project][business].keys()), value=first_command),
            q[project][business][first_command],
        )

    def _select_command(project, business, command):
        return q[project][business][command]

    with gr.Blocks(
        title="BriefMe · 多场景数据统计助手",
        fill_height=True,
    ) as demo:

        # ------- 档位 2 · 顶部品牌栏 -------
        with gr.Row(elem_classes="brand-wrap"):
            with gr.Column(scale=6):
                gr.HTML(
                    '<div class="brand-title">BriefMe · 多场景数据统计助手</div>'
                    '<div class="brand-org">中冶赛迪（重庆）信息技术有限公司</div>'
                    '<div class="brand-subtitle">'
                    "一个入口，多钢厂多场景 — 当前已接入："
                    '<span class="proj-chip pt-chip">打包带钢卷 @ 永锋钢铁</span>'
                    '<span class="proj-chip sc-chip">废钢检判 @ 镔鑫钢铁</span>'
                    '<span class="proj-chip sc-chip">球机图像 @ 镔鑫钢铁</span>'
                    '<span class="proj-chip sl-chip">废钢检判 @ 盛隆钢铁</span>'
                    "</div>"
                )
            with gr.Column(scale=4):
                status_display = gr.HTML(_status_html())
                refresh_btn = gr.Button("🔄 刷新 VPN 状态", size="sm")

        # ------- 档位 3 · 左右分栏 -------
        with gr.Row():
            # --- 左侧：业务下拉 + 状态说明 ---
            with gr.Column(scale=1, min_width=260):
                gr.HTML('<div class="side-title">快捷指令</div>')

                project_select = gr.Dropdown(
                    choices=list(q.keys()),
                    value="永锋钢铁",
                    label="项目",
                )
                business_select = gr.Dropdown(
                    choices=list(q["永锋钢铁"].keys()),
                    value="打包带钢卷",
                    label="业务",
                )
                command_select = gr.Dropdown(
                    choices=list(q["永锋钢铁"]["打包带钢卷"].keys()),
                    value="昨日打包带情况",
                    label="指令",
                )
                command_preview = gr.Textbox(
                    label="待发送内容",
                    value=q["永锋钢铁"]["打包带钢卷"]["昨日打包带情况"],
                    interactive=False,
                    lines=4,
                )
                fill_btn = gr.Button("填入输入框", variant="primary", size="sm")
                
                def _update_businesses(project):
                    first_business = next(iter(q[project]))
                    first_command = next(iter(q[project][first_business]))
                    return (
                        gr.update(choices=list(q[project].keys()), value=first_business),
                        gr.update(choices=list(q[project][first_business].keys()), value=first_command),
                        q[project][first_business][first_command],
                    )

                def _update_commands(project, business):
                    first_command = next(iter(q[project][business]))
                    return (
                        gr.update(choices=list(q[project][business].keys()), value=first_command),
                        q[project][business][first_command],
                    )

                def _select_command(project, business, command):
                    return q[project][business][command]

                project_select.change(
                    _update_businesses,
                    inputs=project_select,
                    outputs=[business_select, command_select, command_preview],
                    queue=False,
                )
                business_select.change(
                    _update_commands,
                    inputs=[project_select, business_select],
                    outputs=[command_select, command_preview],
                    queue=False,
                )
                command_select.change(
                    _select_command,
                    inputs=[project_select, business_select, command_select],
                    outputs=command_preview,
                    queue=False,
                )
                
                gr.HTML(
                    '<div class="side-title">业务归属</div>'
                    '<div class="proj-card">'
                    '  <div class="proj-row">'
                    '    <span class="proj-tag pt">打包带钢卷</span>'
                    '    <span class="proj-arrow">→</span>'
                    '    <span class="proj-site">永锋钢铁</span>'
                    '  </div>'
                    '  <div class="proj-row">'
                    '    <span class="proj-tag pt">烧结矿颗粒度</span>'
                    '    <span class="proj-arrow">→</span>'
                    '    <span class="proj-site">永锋钢铁</span>'
                    '  </div>'
                    '  <div class="proj-row">'
                    '    <span class="proj-tag sc">废钢检判</span>'
                    '    <span class="proj-arrow">→</span>'
                    '    <span class="proj-site">镔鑫钢铁</span>'
                    '  </div>'
                    '  <div class="proj-row">'
                    '    <span class="proj-tag sc">球机图像下载+重命名</span>'
                    '    <span class="proj-arrow">→</span>'
                    '    <span class="proj-site">镔鑫钢铁</span>'
                    '  </div>'
                    '  <div class="proj-row">'
                    '    <span class="proj-tag sl">废钢检判</span>'
                    '    <span class="proj-arrow">→</span>'
                    '    <span class="proj-site">盛隆钢铁</span>'
                    '  </div>'
                    '</div>'
                )

                gr.HTML('<div class="side-title">使用提示</div>')
                gr.Markdown(
                    "- 问**打包带**：用「钢卷 / 打包带 / 打数 / 应打数 / 永锋」等词\n"
                    "- 问**镔鑫废钢**：带【镔鑫】字样\n"
                    "- 问**盛隆废钢**：带【盛隆】字样\n"
                    "- 只说「废钢」不指明时，会反问你是镔鑫还是盛隆\n"
                    "- **盛隆主表**支持任意周期累积，按钮里改/补日期即可\n"
                    "- **重废归一化主表**会排除人工无任意重废1/2/3的车次\n"
                    "- **镔鑫球机图像**：在指令中写明日期、工号、密码即可\n"
                    "- 按钮只是填好文字，**回车**发送"
                )

            # --- 右侧：聊天主区 ---
            with gr.Column(scale=4):
                chatbot = gr.Chatbot(
                    height=520,
                    show_label=False,
                    render_markdown=True,
                    elem_classes="chatbot",
                    avatar_images=(None, None),
                    placeholder=(
                        "<center><br/>"
                        "👋 你好，我是 BriefMe，中冶赛迪的多场景数据统计助手<br/>"
                        "左侧点一下「快捷指令」就能开始，"
                        "或直接在下方输入问题<br/>"
                        "</center>"
                    ),
                )

                with gr.Row():
                    msg = gr.Textbox(
                        placeholder="输入消息，如：发 2026-04-15 的废钢检判情况 ...",
                        show_label=False,
                        scale=9,
                        container=False,
                        autofocus=True,
                        lines=3,
                    )
                    submit_btn = gr.Button("发送", scale=1, variant="primary")

                with gr.Row():
                    clear_btn = gr.Button("🗑 清空对话", size="sm")

                def _fill_message(text):
                    return text

                # ------- 档位 5 · 产物下载面板 -------
                with gr.Accordion(
                    "📁 最近生成的报表 / PPT / 错判图片",
                    open=False,
                ):
                    xlsx_init, pptx_init, imgs_init = _scan_latest_artifacts()
                    report_file = gr.File(
                        label="最新 Excel 报表（点击下载）",
                        value=xlsx_init,
                        interactive=False,
                    )
                    pptx_file = gr.File(
                        label="最新 PPT 汇报页（点击下载，仅镔鑫场景产出）",
                        value=pptx_init,
                        interactive=False,
                    )
                    gallery = gr.Gallery(
                        label="最近 20 张错判渲染图（点击放大）",
                        value=imgs_init,
                        columns=4,
                        height=260,
                        show_label=True,
                        allow_preview=True,
                    )
                # ------- 下载图片包 -------
                with gr.Accordion("📦 下载图片包", open=False):
                    download_zip_file = gr.File(label="点击下载 ZIP 文件到本地", interactive=False)


                # ------- 下载实时日志面板 -------
                with gr.Accordion("📋 下载实时日志", open=False):
                    log_file_display = gr.Textbox(
                        label="",
                        value="等待下载...",
                        interactive=False,
                        lines=15,
                        max_lines=20,
                        autoscroll=True,
                        show_label=False,
                        elem_id="log_textbox"
                    )
                    refresh_btn = gr.Button("🔄 手动刷新日志", size="sm")
                    
                    def refresh_log():
                        try:
                            log_file = Path("download_log.txt")
                            if log_file.exists():
                                content = log_file.read_text(encoding="utf-8")
                                if content.strip():
                                    return content
                            return "等待下载..."
                        except Exception as e:
                            return f"读取失败: {e}"
                    
                    refresh_btn.click(
                        refresh_log, 
                        outputs=log_file_display,
                        js="() => { setTimeout(() => { const el = document.querySelector('#log_textbox textarea'); if(el) { el.scrollTop = el.scrollHeight; } }, 200); setTimeout(() => { const el = document.querySelector('#log_textbox textarea'); if(el) { el.scrollTop = el.scrollHeight; } }, 500); }"
                    )
        session = gr.State({"vpn_state": "unknown", "messages": []})

# --- 绑定：输入/按钮 ---
        def _sync_append(message, history):
            history = _normalize_chat_history(history)
            message = (message or "").strip()
            if not message:
                return "", history
            return "", history + [{"role": "user", "content": message}]

        # ========== 下载功能处理器（流式）==========
        def download_minio_handler(message, history, session):
            history = _normalize_chat_history(history)

            import re
            from datetime import datetime, timedelta
            from minio import Minio
            from agent.shenglong.minio_downloader import MINIO_HOST, MINIO_API_PORT, BUCKET, PREFIX_BASE, ACCESS_KEY, SECRET_KEY, download_and_pack

            match = re.search(r'(\d{4}-\d{2}-\d{2})\s*到\s*(\d{4}-\d{2}-\d{2})', message)
            if not match:
                history.append({"role": "assistant", "content": "请使用格式：MINIO图像下载 YYYY-MM-DD 到 YYYY-MM-DD"})
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, None
                return

            start_date = match.group(1)
            end_date = match.group(2)

            client = Minio(
                f"{MINIO_HOST}:{MINIO_API_PORT}",
                access_key=ACCESS_KEY,
                secret_key=SECRET_KEY,
                secure=False,
            )
            start = datetime.strptime(start_date, "%Y-%m-%d").date()
            end = datetime.strptime(end_date, "%Y-%m-%d").date()
            total_files = 0
            current = start
            while current <= end:
                prefix = f"{PREFIX_BASE}/{current.isoformat()}/"
                try:
                    objects = list(client.list_objects(BUCKET, prefix=prefix, recursive=True))
                    files = [o for o in objects if o.size and o.size > 0]
                    total_files += len(files)
                except Exception:
                    pass
                current += timedelta(days=1)

            history.append({"role": "assistant", "content": f"📥 正在下载 {start_date} 到 {end_date} 的 MINIO 图像，共 {total_files} 个文件..."})
            xlsx, pptx, imgs = _scan_latest_artifacts()
            yield history, session, xlsx, pptx, imgs, None

            zip_path, success, failed = download_and_pack(start_date, end_date)
            if zip_path:
                history.append({"role": "assistant", "content": f"\n\n✅ 下载完成！成功 {success} 个文件，失败 {failed} 个\n\n点击下方「📦 下载图片包」按钮下载 ZIP 文件到本地。"})
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, zip_path
            else:
                history.append({"role": "assistant", "content": "❌ 下载失败"})
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, None

        def download_3000_handler(message, history, session):
            history = _normalize_chat_history(history)

            import re
            from agent.shenglong.downloader import download_images_by_date_range

            match = re.search(r'(\d{4}-\d{2}-\d{2})\s*到\s*(\d{4}-\d{2}-\d{2})', message)
            if not match:
                history.append({"role": "assistant", "content": "请使用格式：3000网站图像下载 YYYY-MM-DD 到 YYYY-MM-DD"})
                xlsx, pptx, imgs = _scan_latest_artifacts()
                yield history, session, xlsx, pptx, imgs, None
                return

            start_date = match.group(1)
            end_date = match.group(2)
            history.append({"role": "assistant", "content": f"📥 正在下载 {start_date} 到 {end_date} 的 3000 网站图像（无需筛选），请稍候..."})
            xlsx, pptx, imgs = _scan_latest_artifacts()
            yield history, session, xlsx, pptx, imgs, None

            result = download_images_by_date_range(
                start_date,
                end_date,
                output_dir=SHENGLONG_ROOT,
                include_missing_manual=True,
            )

            success = result.get("success", 0)
            failed = result.get("failed", 0)
            history.append({"role": "assistant", "content": f"\n\n✅ 下载完成！成功 {success} 个文件，失败 {failed} 个\n\n输出目录：{result.get('output_dir', '')}"})
            xlsx, pptx, imgs = _scan_latest_artifacts()
            yield history, session, xlsx, pptx, imgs, None

        # ========== 其他功能处理器（普通）==========
        def normal_handler(user_message, history, session):
            history = _normalize_chat_history(history)
            
            if not user_message:
                xlsx, pptx, imgs = _scan_latest_artifacts()
                return history, session, xlsx, pptx, imgs, None

            # 添加用户消息
            # history.append({"role": "user", "content": user_message})
            
            # 调用 Agent
            reply, session = agent.chat(user_message, session)
            
            history.append({"role": "assistant", "content": str(reply)})
            xlsx, pptx, imgs = _scan_latest_artifacts()
            return history, session, xlsx, pptx, imgs, None

        # ========== 路由处理器 ==========
        def route_handler(_pending_message, history, session):
            # 获取用户消息
            user_message = ""
            if history and len(history) > 0:
                last_item = history[-1]
                if isinstance(last_item, dict):
                    content = last_item.get("content", "")
                    if isinstance(content, list):
                        text_parts = [str(item.get("text", "")) for item in content if isinstance(item, dict) and "text" in item]
                        user_message = "".join(text_parts).strip()
                    else:
                        user_message = str(content).strip()
            
            # 判断是否是下载指令
            if "MINIO图像下载" in user_message:
                # 下载功能：走 minio_downloader 逻辑
                for result in download_minio_handler(user_message, history, session):
                    yield result
            elif "3000网站图像下载" in user_message:
                # 下载功能：走 downloader.py 逻辑
                for result in download_3000_handler(user_message, history, session):
                    yield result
            else:
                # 其他功能：普通模式
                result = normal_handler(user_message, history, session)
                yield result

        # 绑定
        # msg.submit(
        #     _sync_append, 
        #     inputs=[msg, chatbot], 
        #     outputs=[msg, chatbot], 
        #     queue=False
        # ).then(
        #     route_handler,
        #     inputs=[msg, chatbot, session],
        #     outputs=[chatbot, session, report_file, pptx_file, gallery, download_zip_file],
        # )
        
        submit_btn.click(
            _sync_append,
            inputs=[msg, chatbot],
            outputs=[msg, chatbot], 
            queue=False,
        ).then(
            route_handler,
            inputs=[msg, chatbot, session],
            outputs=[chatbot, session, report_file, pptx_file, gallery, download_zip_file],
        )

        clear_btn.click(
            clear_chat,
            outputs=[msg, chatbot, session, report_file, pptx_file, gallery],
        )

        refresh_btn.click(_status_html, outputs=status_display)

        def _update_businesses(project):
            first_business = next(iter(q[project]))
            first_command = next(iter(q[project][first_business]))
            return (
                gr.update(choices=list(q[project].keys()), value=first_business),
                gr.update(choices=list(q[project][first_business].keys()), value=first_command),
                q[project][first_business][first_command],
            )

        def _update_commands(project, business):
            first_command = next(iter(q[project][business]))
            return (
                gr.update(choices=list(q[project][business].keys()), value=first_command),
                q[project][business][first_command],
            )

        def _select_command(project, business, command):
            return q[project][business][command]

        project_select.change(
            _update_businesses,
            inputs=project_select,
            outputs=[business_select, command_select, command_preview],
            queue=False,
        )
        business_select.change(
            _update_commands,
            inputs=[project_select, business_select],
            outputs=[command_select, command_preview],
            queue=False,
        )
        command_select.change(
            _select_command,
            inputs=[project_select, business_select, command_select],
            outputs=command_preview,
            queue=False,
        )
        fill_btn.click(_fill_message, inputs=command_preview, outputs=msg, queue=False)

    return demo


if __name__ == "__main__":
    demo = build_ui()
    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=THEME,
        css=CUSTOM_CSS,
    )