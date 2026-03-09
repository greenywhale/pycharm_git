from __future__ import annotations

import argparse
import contextlib
import io
import platform
import re
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def parse_region(region_text: str) -> tuple[int, int, int, int]:
    parts = [p.strip() for p in region_text.split(",")]
    if len(parts) != 4:
        raise ValueError("--region 格式必须是 x,y,w,h")
    x, y, w, h = [int(p) for p in parts]
    if w <= 0 or h <= 0:
        raise ValueError("--region 的 w 和 h 必须大于0")
    return x, y, w, h


def load_pywinauto():
    if platform.system().lower() != "windows":
        raise RuntimeError("pywinauto 仅支持 Windows")
    try:
        from pywinauto import Desktop
    except ImportError as exc:
        raise RuntimeError("缺少依赖 pywinauto，请先执行: pip install pywinauto") from exc
    return Desktop


def list_candidate_windows(desktop, title_regex: str | None):
    windows = desktop.windows(visible_only=True)
    candidates = []
    for w in windows:
        title = normalize_text(w.window_text())
        if not title:
            continue
        if title_regex and not re.search(title_regex, title, flags=re.IGNORECASE):
            continue
        candidates.append(w)
    return candidates


def choose_window(candidates: list, pick_index: int | None):
    if not candidates:
        raise RuntimeError("未找到可见窗口，请先打开客户端。")

    print("[信息] 可选窗口如下：")
    for i, w in enumerate(candidates):
        title = normalize_text(w.window_text())
        handle = getattr(w, "handle", None)
        print(f"  [{i}] 句柄={handle} 标题={title}")

    if pick_index is not None:
        idx = pick_index
    else:
        raw = input("请输入窗口序号（默认0）: ").strip()
        idx = int(raw) if raw else 0

    if idx < 0 or idx >= len(candidates):
        raise RuntimeError(f"窗口序号越界: {idx}，可选范围 0~{len(candidates)-1}")

    return candidates[idx]


def _descendants_tree_text(window) -> str:
    lines = []
    try:
        descendants = window.descendants()
    except Exception:
        descendants = []

    for d in descendants:
        try:
            info = d.element_info
            ctrl = normalize_text(getattr(info, "control_type", ""))
            aid = normalize_text(getattr(info, "automation_id", ""))
            name = normalize_text(d.window_text())
            cls = normalize_text(getattr(info, "class_name", ""))
            lines.append(f"control={ctrl} | name={name} | auto_id={aid} | class={cls}")
        except Exception:
            continue

    return "\n".join(lines)


def build_control_tree_text(window) -> str:
    if hasattr(window, "dump_tree"):
        try:
            return window.dump_tree()
        except Exception:
            pass

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        try:
            window.print_control_identifiers(depth=30)
        except Exception:
            pass
    text = buf.getvalue().strip()
    if text:
        return text

    text = _descendants_tree_text(window)
    return text if text else "[警告] 未能读取控件树（可能为自绘控件或权限不足）。"


@dataclass
class OCRLine:
    text: str
    x: int
    y: int
    h: int
    conf: float


def resolve_tesseract_path(tesseract_cmd: str | None) -> str:
    candidates = []
    if tesseract_cmd:
        candidates.append(tesseract_cmd)
    else:
        found = shutil.which("tesseract")
        if found:
            candidates.append(found)
        candidates.extend(
            [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
                "/usr/bin/tesseract",
                "/usr/local/bin/tesseract",
            ]
        )

    for c in candidates:
        if c and Path(c).exists():
            return c

    raise RuntimeError("未找到 tesseract，请先安装或用 --tesseract-cmd 指定路径。")


def load_ocr_runtime(tesseract_cmd: str | None):
    try:
        import cv2
        import mss
        import numpy as np
        import pytesseract
        from PIL import Image
        from pytesseract import Output
        from pynput import mouse as pynput_mouse
    except ImportError as exc:
        raise RuntimeError(
            "缺少 OCR 依赖，请先安装: pip install pytesseract opencv-python numpy mss pynput pillow"
        ) from exc

    tess = resolve_tesseract_path(tesseract_cmd)
    pytesseract.pytesseract.tesseract_cmd = tess
    try:
        ver = str(pytesseract.get_tesseract_version())
    except Exception as exc:
        raise RuntimeError(f"已定位 tesseract 但无法调用: {tess}") from exc

    print(f"[信息] OCR模式，使用 Tesseract: {tess} (version={ver})")
    return cv2, mss, np, pytesseract, Output, Image, pynput_mouse


def choose_region_interactive(mouse_controller) -> tuple[int, int, int, int]:
    print("[提示] 将鼠标移动到树区域左上角后按回车...")
    input()
    x1, y1 = mouse_controller.position
    x1, y1 = int(x1), int(y1)
    print(f"[信息] 左上角: ({x1}, {y1})")

    print("[提示] 将鼠标移动到树区域右下角后按回车...")
    input()
    x2, y2 = mouse_controller.position
    x2, y2 = int(x2), int(y2)
    print(f"[信息] 右下角: ({x2}, {y2})")

    x = min(x1, x2)
    y = min(y1, y2)
    w = abs(x2 - x1)
    h = abs(y2 - y1)
    if w < 50 or h < 50:
        raise RuntimeError("选择区域过小，请重试。")

    print(f"[信息] 使用区域: x={x}, y={y}, w={w}, h={h}")
    return x, y, w, h


def preprocess_image(img, cv2, np):
    arr = np.array(img)
    gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
    gray = cv2.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return bw


def ocr_lines_from_image(img, cv2, np, pytesseract, Output, ocr_lang: str, min_conf: float) -> list[OCRLine]:
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

    lines: list[OCRLine] = []
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
        lines.append(OCRLine(text=text, x=x, y=y, h=h, conf=conf))

    lines.sort(key=lambda r: (r.y, r.x))
    return lines


def build_ocr_tree_text(lines: list[OCRLine], region: tuple[int, int, int, int], indent_step: int) -> str:
    if not lines:
        return "[警告] OCR未识别到可用文本。"

    min_x = min(ln.x for ln in lines)
    out = [
        "[模式] OCR结构探测",
        f"[区域] x={region[0]}, y={region[1]}, w={region[2]}, h={region[3]}",
        f"[行数] {len(lines)}",
        "",
    ]

    step = max(1, int(indent_step))
    for i, ln in enumerate(lines, start=1):
        depth = int(round((ln.x - min_x) / step)) + 1
        depth = max(1, depth)
        indent = "  " * (depth - 1)
        out.append(
            f"{i:04d}. {indent}- {ln.text}  [x={ln.x}, y={ln.y}, conf={ln.conf:.0f}]"
        )

    return "\n".join(out)


def run_windows_mode(args) -> str:
    Desktop = load_pywinauto()
    desktop = Desktop(backend="uia")
    candidates = list_candidate_windows(desktop, args.window_title_regex)
    window = choose_window(candidates, args.window_pick_index)

    try:
        window.set_focus()
    except Exception:
        pass

    print(f"[信息] 已选择窗口: {normalize_text(window.window_text())}")
    return build_control_tree_text(window)


def run_ocr_mode(args) -> str:
    cv2, mss, np, pytesseract, Output, Image, pynput_mouse = load_ocr_runtime(args.tesseract_cmd)

    if args.region:
        region = parse_region(args.region)
        print(f"[信息] 使用命令行区域: {region}")
    else:
        mouse_controller = pynput_mouse.Controller()
        region = choose_region_interactive(mouse_controller)

    x, y, w, h = region
    with mss.mss() as sct:
        shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
    img = Image.frombytes("RGB", shot.size, shot.rgb)

    lines = ocr_lines_from_image(img, cv2, np, pytesseract, Output, args.ocr_lang, args.min_conf)
    return build_ocr_tree_text(lines, region, args.indent_step)


def main() -> None:
    parser = argparse.ArgumentParser(description="查看客户端控件结构（Windows UIA / Linux OCR）")
    parser.add_argument("--mode", default="auto", help="auto/windows/ocr")
    parser.add_argument(
        "--window-title-regex",
        default=None,
        help="可选。窗口标题正则，例如 .*万得.*",
    )
    parser.add_argument(
        "--window-pick-index",
        type=int,
        default=None,
        help="可选。窗口序号，不填则运行时输入",
    )
    parser.add_argument("--region", default=None, help="OCR模式下截图区域 x,y,w,h")
    parser.add_argument("--tesseract-cmd", default=None, help="OCR模式下 tesseract 路径")
    parser.add_argument("--ocr-lang", default="chi_sim+eng", help="OCR语言包")
    parser.add_argument("--min-conf", type=float, default=25, help="OCR最低置信度")
    parser.add_argument("--indent-step", type=int, default=18, help="OCR层级缩进像素步长")
    parser.add_argument(
        "--output",
        default="控件树全文.txt",
        help="输出文件",
    )
    parser.add_argument(
        "--preview-chars",
        type=int,
        default=4000,
        help="终端预览字符数",
    )
    parser.add_argument(
        "--skip-login-wait",
        action="store_true",
        help="跳过登录完成等待提示",
    )
    args = parser.parse_args()

    if not args.skip_login_wait:
        print("[提示] 请先打开并登录客户端，进入目标页面后再继续。")
        input("[提示] 准备好后按回车开始... ")

    mode = args.mode.lower()
    if mode not in {"auto", "windows", "ocr"}:
        print("[错误] --mode 仅支持 auto/windows/ocr")
        return

    if mode == "auto":
        mode = "windows" if platform.system().lower() == "windows" else "ocr"

    if mode == "windows":
        try:
            tree_text = run_windows_mode(args)
        except Exception as exc:
            print(f"[警告] Windows模式失败: {exc}")
            print("[信息] 自动切换到 OCR 模式。")
            tree_text = run_ocr_mode(args)
    else:
        tree_text = run_ocr_mode(args)

    preview_chars = max(100, args.preview_chars)
    preview = tree_text[:preview_chars]
    if len(tree_text) > preview_chars:
        preview += "\n...（已截断）"

    print("[信息] 结构预览：")
    print(preview)

    out = Path(args.output)
    out.write_text(tree_text, encoding="utf-8")
    print(f"[完成] 结构全文已保存: {out.resolve()}")


if __name__ == "__main__":
    main()
