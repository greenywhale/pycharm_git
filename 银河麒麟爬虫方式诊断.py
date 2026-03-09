from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MethodReport:
    method: str
    score: int
    status: str
    evidence: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    suggestion: list[str] = field(default_factory=list)


@dataclass
class DiagnoseReport:
    timestamp: str
    os_info: str
    window_regex: str
    process_regex: str
    http_api: MethodReport
    atspi: MethodReport
    ocr: MethodReport
    final_recommendation: str
    conclusion: str


def now_text() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


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


def safe_import(name: str):
    try:
        return __import__(name)
    except Exception:
        return None


def choose_region_interactive() -> tuple[int, int, int, int]:
    pynput = safe_import("pynput")
    if pynput is None:
        raise RuntimeError("未安装 pynput，无法交互选区。")

    from pynput import mouse as pynput_mouse

    controller = pynput_mouse.Controller()
    print("[提示] 将鼠标移动到树区域左上角后按回车...")
    input()
    x1, y1 = controller.position
    print(f"[信息] 左上角: ({int(x1)}, {int(y1)})")

    print("[提示] 将鼠标移动到树区域右下角后按回车...")
    input()
    x2, y2 = controller.position
    print(f"[信息] 右下角: ({int(x2)}, {int(y2)})")

    x = min(int(x1), int(x2))
    y = min(int(y1), int(y2))
    w = abs(int(x2) - int(x1))
    h = abs(int(y2) - int(y1))
    if w < 40 or h < 40:
        raise RuntimeError("选择区域过小，请重试。")
    return x, y, w, h


def diagnose_http_api(process_regex: str, sample_seconds: float, devtools_port_range: str) -> MethodReport:
    rep = MethodReport(method="HTTP/API", score=0, status="不可判断")

    psutil = safe_import("psutil")
    if psutil is None:
        rep.blockers.append("缺少依赖 psutil（pip install psutil）")
        rep.status = "不可判断"
        rep.suggestion.append("安装 psutil 后重试，可识别客户端网络行为。")
        return rep

    try:
        process_pattern = re.compile(process_regex, flags=re.IGNORECASE)
    except re.error as exc:
        rep.blockers.append(f"process regex 无效: {exc}")
        return rep

    matched = []
    for p in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = normalize_text(p.info.get("name", ""))
            cmdline = " ".join(p.info.get("cmdline") or [])
            text = f"{name} {cmdline}"
            if process_pattern.search(text):
                matched.append(p)
        except Exception:
            continue

    rep.evidence.append(f"匹配到疑似客户端进程数: {len(matched)}")
    if not matched:
        rep.status = "较难"
        rep.score = 15
        rep.blockers.append("未匹配到客户端进程，无法分析网络行为。")
        rep.suggestion.append("先手工打开客户端后再执行诊断，或调整 --process-regex。")
        return rep

    start = time.time()
    conn_seen = []
    local_listen_ports: set[int] = set()
    remote_targets: set[str] = set()
    pids = {p.pid for p in matched}

    while time.time() - start < max(0.5, sample_seconds):
        for p in list(matched):
            try:
                for c in p.connections(kind="inet"):
                    laddr = getattr(c, "laddr", None)
                    raddr = getattr(c, "raddr", None)
                    status = normalize_text(getattr(c, "status", ""))
                    if status:
                        conn_seen.append(status)

                    if laddr and getattr(laddr, "port", None):
                        lport = int(laddr.port)
                        if status.upper() == "LISTEN":
                            local_listen_ports.add(lport)

                    if raddr and getattr(raddr, "ip", None) and getattr(raddr, "port", None):
                        remote_targets.add(f"{raddr.ip}:{raddr.port}")
            except Exception:
                continue
        time.sleep(0.25)

    rep.evidence.append(f"采样进程PID: {sorted(pids)}")
    rep.evidence.append(f"远程连接目标数: {len(remote_targets)}")
    rep.evidence.append(f"监听端口数: {len(local_listen_ports)}")

    ports_to_probe: set[int] = set(local_listen_ports)
    m = re.match(r"^(\d+)-(\d+)$", normalize_text(devtools_port_range))
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a > b:
            a, b = b, a
        for p in range(a, b + 1):
            ports_to_probe.add(p)

    devtools_hits = []
    for port in sorted(ports_to_probe):
        for path in ["/json/version", "/json"]:
            url = f"http://127.0.0.1:{port}{path}"
            try:
                with urllib.request.urlopen(url, timeout=0.25) as resp:
                    raw = resp.read(2048).decode("utf-8", errors="ignore")
                    if "Browser" in raw or "webSocketDebuggerUrl" in raw or raw.startswith("["):
                        devtools_hits.append(url)
                        break
            except (urllib.error.URLError, TimeoutError, OSError):
                continue

    if devtools_hits:
        rep.status = "可行"
        rep.score = 88
        rep.evidence.append("检测到疑似可调用的本地HTTP调试接口：")
        rep.evidence.extend([f"- {u}" for u in devtools_hits[:5]])
        rep.suggestion.append("优先尝试 API/网络层方案，可配合 mitmproxy 抓包验证接口字段。")
        return rep

    https_count = 0
    for t in remote_targets:
        try:
            port = int(t.rsplit(":", 1)[1])
            if port in {443, 8443}:
                https_count += 1
        except Exception:
            continue

    if remote_targets:
        rep.status = "可能"
        rep.score = 38
        rep.evidence.append(f"检测到远程网络通信（含HTTPS目标数={https_count}），但未发现本地可直接调用HTTP接口。")
        rep.suggestion.append("可用抓包工具进一步确认是否存在可复用API；若全程加密且有签名，HTTP方案成本高。")
    else:
        rep.status = "较难"
        rep.score = 20
        rep.blockers.append("未观察到稳定网络连接，HTTP/API 方案证据不足。")
        rep.suggestion.append("操作客户端翻页/搜索后重试诊断，增加 --http-sample-seconds。")

    return rep


def role_matches(name: str, candidates: set[str]) -> bool:
    n = normalize_text(name).lower()
    return n in candidates


def walk_atspi_tree(root, max_nodes: int):
    queue = [root]
    idx = 0
    while queue and idx < max_nodes:
        node = queue.pop(0)
        idx += 1
        yield node

        child_count = 0
        try:
            child_count = int(getattr(node, "childCount", 0))
        except Exception:
            child_count = 0

        for i in range(child_count):
            try:
                ch = node.getChildAtIndex(i)
                if ch is not None:
                    queue.append(ch)
            except Exception:
                continue


def diagnose_atspi(window_regex: str, max_nodes: int) -> MethodReport:
    rep = MethodReport(method="AT-SPI2控件结构", score=0, status="不可判断")

    pyatspi = safe_import("pyatspi")
    if pyatspi is None:
        rep.blockers.append("缺少 pyatspi（Linux安装：sudo apt install python3-pyatspi）")
        rep.status = "不可判断"
        rep.suggestion.append("安装 pyatspi 后重新诊断。")
        return rep

    try:
        pattern = re.compile(window_regex, flags=re.IGNORECASE)
    except re.error as exc:
        rep.blockers.append(f"window regex 无效: {exc}")
        return rep

    desktop = pyatspi.Registry.getDesktop(0)

    windows = []
    app_count = int(getattr(desktop, "childCount", 0))
    for ai in range(app_count):
        try:
            app = desktop.getChildAtIndex(ai)
        except Exception:
            continue
        app_name = normalize_text(getattr(app, "name", ""))

        wc = int(getattr(app, "childCount", 0))
        for wi in range(wc):
            try:
                w = app.getChildAtIndex(wi)
            except Exception:
                continue
            title = normalize_text(getattr(w, "name", ""))
            if not title:
                continue
            if pattern.search(title):
                windows.append((app_name, w))

    rep.evidence.append(f"匹配窗口数量: {len(windows)}")
    if not windows:
        rep.status = "较难"
        rep.score = 10
        rep.blockers.append("未匹配到客户端窗口。")
        rep.suggestion.append("先将客户端置前台并调整 --window-regex 后重试。")
        return rep

    tree_roles = {"tree", "tree table", "outline", "list"}
    item_roles = {"tree item", "list item", "table cell", "row header"}

    best_tree_count = 0
    best_item_count = 0
    best_window_title = ""

    for app_name, w in windows[:5]:
        title = normalize_text(getattr(w, "name", ""))
        tree_count = 0
        item_count = 0
        scanned = 0

        for node in walk_atspi_tree(w, max_nodes=max_nodes):
            scanned += 1
            role_name = ""
            try:
                role_name = normalize_text(node.getRoleName()).lower()
            except Exception:
                pass

            if role_matches(role_name, tree_roles):
                tree_count += 1
            if role_matches(role_name, item_roles):
                item_count += 1

        rep.evidence.append(
            f"窗口[{title}] 扫描节点={scanned}, 树容器角色数={tree_count}, 项角色数={item_count}"
        )

        if (tree_count, item_count) > (best_tree_count, best_item_count):
            best_tree_count, best_item_count = tree_count, item_count
            best_window_title = title

    if best_tree_count >= 1 and best_item_count >= 10:
        rep.status = "可行"
        rep.score = 90
        rep.evidence.append(f"最佳窗口: {best_window_title}")
        rep.suggestion.append("优先使用 AT-SPI2 控件方式抓取，稳定性和结构化程度通常高于 OCR。")
    elif best_tree_count >= 1:
        rep.status = "可能"
        rep.score = 58
        rep.evidence.append(f"最佳窗口: {best_window_title}")
        rep.suggestion.append("控件层部分可见，先打印控件树再定向定位树控件。")
    else:
        rep.status = "较难"
        rep.score = 18
        rep.blockers.append("未发现可用 tree/list 角色，疑似自绘控件。")
        rep.suggestion.append("AT-SPI2 不可读时，改用 OCR 方案。")

    return rep


def resolve_tesseract() -> str | None:
    path = shutil.which("tesseract")
    if path:
        return path

    for p in [
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        "/usr/bin/tesseract",
        "/usr/local/bin/tesseract",
    ]:
        if Path(p).exists():
            return p
    return None


def preprocess_image(np_mod, cv2_mod, img):
    arr = np_mod.array(img)
    gray = cv2_mod.cvtColor(arr, cv2_mod.COLOR_RGB2GRAY)
    gray = cv2_mod.GaussianBlur(gray, (3, 3), 0)
    _, bw = cv2_mod.threshold(gray, 0, 255, cv2_mod.THRESH_BINARY + cv2_mod.THRESH_OTSU)
    return bw


def diagnose_ocr(region_text: str | None, pick_region: bool, min_conf: float) -> MethodReport:
    rep = MethodReport(method="OCR", score=0, status="不可判断")

    try:
        import cv2
        import mss
        import numpy as np
        import pytesseract
        from PIL import Image
        from pytesseract import Output
    except Exception:
        rep.blockers.append("OCR依赖缺失（pip install mss pytesseract opencv-python numpy pillow）")
        rep.status = "不可判断"
        rep.suggestion.append("安装 OCR 依赖后重试。")
        return rep

    tess = resolve_tesseract()
    if not tess:
        rep.blockers.append("未找到 tesseract 可执行文件")
        rep.status = "不可判断"
        rep.suggestion.append("安装 tesseract-ocr 与中文包：tesseract-ocr-chi-sim")
        return rep

    pytesseract.pytesseract.tesseract_cmd = tess
    try:
        ver = str(pytesseract.get_tesseract_version())
        rep.evidence.append(f"Tesseract: {tess}, version={ver}")
    except Exception as exc:
        rep.blockers.append(f"tesseract 不可调用: {exc}")
        rep.status = "不可判断"
        return rep

    region = None
    try:
        if pick_region:
            region = choose_region_interactive()
        elif region_text:
            region = parse_region(region_text)
    except Exception as exc:
        rep.blockers.append(f"区域选择失败: {exc}")
        rep.status = "不可判断"
        return rep

    with mss.mss() as sct:
        if region:
            x, y, w, h = region
            shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
            rep.evidence.append(f"OCR区域: x={x}, y={y}, w={w}, h={h}")
        else:
            mon = sct.monitors[0]
            w = int(mon["width"] * 0.5)
            h = int(mon["height"] * 0.8)
            x = int(mon["left"] + mon["width"] * 0.25)
            y = int(mon["top"] + mon["height"] * 0.1)
            shot = sct.grab({"left": x, "top": y, "width": w, "height": h})
            rep.evidence.append("未指定区域，已使用屏幕中部自动测试区域。")
            rep.evidence.append(f"OCR区域: x={x}, y={y}, w={w}, h={h}")

    img = Image.frombytes("RGB", shot.size, shot.rgb)
    bw = preprocess_image(np, cv2, img)

    data = pytesseract.image_to_data(
        bw,
        lang="chi_sim+eng",
        config="--oem 3 --psm 6",
        output_type=Output.DICT,
    )

    n = len(data.get("text", []))
    valid = 0
    conf_sum = 0.0
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
        valid += 1
        conf_sum += conf

    avg_conf = (conf_sum / valid) if valid else 0.0
    rep.evidence.append(f"OCR有效词数(min_conf={min_conf}): {valid}")
    rep.evidence.append(f"OCR平均置信度: {avg_conf:.1f}")

    if valid >= 40 and avg_conf >= 45:
        rep.status = "可行"
        rep.score = 86
        rep.suggestion.append("OCR识别质量较好，可作为主方案或AT-SPI2失败时兜底方案。")
    elif valid >= 12:
        rep.status = "可能"
        rep.score = 62
        rep.suggestion.append("OCR可用，建议固定截图区域并放慢滚动速度提升准确率。")
    else:
        rep.status = "较难"
        rep.score = 25
        rep.blockers.append("当前画面OCR有效识别过少。")
        rep.suggestion.append("请确保目录区域清晰、无遮挡，并使用 --pick-region 重新测试。")

    return rep


def decide_final(http_r: MethodReport, atspi_r: MethodReport, ocr_r: MethodReport) -> tuple[str, str]:
    ranking = sorted([http_r, atspi_r, ocr_r], key=lambda x: x.score, reverse=True)
    top = ranking[0]

    if top.method == "AT-SPI2控件结构" and top.score >= 55:
        return (
            "优先 AT-SPI2 控件爬虫",
            "检测到可访问控件树，建议先用控件方式，结构化程度高、后处理成本低。",
        )

    if top.method == "OCR" and top.score >= 55:
        return (
            "优先 OCR 爬虫",
            "控件可访问性不足或不稳定，OCR可行性更高，建议使用区域固定+慢速滚动策略。",
        )

    if top.method == "HTTP/API" and top.score >= 70:
        return (
            "优先 HTTP/API 方案",
            "发现疑似可调用接口，建议先做接口复现与鉴权验证，再决定是否保留UI层爬虫。",
        )

    return (
        "建议双轨：AT-SPI2 + OCR",
        "当前证据不足以单一路线完全覆盖，建议先做AT-SPI2试抓，失败后自动回退OCR。",
    )


def render_text(report: DiagnoseReport) -> str:
    lines = []
    lines.append("银河麒麟客户端爬虫方式诊断报告")
    lines.append(f"生成时间: {report.timestamp}")
    lines.append(f"系统信息: {report.os_info}")
    lines.append(f"窗口匹配正则: {report.window_regex}")
    lines.append(f"进程匹配正则: {report.process_regex}")
    lines.append("")

    def add_method(rep: MethodReport):
        lines.append(f"[{rep.method}] 评分={rep.score} 状态={rep.status}")
        if rep.evidence:
            lines.append("证据:")
            for x in rep.evidence:
                lines.append(f"- {x}")
        if rep.blockers:
            lines.append("阻塞点:")
            for x in rep.blockers:
                lines.append(f"- {x}")
        if rep.suggestion:
            lines.append("建议:")
            for x in rep.suggestion:
                lines.append(f"- {x}")
        lines.append("")

    add_method(report.http_api)
    add_method(report.atspi)
    add_method(report.ocr)

    lines.append(f"最终建议: {report.final_recommendation}")
    lines.append(f"结论说明: {report.conclusion}")
    return "\n".join(lines)


def parse_args():
    p = argparse.ArgumentParser(description="银河麒麟客户端爬虫方式诊断（HTTP/API、AT-SPI2、OCR）")
    p.add_argument("--window-regex", default=r".*(万得|Wind|国投).*", help="窗口标题匹配正则")
    p.add_argument("--process-regex", default=r"(wind|wft|万得|国投)", help="进程名/命令行匹配正则")
    p.add_argument("--http-sample-seconds", type=float, default=6.0, help="HTTP/API网络采样秒数")
    p.add_argument("--devtools-port-range", default="9222-9333", help="本地调试端口扫描范围")
    p.add_argument("--atspi-max-nodes", type=int, default=12000, help="AT-SPI2 每窗口最大扫描节点数")
    p.add_argument("--region", default=None, help="OCR测试区域 x,y,w,h")
    p.add_argument("--pick-region", action="store_true", help="交互选取 OCR 测试区域")
    p.add_argument("--ocr-min-conf", type=float, default=25.0, help="OCR最低置信度")
    p.add_argument("--json-output", default="爬虫方式诊断报告.json", help="诊断结果 JSON 输出路径")
    p.add_argument("--text-output", default="爬虫方式诊断报告.txt", help="诊断结果文本输出路径")
    return p.parse_args()


def main() -> None:
    args = parse_args()

    os_info = f"{platform.platform()} | session={normalize_text(__import__('os').environ.get('XDG_SESSION_TYPE', 'unknown'))}"
    print(f"[信息] 系统: {os_info}")
    print("[信息] 开始诊断 HTTP/API ...")
    http_rep = diagnose_http_api(args.process_regex, args.http_sample_seconds, args.devtools_port_range)

    print("[信息] 开始诊断 AT-SPI2 ...")
    atspi_rep = diagnose_atspi(args.window_regex, args.atspi_max_nodes)

    print("[信息] 开始诊断 OCR ...")
    ocr_rep = diagnose_ocr(args.region, args.pick_region, args.ocr_min_conf)

    final_rec, conclusion = decide_final(http_rep, atspi_rep, ocr_rep)

    report = DiagnoseReport(
        timestamp=now_text(),
        os_info=os_info,
        window_regex=args.window_regex,
        process_regex=args.process_regex,
        http_api=http_rep,
        atspi=atspi_rep,
        ocr=ocr_rep,
        final_recommendation=final_rec,
        conclusion=conclusion,
    )

    text = render_text(report)
    print("\n" + text)

    text_path = Path(args.text_output)
    json_path = Path(args.json_output)

    text_path.write_text(text, encoding="utf-8")
    json_path.write_text(
        json.dumps(asdict(report), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\n[完成] 文本报告: {text_path.resolve()}")
    print(f"[完成] JSON报告: {json_path.resolve()}")


if __name__ == "__main__":
    main()
