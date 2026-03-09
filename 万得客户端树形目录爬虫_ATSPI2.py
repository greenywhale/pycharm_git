from __future__ import annotations

import argparse
import re
import time
from collections import deque
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
    tree_name_regex: str | None
    tree_index: int
    source_code: str
    source_name: str
    source_desc: str
    updated_at: str
    output_path: Path
    controls_output: Path
    expand_wait_seconds: float
    max_depth: int
    max_walk_nodes: int
    print_controls: bool
    skip_login_wait: bool


def normalize_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def sanitize_for_code(name: str) -> str:
    text = normalize_text(name).upper()
    text = re.sub(r"[^A-Z0-9]+", "_", text)
    return text.strip("_")


def extract_cn_en(text: str) -> tuple[str, str | None]:
    src = normalize_text(text)
    m = re.match(r"^(?P<cn>.*?)\s*[\[【](?P<en>[A-Za-z0-9_]+)[\]】]\s*$", src)
    if not m:
        return src, None
    cn = normalize_text(m.group("cn")) or src
    en = normalize_text(m.group("en")) or None
    return cn, en


def format_dir_code(source_code: str, index_path: tuple[int, ...]) -> str:
    suffix = "".join(f"{i:02d}" for i in index_path)
    return f"{source_code}{suffix}"


def format_table_code(
    source_code: str,
    node_name: str,
    table_en: str | None,
    index_path: tuple[int, ...],
    used_codes: set[str],
) -> str:
    base = sanitize_for_code(table_en if table_en else node_name)
    if base:
        code = f"{source_code}_{base}"
    else:
        code = f"{source_code}_NODE{''.join(f'{i:02d}' for i in index_path)}"

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


def load_atspi():
    try:
        import pyatspi
    except ImportError as exc:
        raise RuntimeError(
            "缺少依赖 pyatspi。银河麒麟请安装系统包，例如：\n"
            "  sudo apt install python3-pyatspi\n"
            "如果是 dnf/yum 系发行版，可尝试：\n"
            "  sudo dnf install python3-pyatspi"
        ) from exc
    return pyatspi


def child_count(node) -> int:
    try:
        return int(getattr(node, "childCount", 0))
    except Exception:
        return 0


def get_children(node):
    result = []
    n = child_count(node)
    for i in range(n):
        try:
            ch = node.getChildAtIndex(i)
            if ch is not None:
                result.append(ch)
        except Exception:
            continue
    return result


def get_role_id(node):
    try:
        return node.getRole()
    except Exception:
        return None


def get_role_name(node) -> str:
    try:
        return normalize_text(node.getRoleName())
    except Exception:
        return ""


def role_set(pyatspi, names: list[str]) -> set[int]:
    out: set[int] = set()
    for n in names:
        v = getattr(pyatspi, n, None)
        if v is not None:
            out.add(v)
    return out


def list_candidate_windows(desktop, pyatspi, title_regex: str | None):
    win_roles = role_set(
        pyatspi,
        [
            "ROLE_FRAME",
            "ROLE_WINDOW",
            "ROLE_DIALOG",
            "ROLE_FILE_CHOOSER",
        ],
    )

    candidates = []
    app_count = child_count(desktop)
    for ai in range(app_count):
        try:
            app = desktop.getChildAtIndex(ai)
        except Exception:
            continue

        app_name = normalize_text(getattr(app, "name", ""))
        for w in get_children(app):
            role_id = get_role_id(w)
            role_name = get_role_name(w)
            title = normalize_text(getattr(w, "name", ""))
            if not title:
                continue

            if win_roles and role_id not in win_roles:
                if role_name.lower() not in {"frame", "window", "dialog"}:
                    continue

            if title_regex and not re.search(title_regex, title, flags=re.IGNORECASE):
                continue

            candidates.append((app_name, w))

    return candidates


def choose_window(candidates: list[tuple[str, object]], pick_index: int | None):
    if not candidates:
        raise RuntimeError("未找到可见窗口，请先打开并登录客户端。")

    print("[信息] 可选窗口如下：")
    for i, (app_name, w) in enumerate(candidates):
        title = normalize_text(getattr(w, "name", ""))
        role_name = get_role_name(w)
        print(f"  [{i}] 应用={app_name} 标题={title} 角色={role_name}")

    if pick_index is not None:
        idx = pick_index
    else:
        raw = input("请输入窗口序号（默认0）: ").strip()
        idx = int(raw) if raw else 0

    if idx < 0 or idx >= len(candidates):
        raise RuntimeError(f"窗口序号越界: {idx}，可选范围 0~{len(candidates)-1}")

    return candidates[idx]


def grab_focus(node) -> None:
    try:
        comp = node.queryComponent()
        comp.grabFocus()
    except Exception:
        pass


def try_action(node, keywords: list[str]) -> bool:
    try:
        act = node.queryAction()
    except Exception:
        return False

    names = []
    for i in range(act.nActions):
        try:
            names.append((i, normalize_text(act.getName(i)).lower()))
        except Exception:
            continue

    for i, name in names:
        if any(k in name for k in keywords):
            try:
                return bool(act.doAction(i))
            except Exception:
                continue

    return False


def expand_item_if_possible(item, pyatspi, wait_sec: float) -> None:
    expanded = getattr(pyatspi, "STATE_EXPANDED", None)
    expandable = getattr(pyatspi, "STATE_EXPANDABLE", None)

    can_expand = True
    is_expanded = False
    try:
        st = item.getState()
        if expandable is not None:
            can_expand = bool(st.contains(expandable))
        if expanded is not None:
            is_expanded = bool(st.contains(expanded))
    except Exception:
        pass

    if is_expanded or not can_expand:
        return

    ok = try_action(item, ["expand", "open", "toggle", "press", "click"])
    if not ok:
        try_action(item, ["activate"])

    if wait_sec > 0:
        time.sleep(wait_sec)


def is_tree_container(node, pyatspi) -> bool:
    role_id = get_role_id(node)
    role_name = get_role_name(node).lower()

    tree_roles = role_set(
        pyatspi,
        [
            "ROLE_TREE",
            "ROLE_TREE_TABLE",
            "ROLE_LIST",
            "ROLE_OUTLINE",
        ],
    )

    if tree_roles and role_id in tree_roles:
        return True

    return role_name in {"tree", "tree table", "list", "outline"}


def is_tree_item(node, pyatspi) -> bool:
    role_id = get_role_id(node)
    role_name = get_role_name(node).lower()

    item_roles = role_set(
        pyatspi,
        [
            "ROLE_TREE_ITEM",
            "ROLE_LIST_ITEM",
            "ROLE_TABLE_CELL",
            "ROLE_ROW_HEADER",
            "ROLE_COLUMN_HEADER",
            "ROLE_MENU_ITEM",
        ],
    )

    if item_roles and role_id in item_roles:
        return True

    if "tree item" in role_name or "list item" in role_name:
        return True

    return False


def dump_accessibility_tree(root, max_nodes: int = 5000) -> str:
    lines: list[str] = []
    stack = [(root, 0)]
    seen = 0

    while stack and seen < max_nodes:
        node, depth = stack.pop()
        seen += 1

        role_name = get_role_name(node)
        name = normalize_text(getattr(node, "name", ""))
        desc = normalize_text(getattr(node, "description", ""))
        cnt = child_count(node)

        indent = "  " * depth
        line = f"{indent}- role={role_name} | name={name} | desc={desc} | children={cnt}"
        lines.append(line)

        children = get_children(node)
        for ch in reversed(children):
            stack.append((ch, depth + 1))

    if stack:
        lines.append(f"...（已截断，超过 max_nodes={max_nodes}）")

    return "\n".join(lines)


def find_tree_control(window, pyatspi, args: CrawlArgs):
    matches = []
    queue = deque([window])
    scanned = 0

    while queue and scanned < args.max_walk_nodes:
        node = queue.popleft()
        scanned += 1

        if is_tree_container(node, pyatspi):
            name = normalize_text(getattr(node, "name", ""))
            if args.tree_name_regex:
                if re.search(args.tree_name_regex, name, flags=re.IGNORECASE):
                    matches.append(node)
            else:
                matches.append(node)

        for ch in get_children(node):
            queue.append(ch)

    if not matches:
        raise RuntimeError("未找到 Tree/List 类控件。建议先用 --print-controls 检查可访问结构。")

    if args.tree_index < 0 or args.tree_index >= len(matches):
        raise RuntimeError(
            f"tree-index 越界: {args.tree_index}，当前匹配到 {len(matches)} 个树控件。"
        )

    target = matches[args.tree_index]
    title = normalize_text(getattr(target, "name", ""))
    print(f"[信息] 选中树控件: index={args.tree_index}, name={title}")
    return target


def get_item_children(node, pyatspi):
    items = [ch for ch in get_children(node) if is_tree_item(ch, pyatspi)]
    if items:
        return items

    # 某些客户端会多包一层容器
    for ch in get_children(node):
        nested = [g for g in get_children(ch) if is_tree_item(g, pyatspi)]
        if nested:
            return nested

    return []


def crawl_tree(tree, pyatspi, args: CrawlArgs) -> pd.DataFrame:
    rows: list[dict[str, object]] = [
        {
            COL_SEQ: None,
            COL_NODE_CODE: args.source_code,
            COL_NODE_NAME: args.source_name,
            COL_PARENT_CODE: "0",
            COL_DESC: args.source_desc,
            COL_TYPE: 0,
            COL_UPDATE_TIME: args.updated_at,
        }
    ]

    used_table_codes: set[str] = set()
    visited_objects: set[int] = set()

    def walk_item(item, index_path: tuple[int, ...], parent_code: str, depth: int):
        if depth > args.max_depth:
            return

        oid = id(item)
        if oid in visited_objects:
            return
        visited_objects.add(oid)

        raw_name = normalize_text(getattr(item, "name", ""))
        if not raw_name:
            raw_name = normalize_text(getattr(item, "description", "")) or "未命名节点"

        cn_name, en_name = extract_cn_en(raw_name)

        expand_item_if_possible(item, pyatspi, args.expand_wait_seconds)
        children = get_item_children(item, pyatspi)
        is_leaf = len(children) == 0

        if is_leaf:
            node_code = format_table_code(
                source_code=args.source_code,
                node_name=cn_name,
                table_en=en_name,
                index_path=index_path,
                used_codes=used_table_codes,
            )
            node_type = 2
        else:
            node_code = format_dir_code(args.source_code, index_path)
            node_type = 1

        rows.append(
            {
                COL_SEQ: None,
                COL_NODE_CODE: node_code,
                COL_NODE_NAME: cn_name,
                COL_PARENT_CODE: parent_code,
                COL_DESC: cn_name,
                COL_TYPE: node_type,
                COL_UPDATE_TIME: args.updated_at,
            }
        )

        for child_idx, child in enumerate(children, start=1):
            walk_item(
                item=child,
                index_path=index_path + (child_idx,),
                parent_code=node_code,
                depth=depth + 1,
            )

    roots = get_item_children(tree, pyatspi)
    if not roots:
        raise RuntimeError("树控件下未发现可遍历的节点项。可能该控件未暴露给 AT-SPI。")

    for i, root in enumerate(roots, start=1):
        walk_item(root, (i,), args.source_code, 1)

    for i, row in enumerate(rows, start=1):
        row[COL_SEQ] = i

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def parse_args() -> CrawlArgs:
    parser = argparse.ArgumentParser(
        description="万得客户端树形目录爬虫（Linux AT-SPI2 版）"
    )
    parser.add_argument(
        "--window-title-regex",
        default=None,
        help="可选。窗口标题正则，例如 .*万得.*；不填则列出所有窗口",
    )
    parser.add_argument(
        "--window-pick-index",
        type=int,
        default=None,
        help="可选。窗口序号，不填则运行时输入",
    )
    parser.add_argument(
        "--tree-name-regex",
        default=None,
        help="可选。树控件名称正则，不填则按 role 自动匹配",
    )
    parser.add_argument(
        "--tree-index",
        type=int,
        default=0,
        help="匹配到多个树控件时选第几个（从0开始）",
    )
    parser.add_argument("--source-code", default="WIND", help="根节点编码前缀")
    parser.add_argument("--source-name", default="万得数据", help="根节点名称")
    parser.add_argument(
        "--source-desc",
        default="南京万得资讯科技有限公司",
        help="根节点描述",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="更新时间，格式 YYYY-MM-DD",
    )
    parser.add_argument(
        "--output",
        default="万得数据节点目录_爬取结果_ATSPI2.xlsx",
        help="输出 Excel 文件",
    )
    parser.add_argument(
        "--controls-output",
        default="控件树全文_ATSPI2.txt",
        help="--print-controls 时输出文件",
    )
    parser.add_argument(
        "--expand-wait-seconds",
        type=float,
        default=0.25,
        help="每次展开节点后等待秒数",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=20,
        help="最大递归层级",
    )
    parser.add_argument(
        "--max-walk-nodes",
        type=int,
        default=30000,
        help="控件树扫描的最大节点数，防止死循环",
    )
    parser.add_argument(
        "--print-controls",
        action="store_true",
        help="仅打印并导出可访问控件树，不执行抓取",
    )
    parser.add_argument(
        "--skip-login-wait",
        action="store_true",
        help="跳过登录等待提示",
    )

    ns = parser.parse_args()
    return CrawlArgs(
        window_title_regex=ns.window_title_regex,
        window_pick_index=ns.window_pick_index,
        tree_name_regex=ns.tree_name_regex,
        tree_index=ns.tree_index,
        source_code=ns.source_code,
        source_name=ns.source_name,
        source_desc=ns.source_desc,
        updated_at=ns.date,
        output_path=Path(ns.output),
        controls_output=Path(ns.controls_output),
        expand_wait_seconds=ns.expand_wait_seconds,
        max_depth=ns.max_depth,
        max_walk_nodes=ns.max_walk_nodes,
        print_controls=ns.print_controls,
        skip_login_wait=ns.skip_login_wait,
    )


def main() -> None:
    args = parse_args()

    if not args.skip_login_wait:
        print("[提示] 请先打开并登录万得客户端，进入树形目录页面后继续。")
        input("[提示] 准备好后按回车开始... ")

    try:
        pyatspi = load_atspi()
    except RuntimeError as exc:
        print(f"[错误] {exc}")
        return

    desktop = pyatspi.Registry.getDesktop(0)
    candidates = list_candidate_windows(desktop, pyatspi, args.window_title_regex)
    app_name, window = choose_window(candidates, args.window_pick_index)

    print(f"[信息] 已选择窗口: 应用={app_name}, 标题={normalize_text(getattr(window, 'name', ''))}")
    grab_focus(window)

    if args.print_controls:
        text = dump_accessibility_tree(window, max_nodes=args.max_walk_nodes)
        args.controls_output.write_text(text, encoding="utf-8")
        print(f"[完成] 控件树已导出: {args.controls_output.resolve()}")
        return

    tree = find_tree_control(window, pyatspi, args)
    df = crawl_tree(tree, pyatspi, args)
    df.to_excel(args.output_path, index=False)
    print(f"[完成] 共抓取 {len(df)} 条节点，已输出: {args.output_path.resolve()}")


if __name__ == "__main__":
    main()
