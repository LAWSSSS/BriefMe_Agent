"""页面操作模块：表格行识别、点击行加载详情、图片提取、日期筛选、翻页。"""
from __future__ import annotations

import re
import time
import logging
from typing import Dict, List, Tuple

from playwright.sync_api import Page

logger = logging.getLogger(__name__)

ALLOWED_GRADES = {"精炉料-等级二", "精炉料-等级三", "重废-等级一", "重废-等级二"}

IMAGE_CATEGORIES = {
    "visualize_superdetect_": "危险物图",
    "visualize_special_":     "夹杂物图",
    "visualize_top_":         "判级识别图",
    "visualize_":             "判级识别图",
    "":                       "原图",
}


def _classify_image(url: str) -> str | None:
    if '/thumbnail/' in url or '_s.' in url.lower():
        return None
    fname = url.rsplit('/', 1)[-1] if '/' in url else url
    for prefix, category in IMAGE_CATEGORIES.items():
        if prefix == '':
            return category
        if fname.startswith(prefix):
            return category
    return "原图"


def get_table_rows(page: Page) -> list:
    for sel in [
        '.wui-table-body .wui-table-row',
        '.wui-table-body tr',
        '.wui-table-tbody tr',
        '[class*="table-body"] tr',
        '[class*="table"] tbody tr:not([class*="header"])',
        'table tbody tr',
    ]:
        rows = page.query_selector_all(sel)
        if rows:
            data_rows = []
            for r in rows:
                cls = r.get_attribute('class') or ''
                if 'thead' in cls or 'header' in cls:
                    continue
                text = r.inner_text().strip()
                if text:
                    data_rows.append(r)
            if data_rows:
                return data_rows
    return []


def get_grading_col_index(page: Page) -> int:
    ths = page.query_selector_all('.wui-table-thead-th')
    for idx, th in enumerate(ths):
        key = th.get_attribute('data-col-key') or ''
        if key == 'afGradingDef':
            logger.debug("人工级别列: 第 %d 列", idx)
            return idx
    logger.warning("未找到 data-col-key='afGradingDef' 列")
    return -1


def parse_primary_grade(grade_text: str) -> str:
    if not grade_text:
        return ""
    first = grade_text.split(",")[0].strip()
    first = re.sub(r'-\d+(?:\.\d+)?%$', '', first)
    return first


def get_row_grade(row, col_index: int) -> str:
    if col_index >= 0:
        cells = row.query_selector_all('td')
        if col_index < len(cells):
            title = cells[col_index].get_attribute('title') or cells[col_index].inner_text()
            grade = parse_primary_grade(title)
            if grade:
                return grade

    for cell in row.query_selector_all('td'):
        title = cell.get_attribute('title') or cell.inner_text()
        if title and re.search(r'等级[一二三四五六\d]', title):
            grade = parse_primary_grade(title)
            if grade:
                return grade
    return ""


def is_allowed_grade(grade: str) -> bool:
    return grade in ALLOWED_GRADES


def wait_spinner_gone(page: Page, timeout: float = 15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        spinner = page.query_selector('.wui-spin-full-screen')
        if not spinner:
            return
        time.sleep(0.5)
    logger.warning("加载遮罩超时 %ds", timeout)


def click_row_and_extract(
    page: Page,
    row,
    row_index: int,
    total: int,
    delay: float = 2.0,
) -> Tuple[str, Dict[str, List[str]]]:
    plate = f"row_{row_index}"
    plate_cell = None

    cells = row.query_selector_all('td')
    for cell in cells:
        title = (cell.get_attribute('title') or '').strip()
        if title and not title.isdigit() and len(title) >= 2:
            plate = title
            plate_cell = cell
            break

    if plate_cell is None:
        for cell in cells:
            title = (cell.get_attribute('title') or '').strip()
            if title:
                plate = title
                plate_cell = cell
                break

    if plate_cell is None:
        if cells:
            plate_cell = cells[0]
            plate = cells[0].inner_text().strip() or plate
        else:
            plate_cell = row

    logger.info("=" * 50)
    logger.info("[%d] %s", total + 1, plate.strip())
    logger.info("=" * 50)

    try:
        wait_spinner_gone(page, timeout=15)

        try:
            plate_cell.click(timeout=5000)
        except Exception:
            logger.debug("正常点击被拦截，使用 force click")
            plate_cell.click(force=True, timeout=5000)

        time.sleep(delay)
        _wait_for_detail_section(page)

        logger.debug("触发图片懒加载...")
        _scroll_detail_section(page)

        classified = _extract_classified_images(page)
        total_imgs = sum(len(v) for v in classified.values())
        for cat, urls in classified.items():
            logger.info("  %s: %d 张", cat, len(urls))
        logger.info("  合计: %d 张图片", total_imgs)
        return plate.strip(), classified
    except Exception as e:
        logger.error("点击行失败: %s", e)
        return plate.strip(), {}


def _wait_for_detail_section(page: Page):
    try:
        page.wait_for_selector('img[src*="/uploadFile/"]', timeout=15000)
        time.sleep(3)
    except Exception:
        time.sleep(2)


def _scroll_detail_section(page: Page):
    container = None
    for sel in [
        '.wui-table-body', '[class*="detail"]', '[class*="result"]',
        '[class*="image"]', '[class*="preview"]', '[class*="scroll"]',
    ]:
        container = page.query_selector(sel)
        if container:
            break

    if not container:
        page.evaluate('window.scrollTo(0, document.body.scrollHeight)')
        time.sleep(1)
        return

    prev_height = 0
    for i in range(8):
        container.evaluate('el => el.scrollTop = el.scrollHeight')
        time.sleep(1)
        new_height = container.evaluate('el => el.scrollHeight')
        if new_height == prev_height and i > 0:
            break
        prev_height = new_height
        page.evaluate('window.scrollBy(0, 300)')
        time.sleep(0.5)


def _extract_classified_images(page: Page) -> Dict[str, List[str]]:
    classified: Dict[str, List[str]] = {}
    seen: set[str] = set()

    for img in page.query_selector_all('img[data-original]'):
        url = img.get_attribute('data-original') or ''
        if url and '/uploadFile/' in url and url not in seen:
            cat = _classify_image(url)
            if cat:
                seen.add(url)
                classified.setdefault(cat, []).append(url)

    for img in page.query_selector_all('img[src]'):
        url = img.get_attribute('src') or ''
        if url and '/uploadFile/' in url and url not in seen:
            cat = _classify_image(url)
            if cat:
                seen.add(url)
                classified.setdefault(cat, []).append(url)

    for attr in ['data-src', 'data-lazy-src']:
        for img in page.query_selector_all(f'img[{attr}]'):
            url = img.get_attribute(attr) or ''
            if url and '/uploadFile/' in url and url not in seen:
                cat = _classify_image(url)
                if cat:
                    seen.add(url)
                    classified.setdefault(cat, []).append(url)

    return classified


def extract_pie_charts(page: Page) -> List[Tuple[str, str]]:
    charts: List[Tuple[str, str]] = []

    result = page.evaluate('''() => {
        const charts = [];
        const pieDivs = document.querySelectorAll('[id^="PieChart"]');
        pieDivs.forEach((div, idx) => {
            const canvas = div.querySelector('canvas');
            if (!canvas) return;

            let label = '';
            const picture = div.closest('.picture');
            if (picture) {
                const pointName = picture.querySelector('.chart-point-name');
                if (pointName) label = pointName.innerText.trim();
            }
            if (!label) label = 'pie_' + idx;

            const dataUrl = canvas.toDataURL('image/png');
            charts.push({
                label: label.replace(/[\\\\/:*?"<>|]/g, '_'),
                dataUrl: dataUrl,
                idx: idx
            });
        });
        return charts;
    }''')

    label_count: dict[str, int] = {}
    for c in result:
        lbl = c['label']
        if lbl not in label_count:
            label_count[lbl] = 0
        else:
            label_count[lbl] += 1
        if label_count[lbl] > 0:
            lbl = f"{lbl}_{label_count[lbl]}"
        charts.append((lbl, c['dataUrl']))

    logger.info("提取到 %d 个饼图", len(charts))
    return charts


def click_history_data(page: Page):
    logger.info("进入历史数据...")
    link = page.query_selector('a[href="#/record"]:has-text("历史数据")')
    if not link:
        link = page.query_selector('span:has-text("历史数据")')
    if link:
        link.click()
        time.sleep(4)
        logger.info("已点击'历史数据'")


def apply_date_filter(page: Page, from_date: str = "", to_date: str = ""):
    if not from_date and not to_date:
        return

    logger.info("应用时间筛选: %s ~ %s", from_date or "(不限)", to_date or "(不限)")

    clicked = False
    for sel in ['.icon-wrap .uf-arrow-down', '.icon-arrow', 'i.uf-arrow-down', '[class*="icon-wrap"] i']:
        arrow = page.query_selector(sel)
        if arrow:
            try:
                arrow.click()
                time.sleep(0.8)
                clicked = True
                break
            except Exception:
                continue
    if not clicked:
        logger.warning("未找到下拉箭头，时间筛选可能未生效")

    if from_date:
        _fill_date_input(page, '#fromDate', from_date)
    if to_date:
        _fill_date_input(page, '#toDate', to_date)

    if from_date or to_date:
        time.sleep(0.5)
        triggered = False
        for sel in [
            'button:has(.uf-search-light-2)',
            '.uf-search-light-2',
            'button:has-text("查询")',
            'button:has-text("搜索")',
        ]:
            btn = page.query_selector(sel)
            if btn:
                try:
                    btn.click()
                    triggered = True
                    logger.debug("已点击查询按钮: %s", sel)
                    break
                except Exception:
                    continue
        if not triggered:
            page.keyboard.press("Enter")
            logger.debug("已按 Enter 触发查询")
        time.sleep(3)


def _fill_date_input(page: Page, selector: str, value: str):
    inp = page.query_selector(selector)
    if inp:
        inp.click()
        time.sleep(0.3)
        inp.fill('')
        inp.type(value, delay=50)
        logger.debug("已填写 %s: %s", selector, value)


def change_page_size(page: Page, page_size: int = 100):
    time.sleep(1)
    for sel in [
        '.wui-pagination .wui-select',
        '[class*="pagination"] [class*="select"]',
        '.wui-table-footer [class*="select"]',
    ]:
        trigger = page.query_selector(sel)
        if trigger:
            trigger.click()
            time.sleep(0.5)
            break

    for opt_sel in [
        f'.wui-select-dropdown [title="{page_size}"]',
        f'[role="option"][title="{page_size}"]',
        f'.wui-select-item[title="{page_size}"]',
    ]:
        option = page.query_selector(opt_sel)
        if option:
            option.click()
            time.sleep(2)
            logger.info("每页显示 %d 条", page_size)
            return
    logger.warning("未能修改每页显示条数")


def go_to_next_page(page: Page) -> bool:
    for sel in [
        '.wui-pagination li:last-child:not([class*="disabled"])',
        '.wui-pagination [class*="next"]:not([class*="disabled"])',
        '[class*="pagination"] li[title="下一页"]:not([class*="disabled"])',
        'li[title="下一页"]:not([aria-disabled="true"])',
        '[class*="pagination"] li:last-child',
    ]:
        btn = page.query_selector(sel)
        if btn:
            cls = btn.get_attribute('class') or ''
            aria = btn.get_attribute('aria-disabled') or ''
            if 'disabled' in cls or aria == 'true':
                continue
            btn.click()
            time.sleep(2)
            return True
    return False


def wait_for_table(page: Page, timeout: float = 20.0):
    logger.info("等待表格加载...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = get_table_rows(page)
        if rows:
            logger.info("表格已就绪 (%d 行)", len(rows))
            return rows
        time.sleep(2)

    logger.error("表格未加载！正在 dump 页面信息...")
    try:
        logger.error("  当前 URL: %s", page.url)
        logger.error("  页面标题: %s", page.title())
        for sel in ['.wui-table', '[class*="table"]', 'table', '.wui-table-body']:
            cnt = len(page.query_selector_all(sel))
            if cnt > 0:
                logger.error("  %s: %d 个", sel, cnt)
        logger.error("  页面文本前500字: %s", page.inner_text('body')[:500])
    except Exception:
        pass
    return []
