from __future__ import annotations

import argparse
import contextlib
import io
import re
from pathlib import Path


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def load_pywinauto():
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
    # 兼容不同 pywinauto 版本
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


def main() -> None:
    parser = argparse.ArgumentParser(description="查看客户端控件结构并导出全文")
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
    parser.add_argument(
        "--output",
        default="控件树全文.txt",
        help="控件树全文输出文件",
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

    try:
        Desktop = load_pywinauto()
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return

    desktop = Desktop(backend="uia")
    candidates = list_candidate_windows(desktop, args.window_title_regex)
    window = choose_window(candidates, args.window_pick_index)

    try:
        window.set_focus()
    except Exception:
        pass

    print(f"[信息] 已选择窗口: {normalize_text(window.window_text())}")
    tree_text = build_control_tree_text(window)

    preview_chars = max(100, args.preview_chars)
    preview = tree_text[:preview_chars]
    if len(tree_text) > preview_chars:
        preview += "\n...（已截断）"

    print("[信息] 控件树预览：")
    print(preview)

    out = Path(args.output)
    out.write_text(tree_text, encoding="utf-8")
    print(f"[完成] 控件树全文已保存: {out.resolve()}")


if __name__ == "__main__":
    main()
