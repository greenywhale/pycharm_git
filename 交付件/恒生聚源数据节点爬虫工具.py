#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import getpass
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd
import requests

BASE_URL = "https://dd.gildata.com"
DEFAULT_URL = "https://dd.gildata.com/#/tableShow/286/column///"
DEFAULT_OUTPUT = "恒生聚源数据节点_爬取结果1.xlsx"
CAPTCHA_FILE = "dd_captcha.jpg"
DEFAULT_EXPECTED_LIBRARIES = "聚源新版数据库,创新产品数据库,新三板数据库,聚源公司金融信息库"


@dataclass
class SourceMeta:
    seq: Optional[float]
    code: str
    name: str
    desc: str


@dataclass
class LibraryMeta:
    cp_id: int
    name: str
    table_id: Optional[int] = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="聚源数据字典节点爬虫")
    parser.add_argument("--url", default=DEFAULT_URL, help="tableShow 页面 URL")
    parser.add_argument("--table-id", type=int, default=None, help="表 ID（可不填，将从 URL 解析）")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help="输出 Excel 路径")
    parser.add_argument("--reference", default="恒生聚源数据节点.xlsx", help="参考节点文件（用于读取根节点信息）")

    parser.add_argument("--username", default=None, help="登录用户名")
    parser.add_argument("--password", default=None, help="登录密码")
    parser.add_argument("--captcha", default=None, help="验证码")
    parser.add_argument("--paa", type=int, default=2, help="登录 j_paa 参数，默认 2")
    parser.add_argument("--remember-me", action="store_true", help="是否记住登录")
    parser.add_argument("--visible-password", action="store_true", help="密码使用可见输入（适合 PyCharm 运行窗口）")

    parser.add_argument("--source-seq", type=float, default=None, help="数据源序号（默认从参考文件首行读取）")
    parser.add_argument("--source-code", default=None, help="数据源编号（默认从参考文件首行读取）")
    parser.add_argument("--source-name", default=None, help="数据源名称（默认从参考文件首行读取）")
    parser.add_argument("--source-desc", default=None, help="数据源描述（默认从参考文件首行读取）")
    parser.add_argument("--update-date", default=datetime.now().strftime("%Y-%m-%d"), help="更新时间，格式 YYYY-MM-DD")
    parser.add_argument("--workers", type=int, default=8, help="并发获取表详情线程数")
    parser.add_argument("--cp-probe-max", type=int, default=80, help="多库识别失败时，探测 cpId 最大值（0 表示不探测）")
    return parser.parse_args()


def parse_table_id(url: str) -> int:
    match = re.search(r"/tableShow/(\d+)", url)
    if not match:
        raise ValueError(f"无法从 URL 解析 tableId: {url}")
    return int(match.group(1))


def can_interactive_input() -> bool:
    return os.environ.get("PYCHARM_HOSTED") == "1" or sys.stdin.isatty()


def first_non_empty(d: Dict[str, Any], keys: Iterable[str], default: str = "") -> str:
    for key in keys:
        value = d.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return default


def to_non_negative_int(value: Any) -> Optional[int]:
    try:
        n = int(value)
    except Exception:
        return None
    if n < 0:
        return None
    return n


def normalize_text(text: str) -> str:
    return re.sub(r"[\s\-\_()（）]", "", str(text or ""))


def parse_expected_libraries(raw: str) -> List[str]:
    text = str(raw or "").strip()
    if not text:
        return []
    parts = re.split(r"[,，;；\n]+", text)
    seen: set = set()
    names: List[str] = []
    for part in parts:
        name = part.strip()
        if not name:
            continue
        key = normalize_text(name)
        if key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def strip_library_suffix(name: str) -> str:
    normalized = normalize_text(name)
    for suffix in ("数据库", "信息库", "数据仓库", "库"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)]
    return normalized


def match_expected_library_name(actual_name: str, expected_keys: Dict[str, str]) -> Optional[str]:
    actual_key = normalize_text(actual_name)
    if not actual_key:
        return None
    if actual_key in expected_keys:
        return expected_keys[actual_key]

    actual_core = strip_library_suffix(actual_key)
    candidates: List[str] = []
    for exp_key, exp_name in expected_keys.items():
        exp_core = strip_library_suffix(exp_key)
        if not exp_core:
            continue
        if actual_core == exp_core:
            candidates.append(exp_name)
            continue
        if actual_core and exp_core and (actual_core in exp_core or exp_core in actual_core):
            candidates.append(exp_name)
    if len(candidates) == 1:
        return candidates[0]
    return None


def iter_dict_objects(value: Any) -> Iterable[Dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for v in value.values():
            yield from iter_dict_objects(v)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dict_objects(item)


def node_display_name(node: Dict[str, Any]) -> str:
    return first_non_empty(node, ["groupName", "tableChiName", "chineseName", "name", "title"], default="未命名节点")


def is_group_node(node: Dict[str, Any]) -> bool:
    istable = node.get("istable")
    if isinstance(istable, bool):
        return not istable
    if isinstance(istable, (int, float)):
        return int(istable) == 0
    if isinstance(istable, str):
        return istable.strip() in {"0", "false", "False", ""}
    # 兜底：如果有子节点则按目录处理
    return bool(node.get("nodes"))


def load_source_meta(args: argparse.Namespace) -> SourceMeta:
    ref_path = Path(args.reference)
    row = {}
    if ref_path.exists():
        try:
            df = pd.read_excel(ref_path)
            if not df.empty:
                row = df.iloc[0].to_dict()
        except Exception as exc:
            print(f"[WARN] 读取参考文件失败，将使用参数/默认值: {exc}")

    seq = args.source_seq if args.source_seq is not None else row.get("序号")
    code = args.source_code or str(row.get("节点编号", "")).strip() or "JYZX"
    name = args.source_name or str(row.get("节点名称", "")).strip() or "恒生聚源"
    desc = args.source_desc or str(row.get("描述", "")).strip() or "上海恒生聚源数据服务有限公司"
    return SourceMeta(seq=seq, code=code, name=name, desc=desc)


def read_password_interactive(preset: Optional[str] = None, visible_password: bool = False) -> str:
    if preset is not None:
        return preset

    pycharm_hosted = os.environ.get("PYCHARM_HOSTED") == "1"
    no_tty = not can_interactive_input()

    if visible_password or pycharm_hosted or no_tty:
        print("账号已接收，当前终端使用可见输入密码...", flush=True)
        try:
            return input("请输入密码: ").strip()
        except EOFError as exc:
            raise RuntimeError("当前运行环境无法交互输入密码，请在运行参数里传 --password。") from exc

    print("账号已接收，下一步输入密码（输入内容默认不显示）...", flush=True)
    try:
        password = getpass.getpass("请输入密码（不回显）: ")
        if password:
            return password
    except (EOFError, KeyboardInterrupt):
        raise
    except Exception as exc:
        print(f"[WARN] 当前终端不支持隐藏输入，改为可见输入: {exc}", flush=True)

    return input("请输入密码（可见输入）: ").strip()


def is_valid_username(username: str) -> bool:
    if not username:
        return False
    username = username.strip()
    # 防止把整段命令/路径误贴到账号输入框
    if any(token in username.lower() for token in ("python.exe", ".py", ":\\", " --", "  ")):
        return False
    # 常见账号规则：字母数字下划线/点/横线/@，长度 3~64
    return re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", username) is not None


def read_username_interactive(preset: Optional[str] = None) -> str:
    if preset is not None:
        val = preset.strip()
        if not is_valid_username(val):
            raise RuntimeError("账号格式异常，请确认只输入账号本身，不要粘贴命令行。")
        return val

    while True:
        print("[进度] 等待输入账号...", flush=True)
        try:
            username = input("请输入账号: ").strip()
        except EOFError as exc:
            raise RuntimeError("当前运行环境无法交互输入账号，请在运行参数里传 --username。") from exc
        if is_valid_username(username):
            return username
        print("[WARN] 账号格式异常，请仅输入账号（不要粘贴 python 命令或文件路径）。")


class DDClient:
    def __init__(self) -> None:
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update(
            {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            }
        )
        self.table_cache: Dict[int, Dict[str, Any]] = {}

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = path if path.startswith("http") else f"{BASE_URL}/{path.lstrip('/')}"
        resp = self.session.request(method, url, timeout=30, **kwargs)
        return resp

    def _set_csrf_header(self) -> None:
        token = self.session.cookies.get("CSRF-TOKEN")
        if token:
            self.session.headers["X-CSRF-TOKEN"] = token

    def bootstrap(self) -> None:
        self._request("GET", "/")
        self._set_csrf_header()

    def download_captcha(self, output_file: str = CAPTCHA_FILE) -> Path:
        stamp = int(time.time() * 1000)
        resp = self._request("GET", f"/api/captcha?time={stamp}")
        resp.raise_for_status()
        path = Path(output_file).resolve()
        path.write_bytes(resp.content)
        return path

    def login(self, username: str, password: str, captcha: str, paa: int = 2, remember_me: bool = False) -> None:
        data = {
            "j_username": username,
            "j_password": password,
            "j_captcha": captcha,
            "j_paa": str(paa),
            "remember-me": str(bool(remember_me)).lower(),
            "submit": "Login",
        }
        self._set_csrf_header()
        resp = self._request(
            "POST",
            "/api/authentication",
            data=data,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

        if resp.status_code >= 400:
            if resp.status_code == 500:
                # 部分场景服务端虽然返回 500，但会话已建立，做一次兜底校验
                account_probe = self._request("GET", "/api/account")
                if account_probe.status_code == 200:
                    try:
                        data = account_probe.json()
                        if isinstance(data, dict) and data.get("login"):
                            return
                    except Exception:
                        pass
            message = self._extract_error_message(resp)
            raise RuntimeError(f"登录失败（HTTP {resp.status_code}）：{message}")

        account_resp = self._request("GET", "/api/account")
        if account_resp.status_code != 200:
            message = self._extract_error_message(account_resp)
            raise RuntimeError(f"登录校验失败（HTTP {account_resp.status_code}）：{message}")

    @staticmethod
    def _extract_error_message(resp: requests.Response) -> str:
        try:
            data = resp.json()
            if isinstance(data, dict):
                return str(data.get("message") or data.get("error") or data)
            return str(data)
        except Exception:
            return resp.text[:200]

    def get_table(self, table_id: int) -> Dict[str, Any]:
        if table_id in self.table_cache:
            return self.table_cache[table_id]
        resp = self._request("GET", f"/api/table/{table_id}")
        if resp.status_code != 200:
            message = self._extract_error_message(resp)
            raise RuntimeError(f"获取表详情失败 tableId={table_id}: {message}")
        raw = resp.json()
        table_data = raw.get("data", raw) if isinstance(raw, dict) else {}
        if not isinstance(table_data, dict):
            table_data = {}
        self.table_cache[table_id] = table_data
        return table_data

    def get_tree_with_tables(self, cp_id: int, table_id: int, tree_type: str = "ALL_TREE") -> List[Dict[str, Any]]:
        resp = self._request("GET", f"/api/productGroupTreeWithTables/{cp_id}/{table_id}/{tree_type}")
        if resp.status_code != 200:
            message = self._extract_error_message(resp)
            raise RuntimeError(f"获取目录树失败: {message}")
        data = resp.json()
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "rows", "content", "result"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        raise RuntimeError("目录树接口返回格式异常，无法解析")

    def list_libraries(self) -> List[LibraryMeta]:
        candidates = [
            ("/api/cps", {"params": {"size": 5000}}),
            ("/api/cps", {}),
            ("/api/products", {"params": {"size": 5000}}),
            ("/api/products", {}),
            ("/api/cp", {}),
            ("/api/cp/list", {}),
            ("/api/cpList", {}),
            ("/api/productList", {}),
        ]
        by_cp: Dict[int, LibraryMeta] = {}

        for path, kwargs in candidates:
            try:
                resp = self._request("GET", path, **kwargs)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                payload = resp.json()
            except Exception:
                continue

            for item in iter_dict_objects(payload):
                if not any(k in item for k in ("cpId", "cpid", "cpID", "productId", "cpName", "cpname", "productName")):
                    continue
                cp_id = None
                for raw_cp_id in (
                    item.get("cpId"),
                    item.get("cpid"),
                    item.get("cpID"),
                    item.get("productId"),
                    item.get("id"),
                ):
                    normalized_cp_id = to_non_negative_int(raw_cp_id)
                    if normalized_cp_id is not None:
                        cp_id = normalized_cp_id
                        break
                if cp_id is None:
                    continue

                name = first_non_empty(
                    item,
                    ["cpName", "cpname", "productName", "product", "name", "title"],
                    default="",
                )
                if not name:
                    continue
                if "库" not in name and "数据库" not in name:
                    continue

                table_id = None
                for raw_table_id in (
                    item.get("tableId"),
                    item.get("defaultTableId"),
                    item.get("firstTableId"),
                    item.get("tableid"),
                ):
                    normalized_table_id = to_non_negative_int(raw_table_id)
                    if normalized_table_id is not None:
                        table_id = normalized_table_id
                        break
                current = by_cp.get(cp_id)
                if current is None:
                    by_cp[cp_id] = LibraryMeta(cp_id=cp_id, name=name, table_id=table_id)
                else:
                    if not current.name and name:
                        current.name = name
                    if current.table_id is None and table_id is not None:
                        current.table_id = table_id

        return sorted(by_cp.values(), key=lambda x: x.cp_id)

    def get_tree_for_library(
        self,
        cp_id: int,
        table_candidates: List[Optional[int]],
        tree_type: str = "ALL_TREE",
    ) -> List[Dict[str, Any]]:
        ordered_table_ids: List[int] = []
        seen: set = set()
        for tid in table_candidates + [0]:
            normalized = to_non_negative_int(tid)
            if normalized is None or normalized in seen:
                continue
            seen.add(normalized)
            ordered_table_ids.append(normalized)

        errors: List[str] = []
        empty_result: Optional[List[Dict[str, Any]]] = None
        for tid in ordered_table_ids:
            try:
                nodes = self.get_tree_with_tables(cp_id, tid, tree_type=tree_type)
                if nodes:
                    return nodes
                empty_result = nodes
            except Exception as exc:
                errors.append(f"tableId={tid}: {exc}")

        if empty_result is not None:
            return empty_result
        if errors:
            raise RuntimeError("; ".join(errors[:3]))
        raise RuntimeError("未找到可用的 tableId 来抓取目录树。")


def collect_table_ids(nodes: List[Dict[str, Any]], bucket: List[int]) -> None:
    for node in nodes:
        if is_group_node(node):
            children = node.get("nodes") or []
            if isinstance(children, list):
                collect_table_ids(children, bucket)
        else:
            table_id = node.get("id")
            if isinstance(table_id, (int, float)) and int(table_id) > 0:
                bucket.append(int(table_id))
            elif isinstance(table_id, str) and table_id.isdigit():
                bucket.append(int(table_id))


def collect_group_names(nodes: List[Dict[str, Any]], bucket: List[str]) -> None:
    for node in nodes:
        if not is_group_node(node):
            continue
        name = node_display_name(node)
        if name:
            bucket.append(name)
        children = node.get("nodes") or []
        if isinstance(children, list) and children:
            collect_group_names(children, bucket)


def extract_library_like_names(nodes: List[Dict[str, Any]]) -> List[str]:
    group_names: List[str] = []
    collect_group_names(nodes, group_names)
    return sorted({name for name in group_names if "数据库" in name or name.endswith("库")})


def tree_signature(nodes: List[Dict[str, Any]]) -> str:
    top_names = sorted(
        {
            normalize_text(node_display_name(node))
            for node in nodes
            if is_group_node(node)
        }
    )
    lib_names = [normalize_text(name) for name in extract_library_like_names(nodes)]
    return f"top={','.join(top_names[:20])}|libs={','.join(sorted(set(lib_names))[:40])}|n={len(nodes)}"


def detect_single_wrapper_group(nodes: List[Dict[str, Any]]) -> Optional[str]:
    if len(nodes) != 1:
        return None
    node = nodes[0]
    if not is_group_node(node):
        return None
    name = node_display_name(node).strip()
    if not name:
        return None
    normalized = normalize_text(name)
    if "新版数据库" in normalized or "数据库" in normalized or "信息库" in normalized or normalized.endswith("库"):
        return name
    return None


def extract_top_wrapper_names(nodes: List[Dict[str, Any]]) -> List[str]:
    names: List[str] = []
    for node in nodes:
        if not is_group_node(node):
            continue
        name = node_display_name(node).strip()
        if name:
            names.append(name)
    return names


def get_missing_expected_names(actual_names: List[str], expected_keys: Dict[str, str]) -> List[str]:
    if not expected_keys:
        return []
    matched_expected: set = set()
    for actual in actual_names:
        matched = match_expected_library_name(actual, expected_keys)
        if matched:
            matched_expected.add(matched)
    expected_order = list(expected_keys.values())
    missing = [name for name in expected_order if name not in matched_expected]
    return missing


def filter_top_nodes_by_expected(nodes: List[Dict[str, Any]], expected_keys: Dict[str, str]) -> List[Dict[str, Any]]:
    if not expected_keys:
        return list(nodes)
    filtered: List[Dict[str, Any]] = []
    for node in nodes:
        if not is_group_node(node):
            continue
        raw_name = node_display_name(node)
        matched_name = match_expected_library_name(raw_name, expected_keys)
        if not matched_name:
            continue
        if normalize_text(raw_name) == normalize_text(matched_name):
            filtered.append(node)
            continue
        cloned = dict(node)
        cloned["groupName"] = matched_name
        cloned["name"] = matched_name
        filtered.append(cloned)
    return filtered


def preload_tables(client: DDClient, table_ids: List[int], workers: int = 8) -> None:
    unique_ids = sorted(set(table_ids))
    if not unique_ids:
        print("[进度] 无表详情需要预加载。", flush=True)
        return

    workers = max(1, workers)
    total = len(unique_ids)
    done = 0
    failed = 0
    start_ts = time.time()
    report_every = max(1, total // 20)  # 约 5% 报一次
    print(f"[进度] 开始预加载表详情: {total} 张表, 并发={workers}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_id = {executor.submit(client.get_table, table_id): table_id for table_id in unique_ids}
        for future in as_completed(future_to_id):
            table_id = future_to_id[future]
            try:
                future.result()
            except Exception as exc:
                failed += 1
                print(f"[WARN] 预加载表详情失败 tableId={table_id}: {exc}")
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


def build_rows(
    client: DDClient,
    tree_nodes: List[Dict[str, Any]],
    source: SourceMeta,
    update_date: str,
) -> List[Dict[str, Any]]:
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

    def table_code(table_id: int, node: Dict[str, Any]) -> str:
        inline_name = first_non_empty(node, ["tableName", "tableEnName", "tableCode", "enName"])
        table_name = inline_name
        if not table_name:
            table_data = client.table_cache.get(table_id) or {}
            table_name = first_non_empty(
                table_data,
                ["tableName", "tableEnName", "tableCode", "resourceName", "name"],
            )
        cleaned = re.sub(r"\s+", "", table_name).upper()
        if not cleaned:
            cleaned = f"TABLE_{table_id}"
        return f"{source.code}_{cleaned}"

    def walk(nodes: List[Dict[str, Any]], parent_code: str) -> None:
        group_index = 0

        def process_node(node: Dict[str, Any], current_parent_code: str, current_group_index: int) -> int:
            if is_group_node(node):
                name = node_display_name(node)
                children = node.get("nodes") or []

                current_group_index += 1
                current_code = f"{current_parent_code}{current_group_index:02d}"
                rows.append(
                    {
                        "序号": None,
                        "节点编号": current_code,
                        "节点名称": name,
                        "父节点编号": current_parent_code,
                        "描述": name,
                        "类型（0-数据源，1-目录，2-表）": 1,
                        "更新时间": update_date,
                    }
                )
                if isinstance(children, list) and children:
                    walk(children, current_code)
                return current_group_index

            raw_table_id = node.get("id")
            try:
                tid = int(raw_table_id)
            except Exception:
                tid = -1
            name = node_display_name(node)
            rows.append(
                {
                    "序号": None,
                    "节点编号": table_code(tid, node),
                    "节点名称": name,
                    "父节点编号": current_parent_code,
                    "描述": name,
                    "类型（0-数据源，1-目录，2-表）": 2,
                    "更新时间": update_date,
                }
            )
            return current_group_index

        for node in nodes:
            group_index = process_node(node, parent_code, group_index)

    walk(tree_nodes, source.code)
    return rows


def main() -> int:
    args = parse_args()
    table_id = args.table_id if args.table_id is not None else parse_table_id(args.url)
    source_meta = load_source_meta(args)
    print(f"[进度] 任务启动: tableId={table_id}, 输出={Path(args.output).resolve()}", flush=True)

    client = DDClient()
    print("[进度] 初始化站点会话...", flush=True)
    client.bootstrap()

    username = read_username_interactive(args.username)

    password = read_password_interactive(args.password, visible_password=args.visible_password)
    if not password:
        raise RuntimeError("密码不能为空。")

    captcha = args.captcha
    max_login_attempts = 5
    for attempt in range(1, max_login_attempts + 1):
        if not captcha:
            print("正在获取验证码...", flush=True)
            captcha_path = client.download_captcha(CAPTCHA_FILE)
            print(f"验证码图片已保存: {captcha_path}")
            print("[进度] 等待输入验证码...", flush=True)
            try:
                captcha = input("请输入验证码: ").strip()
            except EOFError as exc:
                raise RuntimeError("当前运行环境无法交互输入验证码，请在运行参数里传 --captcha。") from exc

        try:
            client.login(username=username, password=password, captcha=captcha, paa=args.paa, remember_me=args.remember_me)
            break
        except RuntimeError as exc:
            err = str(exc)
            if "验证码错误" in err and attempt < max_login_attempts:
                if not can_interactive_input():
                    raise RuntimeError("验证码错误且当前运行环境不可交互，请更新 --captcha 后重试。") from exc
                print(f"[WARN] 验证码错误（第 {attempt} 次），请重新输入。")
                captcha = None
                continue
            if ("HTTP 500" in err or "DataException" in err) and attempt < max_login_attempts:
                if not can_interactive_input():
                    raise RuntimeError(
                        "登录失败且当前运行环境不可交互，请在参数中确认 --username/--password/--captcha 后重试。"
                    ) from exc
                print(f"[WARN] 登录接口返回 500（第 {attempt} 次），将重新输入账号并重试。")
                username = read_username_interactive(None)
                password = read_password_interactive(None, visible_password=args.visible_password)
                captcha = None
                continue
            raise

    print("登录成功，开始抓取目录树...")

    print("[进度] 获取目标表信息...", flush=True)
    table_data = client.get_table(table_id)
    cp_id = to_non_negative_int(table_data.get("cpId"))
    if cp_id is None:
        raise RuntimeError("无法从 api/table 返回中获取 cpId，请检查接口返回。")
    seed_library_name = first_non_empty(
        table_data,
        ["cpName", "cpname", "productName", "name", "title"],
        default="",
    )
    if seed_library_name:
        print(f"[进度] 已获取当前库 cpId={cp_id}, 库名={seed_library_name}", flush=True)
    else:
        print(f"[进度] 已获取当前库 cpId={cp_id}", flush=True)

    print("[进度] 识别可抓取数据库库列表...", flush=True)
    libraries = client.list_libraries()
    by_cp: Dict[int, LibraryMeta] = {lib.cp_id: lib for lib in libraries}
    if cp_id not in by_cp:
        by_cp[cp_id] = LibraryMeta(cp_id=cp_id, name=seed_library_name, table_id=table_id)
    else:
        if not by_cp[cp_id].name:
            by_cp[cp_id].name = seed_library_name
        if by_cp[cp_id].table_id is None:
            by_cp[cp_id].table_id = table_id
    libraries = sorted(by_cp.values(), key=lambda x: x.cp_id)
    expected_libraries = parse_expected_libraries(DEFAULT_EXPECTED_LIBRARIES)
    expected_library_keys = {normalize_text(name): name for name in expected_libraries}
    print(f"[进度] 写表保留一级库: {'，'.join(expected_libraries)}", flush=True)

    tree_nodes: List[Dict[str, Any]] = []
    table_ids: List[int] = []
    success_count = 0
    seen_tree_signatures: set = set()

    def append_library_tree(lib_name: str, lib_tree: List[Dict[str, Any]]) -> bool:
        nonlocal success_count
        if not isinstance(lib_tree, list) or not lib_tree:
            return False

        sig = tree_signature(lib_tree)
        if sig in seen_tree_signatures:
            return False
        seen_tree_signatures.add(sig)

        normalized_lib_name = (lib_name or "").strip()
        if (
            normalized_lib_name
            and len(lib_tree) == 1
            and is_group_node(lib_tree[0])
            and normalize_text(node_display_name(lib_tree[0])) == normalize_text(normalized_lib_name)
        ):
            tree_nodes.extend(lib_tree)
        elif normalized_lib_name:
            tree_nodes.append(
                {
                    "groupName": normalized_lib_name,
                    "name": normalized_lib_name,
                    "istable": 0,
                    "nodes": lib_tree,
                }
            )
        else:
            tree_nodes.extend(lib_tree)

        collect_table_ids(lib_tree, table_ids)
        success_count += 1
        return True

    # 常见情况：库列表接口拿不到有效库名/库清单，此时直接抓当前 cpId 的完整树，避免出现“数据库8”占位节点。
    valid_libraries = [lib for lib in libraries if lib.name.strip()]
    print(f"[进度] 库列表识别结果: 候选={len(libraries)}, 有效命名={len(valid_libraries)}", flush=True)

    non_target_samples: List[str] = []

    def probe_cp_range(start_cp: int, end_cp: int, label: str) -> None:
        if start_cp > end_cp:
            return
        print(f"[进度] {label}: 探测 cpId（{start_cp}..{end_cp}）...", flush=True)
        probe_hits = 0
        probe_dup = 0
        probe_non_expected = 0
        for probe_cp_id in range(start_cp, end_cp + 1):
            if probe_cp_id == cp_id:
                continue
            if probe_cp_id % 10 == 0 or probe_cp_id == end_cp:
                print(
                    f"[进度] cpId 探测进度: {probe_cp_id}/{end_cp}, 新增={probe_hits}, 重复={probe_dup}, 非目标={probe_non_expected}",
                    flush=True,
                )
            try:
                probe_tree = client.get_tree_for_library(probe_cp_id, [table_id], tree_type="ALL_TREE")
            except Exception:
                continue
            if not isinstance(probe_tree, list) or not probe_tree:
                continue

            probe_name = detect_single_wrapper_group(probe_tree) or ""
            if not probe_name:
                probe_non_expected += 1
                if len(non_target_samples) < 12:
                    non_target_samples.append(f"{probe_cp_id}:(无一级库包装名)")
                continue

            before = success_count
            if append_library_tree(probe_name, probe_tree):
                probe_hits += 1
                if probe_name:
                    print(f"[进度] 探测命中 cpId={probe_cp_id}: {probe_name}", flush=True)
                else:
                    print(f"[进度] 探测命中 cpId={probe_cp_id}: (未识别包装名)", flush=True)
            elif success_count == before:
                probe_dup += 1

        tail = f"，非目标样本={'; '.join(non_target_samples[:5])}" if non_target_samples else ""
        print(
            f"[进度] {label}完成，新增库树={probe_hits}，重复树={probe_dup}，非目标树={probe_non_expected}{tail}",
            flush=True,
        )

    if len(valid_libraries) <= 1:
        print("[WARN] 多库接口返回不足（常见于权限/接口差异），将启用树抓取+cpId探测补偿。", flush=True)
        print("[进度] 未识别到稳定的多库清单，改为抓取当前 cpId 的完整目录树...", flush=True)
        seed_tree = client.get_tree_for_library(cp_id, [table_id], tree_type="ALL_TREE")
        append_library_tree(seed_library_name, seed_tree)

        wrapper_name = detect_single_wrapper_group(seed_tree)
        if wrapper_name:
            print(f"[进度] 检测到单一包装节点: {wrapper_name}，判定为下拉库场景。", flush=True)

        if args.cp_probe_max > 0:
            probe_cp_range(1, args.cp_probe_max, "首轮探测")
        print("[进度] 已按要求关闭扩展探测。", flush=True)
    else:
        preview = "，".join([f"{lib.name}(cpId={lib.cp_id})" for lib in valid_libraries[:8]])
        print(f"[进度] 计划抓取数据库库 {len(valid_libraries)} 个：{preview}", flush=True)
        for idx, lib in enumerate(valid_libraries, start=1):
            lib_name = lib.name
            print(f"[进度] ({idx}/{len(valid_libraries)}) 抓取库 {lib_name} (cpId={lib.cp_id})...", flush=True)
            try:
                lib_tree = client.get_tree_for_library(lib.cp_id, [lib.table_id, table_id], tree_type="ALL_TREE")
            except Exception as exc:
                print(f"[WARN] 抓取库失败，已跳过 {lib_name} (cpId={lib.cp_id}): {exc}", flush=True)
                continue

            if not isinstance(lib_tree, list):
                print(f"[WARN] 抓取库返回异常（非列表），已跳过 {lib_name} (cpId={lib.cp_id})", flush=True)
                continue

            append_library_tree(lib_name, lib_tree)

    if not tree_nodes:
        raise RuntimeError("未抓取到任何库目录树，请检查账号权限或接口返回。")

    # 按用户要求：抓取可用库后，写表时仅保留这四个一级库，并让后续编号随过滤结果自动重排。
    tree_nodes = filter_top_nodes_by_expected(tree_nodes, expected_library_keys)
    if not tree_nodes:
        raise RuntimeError(f"过滤后无可写入数据，请检查一级库名称: {'，'.join(expected_libraries)}")
    table_ids = []
    collect_table_ids(tree_nodes, table_ids)

    print(f"[进度] 目录树抓取完成，成功库数={success_count}，顶层节点数={len(tree_nodes)}", flush=True)
    top_wrapper_names = extract_top_wrapper_names(tree_nodes)
    if top_wrapper_names:
        preview = "，".join(top_wrapper_names[:10])
        print(f"[进度] 顶层一级库节点 {len(top_wrapper_names)} 个（预览）: {preview}", flush=True)
    missing = get_missing_expected_names(top_wrapper_names, expected_library_keys)
    if missing:
        print(f"[WARN] 以下目标一级库未抓到: {'，'.join(missing)}", flush=True)
    print(f"[进度] 识别到表节点 {len(set(table_ids))} 个，开始预加载表详情...", flush=True)
    preload_tables(client, table_ids, workers=args.workers)

    print("[进度] 生成节点清单与编号...", flush=True)
    rows = build_rows(client, tree_nodes, source_meta, args.update_date)
    print(f"[进度] 节点清单生成完成，共 {len(rows)} 行。", flush=True)
    root_dirs = [
        r for r in rows
        if r["类型（0-数据源，1-目录，2-表）"] == 1 and r["父节点编号"] == source_meta.code
    ]
    if root_dirs:
        preview = "，".join([f'{r["节点编号"]}:{r["节点名称"]}' for r in root_dirs[:5]])
        print(f"[进度] 一级目录预览: {preview}", flush=True)

    df = pd.DataFrame(
        rows,
        columns=["序号", "节点编号", "节点名称", "父节点编号", "描述", "类型（0-数据源，1-目录，2-表）", "更新时间"],
    )
    output_path = Path(args.output).resolve()
    print("[进度] 写出 Excel...", flush=True)
    df.to_excel(output_path, index=False)
    print(f"完成：共输出 {len(df)} 行 -> {output_path}")
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
