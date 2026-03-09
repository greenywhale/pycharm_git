from __future__ import annotations

import argparse
import re
import shutil
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

COL_SEQ = "序号"
COL_NODE_CODE = "节点编号"
COL_NODE_NAME = "节点名称"
COL_PARENT_CODE = "父节点编号"
COL_DESC = "描述"
COL_TYPE = "类型（0-数据源，1-目录，2-表）"
COL_UPDATE_TIME = "更新时间"

OUTPUT_COLUMNS = [
    COL_SEQ,
    COL_NODE_CODE,
    COL_NODE_NAME,
    COL_PARENT_CODE,
    COL_DESC,
    COL_TYPE,
    COL_UPDATE_TIME,
]


@dataclass
class OCRLine:
    text: str
    x: int
    y: int
    h: int
    conf: float
    page: int
    row_index: int


@dataclass
class NodeMeta:
    key: tuple[str, ...]
    parent_key: tuple[str, ...] | None
    name_cn: str
    name_raw: str
    table_en: str | None
    depth: int
    order: int


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def sanitize_for_code(name: str) -> str:
    text = normalize_text(name).upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    text = text.strip("_")
    return text


def extract_cn_en(text: str) -> tuple[str, str | None]:
    src = normalize_text(text)
    m = re.match(r"^(?P<cn>.*?)\s*[\[\【](?P<en>[A-Za-z0-9_]+)[\]\】]\s*$", src)
    if not m:
        return src, None
    cn = normalize_text(m.group("cn")) or src
    en = normalize_text(m.group("en")) or None
    return cn, en


def parse_region(region_text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in region_text.split(",")]
    if len(parts) != 4:
        raise ValueError("--region 格式必须是 x,y,w,h")
    x, y, w, h = [int(p) for p in parts]
    if w <= 0 or h <= 0:
        raise ValueError("--region 的 w 和 h 必须大于0")
    return x, y, w, h


def choose_region_interactive(pyautogui_module) -> tuple[int, int, int, int]:
    print("[提示] 将鼠标移动到树区域左上角后按回车...")
    input()
    x1, y1 = pyautogui_module.position()
    print(f"[信息] 左上角: ({x1}, {y1})")

    print("[提示] 将鼠标移动到树区域右下角后按回车...")
    input()
    x2, y2 = pyautogui_module.position()
    print(f"[信息] 右下角: ({x2}, {y2})")

    x = min(x1, x2)
    y = min(y1, y2)
    w = abs(x2 - x1)
    h = abs(y2 - y1)
    if w < 50 or h < 50:
        raise RuntimeError("选择区域过小，请重试。")
    print(f"[信息] 使用区域: x={x}, y={y}, w={w}, h={h}")
    return x, y, w, h


def load_runtime_modules(tesseract_cmd: str | None):
    try:
        import cv2
        import numpy as np
        import pyautogui
        import pytesseract
        from pytesseract import Output
    except ImportError as exc:
        raise RuntimeError(
            "?????????: pip install pyautogui pillow pytesseract opencv-python numpy pandas openpyxl"
        ) from exc

    candidates = []
    if tesseract_cmd:
        candidates.append(tesseract_cmd)
    else:
        # ??? PATH
        which_cmd = shutil.which("tesseract")
        if which_cmd:
            candidates.append(which_cmd)
        # ??? Windows ??????
        candidates.extend(
            [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]
        )

    resolved = None
    for cmd in candidates:
        if not cmd:
            continue
        if Path(cmd).exists():
            resolved = cmd
            break

    if not resolved:
        raise RuntimeError(
            "??? tesseract.exe????? Tesseract OCR?"
            "???????? --tesseract-cmd 'C:\\Program Files\\Tesseract-OCR\\tesseract.exe'"
        )

    pytesseract.pytesseract.tesseract_cmd = resolved
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        raise RuntimeError(
            f"??? tesseract.exe ?????: {resolved}?"
            "?????????????????"
        ) from exc

    print(f"[??] ?? Tesseract: {resolved} (version={version})")

    return cv2, np, pyautogui, pytesseract, Output

def preprocess_image(img, cv2, np):
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def ocr_lines_from_image(
    img,
    page: int,
    cv2,
    np,
    pytesseract,
    Output,
    ocr_lang: str,
    min_conf: float,
) -> list[OCRLine]:
    bw = preprocess_image(img, cv2, np)

    data = pytesseract.image_to_data(
        bw,
        lang=ocr_lang,
        config="--oem 3 --psm 6",
        output_type=Output.DICT,
    )

    groups: dict[tuple[int, int, int], list[tuple[int, int, int, float, str]]] = defaultdict(list)
    n = len(data.get("text", []))
    for i in range(n):
        txt = normalize_text(data["text"][i])
        if not txt:
            continue
        try:
            conf = float(data["conf"][i])
        except Exception:
            conf = -1
        if conf < min_conf:
            continue
        key = (int(data["block_num"][i]), int(data["par_num"][i]), int(data["line_num"][i]))
        x = int(data["left"][i])
        y = int(data["top"][i])
        h = int(data["height"][i])
        groups[key].append((x, y, h, conf, txt))

    result: list[OCRLine] = []
    row_index = 0
    for _, items in sorted(groups.items(), key=lambda kv: min(t[1] for t in kv[1])):
        items.sort(key=lambda t: t[0])
        text = "".join(t[4] for t in items)
        text = re.sub(r"\s+", "", text)
        if len(text) <= 1:
            continue
        x = min(t[0] for t in items)
        y = min(t[1] for t in items)
        h = max(t[2] for t in items)
        conf = sum(t[3] for t in items) / len(items)
        result.append(OCRLine(text=text, x=x, y=y, h=h, conf=conf, page=page, row_index=row_index))
        row_index += 1

    result.sort(key=lambda r: (r.y, r.x))
    return result


def try_expand_visible_nodes(
    region: tuple[int, int, int, int],
    lines: list[OCRLine],
    pyautogui_module,
    icon_offset: int,
    max_clicks_per_page: int,
    expand_wait: float,
):
    if not lines:
        return

    x0, y0, _, _ = region
    clicks = 0
    for line in lines:
        cn, en = extract_cn_en(line.text)
        if en:
            continue
        # 仅对疑似目录节点尝试展开
        if len(cn) <= 1:
            continue

        click_x = x0 + max(3, line.x - icon_offset)
        click_y = y0 + line.y + max(2, line.h // 2)
        pyautogui_module.click(click_x, click_y)
        pyautogui_module.press("right")
        clicks += 1
        time.sleep(expand_wait)
        if clicks >= max_clicks_per_page:
            break


def safe_move_and_scroll(pyautogui_module, center_x: int, center_y: int, scroll_pixels: int, wait_sec: float, safe_margin: int) -> bool:
    """???????????????? FAILSAFE ????????"""
    screen_w, screen_h = pyautogui_module.size()
    margin = max(1, int(safe_margin))

    x = min(max(int(center_x), margin), max(margin, screen_w - margin))
    y = min(max(int(center_y), margin), max(margin, screen_h - margin))

    try:
        pyautogui_module.moveTo(x, y, duration=0.12)
        pyautogui_module.scroll(-int(scroll_pixels))
        time.sleep(wait_sec)
        return True
    except Exception as exc:
        if exc.__class__.__name__ == "FailSafeException":
            print("[??] ?? PyAutoGUI FAILSAFE?????? FAILSAFE ??????")
            pyautogui_module.FAILSAFE = False
            try:
                pyautogui_module.moveTo(x, y, duration=0.12)
                pyautogui_module.scroll(-int(scroll_pixels))
                time.sleep(wait_sec)
                return True
            except Exception as exc2:
                print(f"[??] FAILSAFE ????????: {exc2}")
                return False

        print(f"[??] ????: {exc}")
        return False


def collect_ocr_lines(args) -> list[OCRLine]:
    cv2, np, pyautogui_module, pytesseract, Output = load_runtime_modules(args.tesseract_cmd)

    pyautogui_module.FAILSAFE = (not args.disable_failsafe)

    if args.region:
        region = parse_region(args.region)
        print(f"[信息] 使用命令行区域: {region}")
    else:
        region = choose_region_interactive(pyautogui_module)

    x, y, w, h = region
    center_x = x + w // 2
    center_y = y + h // 2

    all_lines: list[OCRLine] = []
    same_sig_count = 0
    last_signature: tuple[str, ...] | None = None

    debug_dir: Path | None = Path(args.debug_dir) if args.debug_dir else None
    if debug_dir:
        debug_dir.mkdir(parents=True, exist_ok=True)

    for page in range(args.max_pages):
        # 第一轮识别
        shot = pyautogui_module.screenshot(region=region)
        lines = ocr_lines_from_image(
            shot,
            page,
            cv2,
            np,
            pytesseract,
            Output,
            args.ocr_lang,
            args.min_conf,
        )

        if args.auto_expand:
            try_expand_visible_nodes(
                region=region,
                lines=lines,
                pyautogui_module=pyautogui_module,
                icon_offset=args.expand_icon_offset,
                max_clicks_per_page=args.expand_max_clicks_per_page,
                expand_wait=args.expand_click_wait,
            )
            time.sleep(args.expand_after_page_wait)

            shot = pyautogui_module.screenshot(region=region)
            lines = ocr_lines_from_image(
                shot,
                page,
                cv2,
                np,
                pytesseract,
                Output,
                args.ocr_lang,
                args.min_conf,
            )

        all_lines.extend(lines)

        if debug_dir:
            shot.save(debug_dir / f"page_{page:03d}.png")
            (debug_dir / f"page_{page:03d}.txt").write_text(
                "\n".join(f"{ln.y:04d}|x={ln.x:04d}|{ln.text}" for ln in lines),
                encoding="utf-8",
            )

        signature = tuple(ln.text for ln in lines[: args.signature_top_n])
        if signature == last_signature:
            same_sig_count += 1
        else:
            same_sig_count = 0
            last_signature = signature

        print(f"[进度] page={page+1}/{args.max_pages}，识别行数={len(lines)}")

        if same_sig_count >= args.stop_repeat_pages:
            print("[信息] 连续页面签名重复，停止滚动采集。")
            break

        if page < args.max_pages - 1:
            ok = safe_move_and_scroll(
                pyautogui_module=pyautogui_module,
                center_x=center_x,
                center_y=center_y,
                scroll_pixels=args.scroll_pixels,
                wait_sec=args.scroll_wait,
                safe_margin=args.mouse_safe_margin,
            )
            if not ok:
                print("[??] ???????????????????")
                break

    return all_lines


def build_nodes_from_lines(lines: list[OCRLine], args) -> list[NodeMeta]:
    if not lines:
        raise RuntimeError("OCR 未识别到任何行，请调大区域或降低 min-conf 重试。")

    lines_sorted = sorted(lines, key=lambda r: (r.page, r.row_index, r.y, r.x))
    min_x = min(r.x for r in lines_sorted)

    seen: dict[tuple[str, ...], NodeMeta] = {}
    stack: list[tuple[tuple[str, ...], int]] = []  # (path, depth)
    order = 0

    for ln in lines_sorted:
        depth = int(round((ln.x - min_x) / max(1, args.indent_step))) + 1
        if depth < 1:
            depth = 1
        if depth > len(stack) + 1:
            depth = len(stack) + 1

        while len(stack) >= depth:
            stack.pop()

        parent_key = stack[-1][0] if stack else None

        cn_name, en_name = extract_cn_en(ln.text)
        seg = f"{cn_name}[{en_name}]" if en_name else cn_name
        key = (seg,) if parent_key is None else (parent_key + (seg,))

        if key not in seen:
            seen[key] = NodeMeta(
                key=key,
                parent_key=parent_key,
                name_cn=cn_name,
                name_raw=ln.text,
                table_en=en_name,
                depth=depth,
                order=order,
            )
            order += 1

        stack.append((key, depth))

    nodes = sorted(seen.values(), key=lambda n: n.order)
    return nodes


def build_output_dataframe(nodes: list[NodeMeta], args) -> pd.DataFrame:
    children: dict[tuple[str, ...], list[tuple[str, ...]]] = defaultdict(list)
    node_map = {n.key: n for n in nodes}

    for n in nodes:
        if n.parent_key is not None:
            children[n.parent_key].append(n.key)

    node_type: dict[tuple[str, ...], int] = {}
    for n in nodes:
        has_children = bool(children.get(n.key))
        if n.table_en:
            node_type[n.key] = 2
        elif has_children:
            node_type[n.key] = 1
        else:
            node_type[n.key] = 2 if args.leaf_without_en_as_table else 1

    # 目录编号（WIND01, WIND0101 ...）
    dir_index_path: dict[tuple[str, ...], tuple[int, ...]] = {}
    dir_code: dict[tuple[str, ...], str] = {}

    top_dirs = [n for n in nodes if n.parent_key is None and node_type[n.key] == 1]
    top_dirs.sort(key=lambda x: x.order)

    for i, n in enumerate(top_dirs, start=1):
        path = (i,)
        dir_index_path[n.key] = path
        dir_code[n.key] = f"{args.source_code}{''.join(f'{v:02d}' for v in path)}"

    queue = top_dirs[:]
    while queue:
        parent = queue.pop(0)
        parent_code = dir_code[parent.key]
        child_dirs = [node_map[k] for k in children.get(parent.key, []) if node_type[k] == 1]
        child_dirs.sort(key=lambda x: x.order)

        for idx, child in enumerate(child_dirs, start=1):
            p = dir_index_path[parent.key] + (idx,)
            dir_index_path[child.key] = p
            dir_code[child.key] = f"{args.source_code}{''.join(f'{v:02d}' for v in p)}"
            queue.append(child)

    # 组装行
    rows: list[dict[str, object]] = []
    rows.append(
        {
            COL_SEQ: None,
            COL_NODE_CODE: args.source_code,
            COL_NODE_NAME: args.source_name,
            COL_PARENT_CODE: "0",
            COL_DESC: args.source_desc,
            COL_TYPE: 0,
            COL_UPDATE_TIME: args.date,
        }
    )

    used_table_codes: set[str] = set()

    for n in sorted(nodes, key=lambda x: x.order):
        ntype = node_type[n.key]
        parent_code = args.source_code
        if n.parent_key is not None:
            parent_code = dir_code.get(n.parent_key, args.source_code)

        if ntype == 1:
            code = dir_code.get(n.key)
            if not code:
                # 容错：无父目录链时按顶层目录顺延
                idx = len([r for r in rows if str(r[COL_NODE_CODE]).startswith(args.source_code) and r[COL_TYPE] == 1]) + 1
                code = f"{args.source_code}{idx:02d}"
            name = n.name_cn
        else:
            base = n.table_en if n.table_en else sanitize_for_code(n.name_cn)
            code = f"{args.table_prefix}_{base}"
            serial = 2
            while code in used_table_codes:
                code = f"{args.table_prefix}_{base}_{serial}"
                serial += 1
            used_table_codes.add(code)
            name = n.name_cn

        rows.append(
            {
                COL_SEQ: None,
                COL_NODE_CODE: code,
                COL_NODE_NAME: name,
                COL_PARENT_CODE: parent_code,
                COL_DESC: name,
                COL_TYPE: ntype,
                COL_UPDATE_TIME: args.date,
            }
        )

    for i, r in enumerate(rows, start=1):
        r[COL_SEQ] = i

    df = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    return df


def validate_output(df: pd.DataFrame, args) -> None:
    errors: list[str] = []

    if list(df.columns) != OUTPUT_COLUMNS:
        errors.append("输出列结构与模板不一致")

    if len(df) < args.min_nodes:
        errors.append(f"节点总数过少: {len(df)} < {args.min_nodes}")

    dup = int(df[COL_NODE_CODE].duplicated().sum())
    if dup > 0:
        errors.append(f"节点编号重复: {dup}")

    valid_codes = set(df[COL_NODE_CODE].astype(str))
    invalid_parent = df[
        (~df[COL_PARENT_CODE].astype(str).isin(valid_codes))
        & (df[COL_PARENT_CODE].astype(str) != "0")
    ]
    if not invalid_parent.empty:
        errors.append(f"父节点编号无效记录: {len(invalid_parent)}")

    bad_dir = df[
        (df[COL_TYPE].isin([0, 1]))
        & (~df[COL_NODE_CODE].astype(str).str.startswith(args.source_code))
    ]
    if not bad_dir.empty:
        errors.append(f"目录/根节点前缀不符合 {args.source_code}: {len(bad_dir)}")

    bad_table = df[
        (df[COL_TYPE] == 2)
        & (~df[COL_NODE_CODE].astype(str).str.startswith(f"{args.table_prefix}_"))
    ]
    if not bad_table.empty:
        errors.append(f"表节点前缀不符合 {args.table_prefix}_: {len(bad_table)}")

    if errors:
        raise RuntimeError("结果校验失败:\n - " + "\n - ".join(errors))


def parse_args():
    p = argparse.ArgumentParser(description="万得自绘目录 OCR 爬虫（D3D/自绘界面兼容）")

    p.add_argument("--region", default=None, help="树区域截图范围 x,y,w,h；不填则交互选区")
    p.add_argument("--max-pages", type=int, default=250, help="最大滚动页数")
    p.add_argument("--scroll-pixels", type=int, default=300, help="????????????????")
    p.add_argument("--scroll-wait", type=float, default=1.2, help="?????????????")
    p.add_argument("--stop-repeat-pages", type=int, default=4, help="连续签名重复后停止")
    p.add_argument("--signature-top-n", type=int, default=25, help="页面签名取前N行")

    p.add_argument("--ocr-lang", default="chi_sim+eng", help="tesseract 语言包，如 chi_sim+eng")
    p.add_argument("--tesseract-cmd", default=None, help="tesseract.exe 完整路径")
    p.add_argument("--min-conf", type=float, default=30, help="OCR 最低置信度")
    p.add_argument("--indent-step", type=int, default=18, help="层级缩进像素步长")

    p.add_argument("--auto-expand", action="store_true", help="采集过程中尝试自动展开目录")
    p.add_argument("--expand-icon-offset", type=int, default=14, help="展开图标相对文本左侧偏移")
    p.add_argument("--expand-max-clicks-per-page", type=int, default=12, help="????????????????")
    p.add_argument("--expand-click-wait", type=float, default=0.2, help="??????????????")
    p.add_argument("--expand-after-page-wait", type=float, default=0.9, help="???????????????")

    p.add_argument("--source-code", default="WIND", help="根/目录编码前缀，默认 WIND")
    p.add_argument("--table-prefix", default="WIND", help="表编码前缀，默认 WIND")
    p.add_argument("--source-name", default="万得数据", help="根节点名称")
    p.add_argument("--source-desc", default="南京万得资讯科技有限公司", help="根节点描述")
    p.add_argument("--date", default=date.today().isoformat(), help="更新时间 YYYY-MM-DD")

    p.add_argument("--leaf-without-en-as-table", action="store_true", help="无英文名叶子也按表处理")
    p.add_argument("--min-nodes", type=int, default=30, help="最小节点数校验")

    p.add_argument("--output", default="万得数据节点目录_爬取结果.xlsx", help="输出文件名")
    p.add_argument("--sheet-name", default="外部数据目录", help="输出工作表名")
    p.add_argument("--debug-dir", default=None, help="可选，保存每页截图和OCR文本")
    p.add_argument("--mouse-safe-margin", type=int, default=20, help="???????????????? FAILSAFE")
    p.add_argument("--disable-failsafe", action="store_true", help="?? PyAutoGUI FAILSAFE??????????")
    p.add_argument("--skip-login-wait", action="store_true", help="跳过登录完成等待")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    if not args.skip_login_wait:
        print("[提示] 请先打开并登录万得客户端，并切到数据字典树页面。")
        input("[提示] 准备好后按回车开始 OCR 采集... ")

    lines = collect_ocr_lines(args)
    nodes = build_nodes_from_lines(lines, args)
    df = build_output_dataframe(nodes, args)
    validate_output(df, args)

    df.to_excel(args.output, index=False, sheet_name=args.sheet_name)

    type_counts = df[COL_TYPE].value_counts().to_dict()
    print(
        f"[完成] 总节点={len(df)}，数据源={type_counts.get(0, 0)}，"
        f"目录={type_counts.get(1, 0)}，表={type_counts.get(2, 0)}"
    )
    print(f"[完成] 输出文件: {Path(args.output).resolve()}")


if __name__ == "__main__":
    main()

