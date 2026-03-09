from __future__ import annotations

import argparse
import contextlib
import io
import re
import time
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
class CrawlArgs:
    window_title_regex: str | None
    window_pick_index: int | None
    exe_path: str | None
    tree_auto_id: str | None
    tree_title: str | None
    tree_index: int
    source_code: str
    table_code_prefix: str
    prefer_bracket_english: bool
    source_name: str
    source_desc: str
    updated_at: str
    output_path: Path
    launch_wait_seconds: int
    expand_wait_seconds: float
    expand_retry_times: int
    max_depth: int
    print_controls: bool
    controls_output: Path | None
    skip_login_wait: bool
    min_nodes: int
    must_contain_regex: str | None
    skip_validation: bool


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def sanitize_for_code(name: str) -> str:
    text = normalize_text(name).upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    text = text.strip("_")
    return text


def extract_table_cn_en(node_name: str) -> tuple[str, str | None]:
    """
    解析形如: 公司简介表[CompIntroductionSummary]
    返回: (公司简介表, CompIntroductionSummary)
    """
    text = normalize_text(node_name)
    match = re.match(r"^(?P<cn>.*?)\s*[\[\【](?P<en>[A-Za-z0-9_]+)[\]\】]\s*$", text)
    if not match:
        return text, None

    cn_name = normalize_text(match.group("cn")) or text
    en_name = normalize_text(match.group("en")) or None
    return cn_name, en_name


def format_dir_code(source_code: str, index_path: tuple[int, ...]) -> str:
    suffix = "".join(f"{i:02d}" for i in index_path)
    return f"{source_code}{suffix}"


def format_table_code(
    table_code_prefix: str,
    table_english_name: str | None,
    node_name: str,
    index_path: tuple[int, ...],
    used_codes: set[str],
) -> str:
    if table_english_name:
        cleaned_en = re.sub(r"[^A-Za-z0-9_]", "", table_english_name)
        base = cleaned_en if cleaned_en else sanitize_for_code(table_english_name)
    else:
        base = sanitize_for_code(node_name)

    prefix = normalize_text(table_code_prefix) or "WIND"
    if base:
        code = f"{prefix}_{base}"
    else:
        code = f"{prefix}_NODE{''.join(f'{i:02d}' for i in index_path)}"

    if code not in used_codes:
        used_codes.add(code)
        return code

    serial = 2
    while True:
        candidate = f"{code}_{serial}"
        if candidate not in used_codes:
            used_codes.add(candidate)
            return candidate
        serial += 1


def load_pywinauto():
    try:
        from pywinauto import Application, Desktop
    except ImportError as exc:
        raise RuntimeError("缺少依赖 pywinauto，请先执行: pip install pywinauto") from exc
    return Application, Desktop


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
        return None

    print("[信息] 可选窗口如下：")
    for i, w in enumerate(candidates):
        title = normalize_text(w.window_text())
        handle = getattr(w, "handle", None)
        print(f"  [{i}] 句柄={handle} 标题={title}")

    if pick_index is not None:
        if pick_index < 0 or pick_index >= len(candidates):
            raise RuntimeError(f"window-pick-index 越界: {pick_index}，可选范围 0~{len(candidates)-1}")
        return candidates[pick_index]

    raw = input("请输入窗口序号（默认0）: ").strip()
    if not raw:
        idx = 0
    else:
        idx = int(raw)
    if idx < 0 or idx >= len(candidates):
        raise RuntimeError(f"输入序号越界: {idx}，可选范围 0~{len(candidates)-1}")
    return candidates[idx]


def attach_or_start_window(args: CrawlArgs, Desktop, Application):
    desktop = Desktop(backend="uia")
    candidates = list_candidate_windows(desktop, args.window_title_regex)

    if not candidates and args.exe_path:
        print(f"[信息] 未找到匹配窗口，尝试启动客户端: {args.exe_path}")
        Application(backend="uia").start(args.exe_path)
        deadline = time.time() + args.launch_wait_seconds
        while time.time() < deadline:
            candidates = list_candidate_windows(desktop, args.window_title_regex)
            if candidates:
                break
            time.sleep(1)

    if not candidates:
        raise RuntimeError(
            "未找到可用窗口。请先打开客户端并登录，再运行脚本；"
            "或使用 --window-title-regex 调整匹配范围。"
        )

    target = choose_window(candidates, args.window_pick_index)
    if target is None:
        raise RuntimeError("无法选择目标窗口。")

    title = normalize_text(target.window_text())
    print(f"[信息] 已选择窗口: {title}")
    try:
        target.set_focus()
    except Exception:
        pass
    return target


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
    # ???? pywinauto ??
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
    return text if text else "[??] ??????????????????????"


def maybe_print_controls(window, output_path: Path | None) -> None:
    print("[??] ???????4000???:")
    text = build_control_tree_text(window)
    preview = text
    if len(preview) > 4000:
        preview = preview[:4000] + "\n...?????"
    print(preview)

    if output_path is not None:
        output_path.write_text(text, encoding="utf-8")
        print(f"[??] ????????: {output_path.resolve()}")
def find_tree_control(window, args: CrawlArgs):
    if args.tree_auto_id:
        spec = window.child_window(auto_id=args.tree_auto_id, control_type="Tree")
        if spec.exists(timeout=2):
            tree = spec.wrapper_object()
            print(f"[信息] 通过 tree_auto_id 命中树控件: {args.tree_auto_id}")
            return tree, ("TreeItem",)

    if args.tree_title:
        spec = window.child_window(title=args.tree_title, control_type="Tree")
        if spec.exists(timeout=2):
            tree = spec.wrapper_object()
            print(f"[信息] 通过 tree_title 命中树控件: {args.tree_title}")
            return tree, ("TreeItem",)

    trees = window.descendants(control_type="Tree")
    if trees:
        if args.tree_index < 0 or args.tree_index >= len(trees):
            raise RuntimeError(
                f"tree_index 越界: {args.tree_index}，当前找到 {len(trees)} 个 Tree 控件。"
            )
        print(f"[信息] 使用第 {args.tree_index} 个 Tree 控件。")
        return trees[args.tree_index], ("TreeItem",)

    lists = window.descendants(control_type="List")
    if lists:
        if args.tree_index < 0 or args.tree_index >= len(lists):
            raise RuntimeError(
                f"tree_index 越界: {args.tree_index}，当前找到 {len(lists)} 个 List 控件。"
            )
        print(f"[信息] 未找到 Tree 控件，改用第 {args.tree_index} 个 List 控件。")
        return lists[args.tree_index], ("ListItem",)

    raise RuntimeError("窗口内未找到 Tree/List 控件，请先用 --print-controls 查看控件结构。")


def get_children_by_types(control, child_types: tuple[str, ...]):
    children = []
    for ct in child_types:
        try:
            part = control.children(control_type=ct)
            if part:
                children.extend(part)
        except Exception:
            pass

    if children:
        return children

    raw = control.children()
    for item in raw:
        ctype = str(getattr(item.element_info, "control_type", ""))
        if ctype in child_types:
            children.append(item)
    return children


def crawl_tree(tree, child_types: tuple[str, ...], args: CrawlArgs) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    rows.append(
        {
            COL_SEQ: None,
            COL_NODE_CODE: args.source_code,
            COL_NODE_NAME: args.source_name,
            COL_PARENT_CODE: "0",
            COL_DESC: args.source_desc,
            COL_TYPE: 0,
            COL_UPDATE_TIME: args.updated_at,
        }
    )

    used_table_codes: set[str] = set()

    def walk_item(item, index_path: tuple[int, ...], parent_code: str, depth: int) -> None:
        if depth > args.max_depth:
            return

        raw_name = normalize_text(item.window_text()) or "未命名节点"
        table_cn_name, table_en_name = extract_table_cn_en(raw_name)

        try:
            item.expand()
            time.sleep(args.expand_wait_seconds)
        except Exception:
            pass

        children = []
        retry_times = max(1, args.expand_retry_times)
        for retry_idx in range(retry_times):
            children = get_children_by_types(item, child_types)
            if children:
                break
            if retry_idx < retry_times - 1:
                time.sleep(args.expand_wait_seconds)

        is_leaf = len(children) == 0
        is_table_by_bracket = args.prefer_bracket_english and bool(table_en_name)

        if is_table_by_bracket or is_leaf:
            node_code = format_table_code(
                args.table_code_prefix,
                table_en_name,
                raw_name,
                index_path,
                used_table_codes,
            )
            node_type = 2
            node_name = table_cn_name if table_en_name else raw_name
        else:
            node_code = format_dir_code(args.source_code, index_path)
            node_type = 1
            node_name = raw_name

        rows.append(
            {
                COL_SEQ: None,
                COL_NODE_CODE: node_code,
                COL_NODE_NAME: node_name,
                COL_PARENT_CODE: parent_code,
                COL_DESC: node_name,
                COL_TYPE: node_type,
                COL_UPDATE_TIME: args.updated_at,
            }
        )

        if node_type == 1:
            for child_idx, child in enumerate(children, start=1):
                walk_item(
                    item=child,
                    index_path=index_path + (child_idx,),
                    parent_code=node_code,
                    depth=depth + 1,
                )

    roots = get_children_by_types(tree, child_types)
    if not roots:
        raise RuntimeError("控件下未找到任何节点。请先手工展开根目录后重试。")

    for i, root in enumerate(roots, start=1):
        walk_item(root, (i,), args.source_code, 1)

    for i, row in enumerate(rows, start=1):
        row[COL_SEQ] = i

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def validate_result(df: pd.DataFrame, args: CrawlArgs) -> None:
    errors: list[str] = []

    if len(df) < args.min_nodes:
        errors.append(f"节点总数={len(df)}，小于最小要求 min_nodes={args.min_nodes}")

    dup_count = int(df[COL_NODE_CODE].duplicated().sum())
    if dup_count > 0:
        errors.append(f"节点编号存在重复，重复数={dup_count}")

    code_set = set(df[COL_NODE_CODE].astype(str))
    invalid_parent = df[
        (~df[COL_PARENT_CODE].astype(str).isin(code_set))
        & (df[COL_PARENT_CODE].astype(str) != "0")
    ]
    if not invalid_parent.empty:
        errors.append(f"存在父节点编号无效记录，数量={len(invalid_parent)}")

    type_counts = df[COL_TYPE].value_counts().to_dict()
    if int(type_counts.get(1, 0)) == 0:
        errors.append("未识别到目录节点（类型1）")
    if int(type_counts.get(2, 0)) == 0:
        errors.append("未识别到表节点（类型2）")

    root_rows = df[df[COL_TYPE] == 0]
    if len(root_rows) != 1:
        errors.append(f"根节点数量异常，期望1，实际={len(root_rows)}")

    prefix = normalize_text(args.table_code_prefix) or "WIND"
    bad_table_codes = df[
        (df[COL_TYPE] == 2)
        & (~df[COL_NODE_CODE].astype(str).str.startswith(f"{prefix}_"))
    ]
    if not bad_table_codes.empty:
        errors.append(f"存在不符合 {prefix}_ 前缀规则的表节点编号，数量={len(bad_table_codes)}")

    source_prefix = normalize_text(args.source_code) or "WIND"
    bad_dir_codes = df[
        (df[COL_TYPE].isin([0, 1]))
        & (~df[COL_NODE_CODE].astype(str).str.startswith(source_prefix))
    ]
    if not bad_dir_codes.empty:
        errors.append(
            f"存在不符合 {source_prefix} 前缀规则的数据源/目录节点编号，数量={len(bad_dir_codes)}"
        )

    if args.must_contain_regex:
        pattern = re.compile(args.must_contain_regex)
        matched = df[COL_NODE_NAME].astype(str).str.contains(pattern, regex=True, na=False)
        if not bool(matched.any()):
            errors.append(f"没有任何节点名称匹配 must_contain_regex={args.must_contain_regex}")

    if errors:
        joined = "\n - " + "\n - ".join(errors)
        raise RuntimeError(f"结果校验失败:{joined}")


def print_summary(df: pd.DataFrame) -> None:
    type_counts = df[COL_TYPE].value_counts().to_dict()
    print(
        "[信息] 抓取统计: "
        f"总数={len(df)}，数据源={type_counts.get(0, 0)}，目录={type_counts.get(1, 0)}，表={type_counts.get(2, 0)}"
    )


def parse_args() -> CrawlArgs:
    parser = argparse.ArgumentParser(description="Windows 客户端树形目录爬虫（短信登录后人工确认）")
    parser.add_argument(
        "--window-title-regex",
        default=None,
        help="可选。窗口标题正则，例如 .*万得.*；不填则列出所有可见窗口让你选择",
    )
    parser.add_argument(
        "--window-pick-index",
        type=int,
        default=None,
        help="可选。窗口选择序号（与控制台打印列表一致）",
    )
    parser.add_argument(
        "--exe-path",
        default=None,
        help="可选。若窗口未打开，使用该路径启动客户端 exe",
    )
    parser.add_argument("--tree-auto-id", default=None, help="可选。树控件的 AutomationId")
    parser.add_argument("--tree-title", default=None, help="可选。树控件标题")
    parser.add_argument(
        "--tree-index",
        type=int,
        default=0,
        help="当定位到多个 Tree/List 控件时，选择第几个（从0开始）",
    )
    parser.add_argument("--source-code", default="WIND", help="目录根节点编号，例如 WIND")
    parser.add_argument("--table-code-prefix", default="WIND", help="表节点编号前缀，例如 WIND")
    parser.add_argument(
        "--disable-bracket-english",
        action="store_true",
        help="关闭“中文名[英文名]优先识别为表并用于编码”的规则",
    )
    parser.add_argument("--source-name", default="客户端数据", help="根节点名称")
    parser.add_argument("--source-desc", default="客户端树形目录", help="根节点描述")
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="更新时间，默认今天，格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--output",
        default="万得数据节点目录_爬取结果.xlsx",
        help="输出 Excel 文件路径",
    )
    parser.add_argument(
        "--launch-wait-seconds",
        type=int,
        default=45,
        help="启动客户端后的最长等待秒数",
    )
    parser.add_argument(
        "--expand-wait-seconds",
        type=float,
        default=0.25,
        help="每次展开节点后的等待秒数（懒加载可适当调大）",
    )
    parser.add_argument(
        "--expand-retry-times",
        type=int,
        default=4,
        help="每个节点展开后的子节点重试次数（用于懒加载）",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=20,
        help="最大递归层级，防止异常无限递归",
    )
    parser.add_argument(
        "--print-controls",
        action="store_true",
        help="仅打印窗口控件树（用于定位 tree_auto_id / tree_title）",
    )
    parser.add_argument(
        "--controls-output",
        default="控件树全文.txt",
        help="--print-controls 时，控件树全文输出路径",
    )
    parser.add_argument(
        "--skip-login-wait",
        action="store_true",
        help="跳过“手工登录后按回车继续”步骤",
    )
    parser.add_argument(
        "--min-nodes",
        type=int,
        default=20,
        help="结果最小节点数，不满足则判定失败",
    )
    parser.add_argument(
        "--must-contain-regex",
        default=None,
        help="可选。结果中至少有一个节点名称匹配该正则，否则判定失败",
    )
    parser.add_argument(
        "--skip-validation",
        action="store_true",
        help="跳过结果校验（不建议）",
    )

    ns = parser.parse_args()
    return CrawlArgs(
        window_title_regex=ns.window_title_regex,
        window_pick_index=ns.window_pick_index,
        exe_path=ns.exe_path,
        tree_auto_id=ns.tree_auto_id,
        tree_title=ns.tree_title,
        tree_index=ns.tree_index,
        source_code=ns.source_code,
        table_code_prefix=ns.table_code_prefix,
        prefer_bracket_english=(not ns.disable_bracket_english),
        source_name=ns.source_name,
        source_desc=ns.source_desc,
        updated_at=ns.date,
        output_path=Path(ns.output),
        launch_wait_seconds=ns.launch_wait_seconds,
        expand_wait_seconds=ns.expand_wait_seconds,
        expand_retry_times=ns.expand_retry_times,
        max_depth=ns.max_depth,
        print_controls=ns.print_controls,
        controls_output=Path(ns.controls_output) if ns.controls_output else None,
        skip_login_wait=ns.skip_login_wait,
        min_nodes=ns.min_nodes,
        must_contain_regex=ns.must_contain_regex,
        skip_validation=ns.skip_validation,
    )


def main() -> None:
    args = parse_args()

    try:
        Application, Desktop = load_pywinauto()
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return

    if not args.skip_login_wait:
        print("[提示] 请先在客户端完成登录（手机号 + 短信验证码），并进入需要抓取的树形目录页面。")
        input("[提示] 准备好后按回车开始抓取... ")

    window = attach_or_start_window(args, Desktop, Application)

    if args.print_controls:
        maybe_print_controls(window, args.controls_output)
        print("[完成] 已输出控件树预览。")
        return

    tree, child_types = find_tree_control(window, args)
    node_df = crawl_tree(tree, child_types, args)

    if not args.skip_validation:
        validate_result(node_df, args)

    print_summary(node_df)
    node_df.to_excel(args.output_path, index=False, sheet_name="外部数据目录")
    print(f"[完成] 共抓取 {len(node_df)} 条节点，已输出: {args.output_path.resolve()}")


if __name__ == "__main__":
    main()



