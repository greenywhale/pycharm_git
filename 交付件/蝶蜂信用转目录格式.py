from __future__ import annotations

import argparse
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import pandas as pd

SHEET_NAME = "表"
COL_CATEGORY = "产品类型"
COL_TABLE_EN = "表名"
COL_TABLE_CN = "产品中文名称"
DEFAULT_INPUT_NAME = "蝶蜂数据字典.xlsx"
DEFAULT_OUTPUT_NAME = "蝶蜂数据节点_爬取结果.xlsx"

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


def normalize_text(value: object) -> str:
    if pd.isna(value):
        return ""
    return str(value).strip()


def split_category_path(category: object) -> list[str]:
    text = normalize_text(category)
    if not text:
        return ["未分类"]

    parts = [
        p.strip()
        for p in re.split(r"[\\/>／＞]+", text)
        if p and p.strip()
    ]
    return parts or [text]


def format_dir_code(source_code: str, index_path: tuple[int, ...]) -> str:
    suffix = "".join(f"{i:02d}" for i in index_path)
    return f"{source_code}{suffix}"


def build_node_table(
    df_table: pd.DataFrame,
    source_code: str,
    source_name: str,
    source_desc: str,
    updated_at: str,
) -> pd.DataFrame:
    required_cols = [COL_CATEGORY, COL_TABLE_EN, COL_TABLE_CN]
    missing = [c for c in required_cols if c not in df_table.columns]
    if missing:
        raise ValueError(f"输入表缺少必要列: {missing}")

    rows: list[dict[str, object]] = []
    rows.append(
        {
            COL_SEQ: None,
            COL_NODE_CODE: source_code,
            COL_NODE_NAME: source_name,
            COL_PARENT_CODE: "0",
            COL_DESC: source_desc,
            COL_TYPE: 0,
            COL_UPDATE_TIME: updated_at,
        }
    )

    dir_meta: dict[tuple[str, ...], dict[str, object]] = {}
    index_paths: dict[tuple[str, ...], tuple[int, ...]] = {(): ()}
    child_counter: defaultdict[tuple[str, ...], int] = defaultdict(int)
    seen_table_codes: set[str] = set()

    for _, record in df_table.iterrows():
        table_en = normalize_text(record.get(COL_TABLE_EN, "")).upper()
        if not table_en:
            continue

        table_cn = normalize_text(record.get(COL_TABLE_CN, "")) or table_en
        category_path = tuple(split_category_path(record.get(COL_CATEGORY, "")))

        for depth in range(1, len(category_path) + 1):
            current_path = category_path[:depth]
            if current_path in dir_meta:
                continue

            parent_path = current_path[:-1]
            child_counter[parent_path] += 1
            idx = child_counter[parent_path]

            parent_idx_path = index_paths.get(parent_path, ())
            idx_path = parent_idx_path + (idx,)
            index_paths[current_path] = idx_path

            node_code = format_dir_code(source_code, idx_path)
            parent_code = source_code if not parent_path else str(dir_meta[parent_path]["code"])
            node_name = current_path[-1]

            dir_meta[current_path] = {
                "code": node_code,
                "parent": parent_code,
                "name": node_name,
            }
            rows.append(
                {
                    COL_SEQ: None,
                    COL_NODE_CODE: node_code,
                    COL_NODE_NAME: node_name,
                    COL_PARENT_CODE: parent_code,
                    COL_DESC: node_name,
                    COL_TYPE: 1,
                    COL_UPDATE_TIME: updated_at,
                }
            )

        table_code = f"{source_code}_{table_en}"
        if table_code in seen_table_codes:
            continue
        seen_table_codes.add(table_code)

        parent_code = source_code
        if category_path:
            parent_code = str(dir_meta[category_path]["code"])

        rows.append(
            {
                COL_SEQ: None,
                COL_NODE_CODE: table_code,
                COL_NODE_NAME: table_cn,
                COL_PARENT_CODE: parent_code,
                COL_DESC: table_cn,
                COL_TYPE: 2,
                COL_UPDATE_TIME: updated_at,
            }
        )

    for i, row in enumerate(rows, start=1):
        row[COL_SEQ] = i

    return pd.DataFrame(rows, columns=OUTPUT_COLUMNS)


def parse_args() -> argparse.Namespace:
    base_dir = Path(__file__).resolve().parent

    parser = argparse.ArgumentParser(
        description="将蝶蜂数据字典转换为目录节点格式"
    )
    parser.add_argument(
        "--input",
        dest="input_path",
        type=Path,
        default=base_dir / DEFAULT_INPUT_NAME,
        help="输入Excel文件路径",
    )
    parser.add_argument(
        "--output",
        dest="output_path",
        type=Path,
        default=base_dir / DEFAULT_OUTPUT_NAME,
        help="输出Excel文件路径",
    )
    parser.add_argument(
        "--sheet",
        default=SHEET_NAME,
        help="使用的工作表名称",
    )
    parser.add_argument(
        "--source-code",
        default="DFSJ",
        help="根节点编号（例：DFSJ）",
    )
    parser.add_argument(
        "--source-name",
        default="蝶蜂数据",
        help="根节点名称",
    )
    parser.add_argument(
        "--source-desc",
        default="蝶蜂数据字典",
        help="根节点描述",
    )
    parser.add_argument(
        "--date",
        default=date.today().isoformat(),
        help="更新时间，默认为今天(YYYY-MM-DD)",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    df_table = pd.read_excel(args.input_path, sheet_name=args.sheet)
    node_df = build_node_table(
        df_table=df_table,
        source_code=args.source_code,
        source_name=args.source_name,
        source_desc=args.source_desc,
        updated_at=args.date,
    )

    node_df.to_excel(args.output_path, index=False)
    print(f"转换完成，共生成{len(node_df)}条节点：{args.output_path}")


if __name__ == "__main__":
    main()
