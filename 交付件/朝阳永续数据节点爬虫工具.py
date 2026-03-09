#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import getpass
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

import pandas as pd
import requests
from requests.adapters import HTTPAdapter

BASE_URL = "https://gogoaldata.go-goal.cn"
DEFAULT_URL = "https://gogoaldata.go-goal.cn/html/Index.html?code=sm4_ddl"
DEFAULT_OUTPUT = "朝阳永续数据节点_爬取结果.xlsx"
DEFAULT_REFERENCE = "朝阳永续数据节点.xlsx"
NAME_NORMALIZER = re.compile(r"[\s\-\_()??]")
TABLE_SPACE_RE = re.compile(r"\s+")
TABLE_CODE_RE = re.compile(r"[^A-Z0-9_]+")
GUID_CLEAN_RE = re.compile(r"[^A-Za-z0-9]")

SKIP_GROUP_NAMES = {
    "聚源新版数据库",
    "恒生聚源新版数据库",
    "私募数据库",
    "朝阳永续私募数据库",
}

def normalize_name(text: str) -> str:
    return NAME_NORMALIZER.sub("", str(text or ""))


NORMALIZED_SKIP_GROUP_NAMES = {normalize_name(name) for name in SKIP_GROUP_NAMES}

@dataclass
class SourceMeta:
    seq: Optional[float]
    code: str
    name: str
    desc: str


@dataclass
class TreeNode:
    guid: str
    father_guid: Optional[str]
    menu_name: str
    display_type: int
    children: List["TreeNode"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="朝阳永续数据节点爬虫")
    parser.add_argument("--url", default=DEFAULT_URL, help="页面 URL，默认 sm4_ddl")
    parser.add_argument("--code", default=None, help="产品功能码（默认从 URL 的 code 参数解析）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出 Excel 路径")
    parser.add_argument("--reference", default=DEFAULT_REFERENCE, help="参考节点文件（用于读取根节点信息）")

    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--visible-password", action="store_true", help="可见输入密码（适合 PyCharm 运行窗口）")

    parser.add_argument("--token", default=None, help="已登录 token（可选，使用现有登录态时可填）")
    parser.add_argument("--org-id", default=None, help="组织 ID（可选，不填则登录后自动取）")
    parser.add_argument("--user-id", default=None, help="用户 ID（可选，不填则登录后自动取）")

    parser.add_argument("--source-seq", type=float, default=None, help="数据源序号")
    parser.add_argument("--source-code", default=None, help="数据源编号，默认 ZYYX")
    parser.add_argument("--source-name", default=None, help="数据源名称，默认 朝阳永续")
    parser.add_argument("--source-desc", default=None, help="数据源描述")
    parser.add_argument("--update-date", default=datetime.now().strftime("%Y-%m-%d"), help="更新时间 YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=8, help="并发抓取表详情线程数")
    return parser.parse_args()


def parse_code_from_url(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    code = query.get("code", [None])[0] or query.get("Code", [None])[0]
    if not code:
        raise ValueError(f"无法从 URL 解析 code 参数: {url}")
    return code


def first_non_empty(d: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def is_skip_group(name: str) -> bool:
    normalized = normalize_name(name)
    if normalized in NORMALIZED_SKIP_GROUP_NAMES:
        return True
    if "??" in normalized and "?????" in normalized:
        return True
    if normalized.endswith("?????"):
        return True
    return False


def load_source_meta(args: argparse.Namespace) -> SourceMeta:
def load_source_meta(args: argparse.Namespace) -> SourceMeta:
    row: Dict[str, Any] = {}
    ref_path = Path(args.reference)
    if ref_path.exists():
        try:
            df = pd.read_excel(ref_path, nrows=1)
            if not df.empty:
                row = df.iloc[0].to_dict()
        except Exception as exc:
            print(f"[WARN] 读取参考文件失败，将使用参数/默认值: {exc}", flush=True)

    seq = args.source_seq if args.source_seq is not None else row.get("序号")
    code = args.source_code or str(row.get("节点编号", "")).strip() or "ZYYX"
    if args.source_code is None and code == "CYYX":
        code = "ZYYX"
    name = args.source_name or str(row.get("节点名称", "")).strip() or "朝阳永续"
    desc = args.source_desc or str(row.get("描述", "")).strip() or "上海朝阳永续信息技术股份有限公司"
    return SourceMeta(seq=seq, code=code, name=name, desc=desc)


def read_password_interactive(preset: Optional[str] = None, visible_password: bool = False) -> str:
    if preset is not None:
        return preset
    pycharm_hosted = os.environ.get("PYCHARM_HOSTED") == "1"
    no_tty = not sys.stdin.isatty()
    if visible_password or pycharm_hosted or no_tty:
        print("账号已接收，当前终端使用可见输入密码...", flush=True)
        return input("请输入密码: ").strip()
    print("账号已接收，下一步输入密码（输入内容默认不显示）...", flush=True)
    try:
        return getpass.getpass("请输入密码（不回显）: ")
    except Exception:
        return input("请输入密码（可见输入）: ").strip()


def is_valid_username(username: str) -> bool:
    if not username:
        return False
    username = username.strip()
    if any(token in username.lower() for token in ("python.exe", ".py", ":\\", " --", "  ")):
        return False
    return re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", username) is not None


def read_username_interactive(preset: Optional[str] = None) -> str:
    if preset is not None:
        val = preset.strip()
        if not is_valid_username(val):
            raise RuntimeError("账号格式异常，请确认只输入账号本身，不要粘贴命令行。")
        return val
    while True:
        username = input("请输入账号: ").strip()
        if is_valid_username(username):
            return username
        print("[WARN] 账号格式异常，请仅输入账号（不要粘贴 python 命令或文件路径）。", flush=True)


class GoGoalClient:
    def __init__(self, pool_size: int = 32) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
                ),
                "Referer": f"{BASE_URL}/html/Index.html",
            }
        )
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0, pool_block=True)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)
        self.table_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_lock = threading.Lock()

    def bootstrap(self, url: str) -> None:
    def bootstrap(self, url: str) -> None:
        self.session.get(url, timeout=30)

    def _request(self, method: str, api_path: str, **kwargs: Any) -> requests.Response:
        url = api_path if api_path.startswith("http") else f"{BASE_URL}/api/{api_path.lstrip('/')}"
        timeout = kwargs.pop("timeout", (8, 25))
        resp = self.session.request(method, url, timeout=timeout, **kwargs)
        resp.raise_for_status()
        return resp

    @staticmethod
    @staticmethod
    def _parse_json(resp: requests.Response) -> Dict[str, Any]:
        try:
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"接口返回非 JSON: HTTP {resp.status_code}, {exc}, body={resp.text[:200]}")
        if not isinstance(data, dict):
            raise RuntimeError(f"接口返回格式异常（非对象）: {data}")
        return data

    def login(self, username: str, password: str, application: int = 74) -> Tuple[str, str, Optional[str], Dict[str, Any]]:
        payload = {"login_name": username, "password": password, "application": str(application)}
        resp = self._request("POST", "gguser_login/by_password", data=payload)
        top = self._parse_json(resp)
        if int(top.get("code", -1)) != 0:
            raise RuntimeError(f"登录失败: {top.get('message') or top}")

        inner = top.get("data")
        if not isinstance(inner, dict):
            raise RuntimeError(f"登录返回异常: {top}")

        inner_code = int(inner.get("code", -1)) if inner.get("code") is not None else -1
        if inner_code != 0:
            msg = inner.get("message") or "账号密码不匹配或无权限"
            raise RuntimeError(f"登录失败: {msg} (code={inner_code})")

        user_id = str(inner.get("account_id") or "").strip()
        org_id = str(inner.get("org_id") or "").strip()
        token = str(inner.get("token") or "").strip() or None
        if not user_id or not org_id:
            raise RuntimeError(f"登录成功但未返回 account_id/org_id: {inner}")

        # 模拟页面登录后的 cookie 状态，兼容后续接口校验
        self.session.cookies.set("AccountID", user_id, domain="gogoaldata.go-goal.cn", path="/")
        self.session.cookies.set("OrgID", org_id, domain="gogoaldata.go-goal.cn", path="/")
        self.session.cookies.set("name", str(inner.get("account_name") or username), domain="gogoaldata.go-goal.cn", path="/")
        if token:
            self.session.cookies.set("tk", token, domain="gogoaldata.go-goal.cn", path="/")

        return org_id, user_id, token, inner

    def query_permission(self, function_code: str, token: Optional[str]) -> None:
        params: Dict[str, str] = {"function_code": function_code}
        if token:
            params["token"] = token
        resp = self._request("GET", "v1/dd_data/query_permi_by_token", params=params)
        data = self._parse_json(resp)
        if int(data.get("code", -1)) != 0:
            raise RuntimeError(f"权限校验失败: {data.get('message') or data}")
        detail = data.get("data")
        if isinstance(detail, dict):
            is_valite = detail.get("is_valite")
            if str(is_valite) not in {"1", "True", "true"}:
                raise RuntimeError(f"当前账号无该产品权限（function_code={function_code}）")

    def get_header_tree(self, function_code: str, org_id: str, user_id: str, table_type: int = 0) -> List[Dict[str, Any]]:
        payload: Dict[str, str] = {
            "table_type": str(table_type),
            "org_id": str(org_id),
            "function_code": function_code,
            "user_id": str(user_id),
        }
        resp = self._request("POST", "v1/dd_data/get_header", data=payload)
        top = self._parse_json(resp)
        if int(top.get("code", -1)) != 0:
            raise RuntimeError(f"获取树失败: {top.get('message') or top}")
        data = top.get("data")
        if not isinstance(data, list) or len(data) == 0:
            raise RuntimeError(
                "获取到空树数据，请检查账号权限、org_id/user_id 与 code 是否匹配。"
            )
        return data

    def get_table_struct(self, guid: str, table_type: int = 0) -> Dict[str, Any]:
        guid = str(guid)
        cached = self.table_cache.get(guid)
        if cached is not None:
            return cached
        payload = {"table_type": str(table_type), "guid": guid}
        resp = self._request("POST", "v1/dd_data/get_table_struct", data=payload)
        top = self._parse_json(resp)
        if int(top.get("code", -1)) != 0:
            raise RuntimeError(f"获取表结构失败 guid={guid}: {top.get('message') or top}")
        data = top.get("data")
        if not isinstance(data, dict):
            data = {}
        with self._cache_lock:
            self.table_cache[guid] = data
        return data


def build_tree_nodes(raw_nodes: List[Dict[str, Any]]) -> List[TreeNode]:
    node_map: Dict[str, TreeNode] = {}
    order: List[str] = []

    for item in raw_nodes:
        guid = str(item.get("guid") or "").strip()
        if not guid:
            continue
        father_guid = item.get("father_guid")
        father_guid = str(father_guid).strip() if father_guid is not None else None
        menu_name = first_non_empty(item, ["menu_name", "name"], default="未命名节点")
        try:
            display_type = int(item.get("display_type") or 0)
        except Exception:
            display_type = 0
        node_map[guid] = TreeNode(
            guid=guid,
            father_guid=father_guid if father_guid else None,
            menu_name=menu_name,
            display_type=display_type,
            children=[],
        )
        order.append(guid)

    roots: List[TreeNode] = []
    for guid in order:
        node = node_map[guid]
        if node.father_guid and node.father_guid in node_map:
            node_map[node.father_guid].children.append(node)
        else:
            roots.append(node)
    return roots


def collect_leaf_guids(nodes: List[TreeNode]) -> List[str]:
    bucket: List[str] = []
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        if is_skip_group(node.menu_name):
            if node.children:
                stack.extend(reversed(node.children))
            continue
        if node.children:
            stack.extend(reversed(node.children))
            continue
        bucket.append(node.guid)
    return bucket


def preload_tables(client: GoGoalClient, guids: List[str], workers: int = 8) -> None:
def preload_tables(client: GoGoalClient, guids: List[str], workers: int = 8) -> None:
    unique_guids = list(dict.fromkeys(guids))
    if not unique_guids:
        print("[进度] 无表详情需要预加载。", flush=True)
        return
    workers = min(max(1, workers), len(unique_guids))
    total = len(unique_guids)
    done = 0
    failed = 0
    start_ts = time.time()
    report_every = max(1, total // 20)
    print(f"[进度] 开始预加载表详情: {total} 张表, 并发={workers}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_guid = {executor.submit(client.get_table_struct, guid, 0): guid for guid in unique_guids}
        for future in as_completed(future_to_guid):
            guid = future_to_guid[future]
            try:
                future.result()
            except Exception as exc:
                failed += 1
                print(f"[WARN] 预加载表详情失败 guid={guid}: {exc}", flush=True)
            finally:
                done += 1
                if done % report_every == 0 or done == total:
                    elapsed = time.time() - start_ts
                    speed = done / elapsed if elapsed > 0 else 0
                    print(
                        f"[进度] 表详情 {done}/{total} ({done / total:.0%}), 失败={failed}, 耗时={elapsed:.1f}s, 速率={speed:.1f}表/s",
                        flush=True,
                    )
    print(f"[进度] 表详情预加载完成: 成功={total - failed}, 失败={failed}", flush=True)


def extract_table_name_en(table_data: Dict[str, Any]) -> str:
    desc = table_data.get("table_describle")
    info: Dict[str, Any] = {}
    if isinstance(desc, list) and desc:
        if isinstance(desc[0], dict):
            info = desc[0]
    elif isinstance(desc, dict):
        info = desc
    return first_non_empty(
        info,
        ["table_name", "table_name_en", "name", "table_english_name"],
        default="",
    )


def build_rows(client: GoGoalClient, roots: List[TreeNode], source: SourceMeta, update_date: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    rows.append(
        {
            "序号": source.seq,
            "节点编号": source.code,
            "节点名称": source.name,
            "父节点编号": "0",
            "描述": source.desc,
            "类型（0-数据源，1-目录，2-表）": 0,
            "更新时间": update_date,
        }
    )

    def table_code(guid: str, fallback_name: str) -> str:
        table_data = client.table_cache.get(guid) or {}
        table_name_en = extract_table_name_en(table_data)
        if not table_name_en:
            table_name_en = fallback_name
        cleaned = TABLE_SPACE_RE.sub("", str(table_name_en)).upper()
        cleaned = TABLE_CODE_RE.sub("_", cleaned).strip("_")
        if not cleaned:
            cleaned = f"TABLE_{GUID_CLEAN_RE.sub('', guid)[:16] or 'UNKNOWN'}"
        return f"{source.code}_{cleaned}"

    def walk(nodes: List[TreeNode], parent_code: str) -> None:
    def walk(nodes: List[TreeNode], parent_code: str) -> None:
        group_index = 0

        def process_node(node: TreeNode, current_parent_code: str, current_group_index: int) -> int:
            # 同旧脚本要求：跳过“聚源新版数据库”这一层，其子节点上移一级参与编号
            if is_skip_group(node.menu_name):
                for child in node.children:
                    current_group_index = process_node(child, current_parent_code, current_group_index)
                return current_group_index

            if node.children:
                current_group_index += 1
                current_code = f"{current_parent_code}{current_group_index:02d}"
                rows.append(
                    {
                        "序号": None,
                        "节点编号": current_code,
                        "节点名称": node.menu_name,
                        "父节点编号": current_parent_code,
                        "描述": node.menu_name,
                        "类型（0-数据源，1-目录，2-表）": 1,
                        "更新时间": update_date,
                    }
                )
                walk(node.children, current_code)
                return current_group_index

            rows.append(
                {
                    "序号": None,
                    "节点编号": table_code(node.guid, node.menu_name),
                    "节点名称": node.menu_name,
                    "父节点编号": current_parent_code,
                    "描述": node.menu_name,
                    "类型（0-数据源，1-目录，2-表）": 2,
                    "更新时间": update_date,
                }
            )
            return current_group_index

        for n in nodes:
            group_index = process_node(n, parent_code, group_index)

    walk(roots, source.code)
    return rows


def main() -> int:
    args = parse_args()
    function_code = args.code or parse_code_from_url(args.url)
    source_meta = load_source_meta(args)
    output_path = Path(args.output).resolve()
    print(f"[进度] 任务启动: code={function_code}, 输出={output_path}", flush=True)

    client = GoGoalClient()
    print("[进度] 初始化站点会话...", flush=True)
    client.bootstrap(args.url)

    org_id = str(args.org_id).strip() if args.org_id is not None else ""
    user_id = str(args.user_id).strip() if args.user_id is not None else ""
    token = str(args.token).strip() if args.token else None
    if token:
        client.session.cookies.set("tk", token, domain="gogoaldata.go-goal.cn", path="/")

    if not org_id or not user_id:
        username = read_username_interactive(args.username)
        password = read_password_interactive(args.password, visible_password=args.visible_password)
        if not password:
            raise RuntimeError("密码不能为空。")
        print("[进度] 登录中...", flush=True)
        org_id, user_id, login_token, _ = client.login(username, password, application=74)
        token = token or login_token
        print(f"[进度] 登录成功: org_id={org_id}, user_id={user_id}", flush=True)
    else:
        print(f"[进度] 使用传入身份参数: org_id={org_id}, user_id={user_id}", flush=True)

    # 权限校验：若 token 可用则校验；无 token 则跳过（部分环境依赖 cookie 会话）
    if token:
        print("[进度] 校验产品权限...", flush=True)
        client.query_permission(function_code, token)
        print("[进度] 权限校验通过。", flush=True)
    else:
        print("[WARN] 未提供 token，跳过权限校验，直接请求树接口。", flush=True)

    print("[进度] 抓取目录树...", flush=True)
    raw_tree = client.get_header_tree(function_code=function_code, org_id=org_id, user_id=user_id, table_type=0)
    print(f"[进度] 目录树抓取完成，节点总数={len(raw_tree)}", flush=True)

    roots = build_tree_nodes(raw_tree)
    print(f"[进度] 树根节点数={len(roots)}", flush=True)

    leaf_guids = collect_leaf_guids(roots)
    print(f"[进度] 识别到叶子节点 {len(set(leaf_guids))} 个，开始预加载表详情...", flush=True)
    preload_tables(client, leaf_guids, workers=args.workers)

    print("[进度] 生成节点清单与编号...", flush=True)
    rows = build_rows(client, roots, source_meta, args.update_date)
    print(f"[进度] 节点清单生成完成，共 {len(rows)} 行。", flush=True)

    root_dirs = [
        r for r in rows
        if r["类型（0-数据源，1-目录，2-表）"] == 1 and r["父节点编号"] == source_meta.code
    ]
    if root_dirs:
        preview = "，".join([f'{r["节点编号"]}:{r["节点名称"]}' for r in root_dirs[:8]])
        print(f"[进度] 一级目录预览: {preview}", flush=True)

    df = pd.DataFrame(
        rows,
        columns=["序号", "节点编号", "节点名称", "父节点编号", "描述", "类型（0-数据源，1-目录，2-表）", "更新时间"],
    )
    print("[进度] 写出 Excel...", flush=True)
    try:
        with pd.ExcelWriter(output_path, engine="xlsxwriter") as writer:
            df.to_excel(writer, index=False)
    except (ModuleNotFoundError, ImportError, ValueError):
        df.to_excel(output_path, index=False)
    print(f"完成：共输出 {len(df)} 行 -> {output_path}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\n已取消。")
        raise SystemExit(130)
    except Exception as exc:
        print(f"[ERROR] {exc}")
        raise SystemExit(1)
