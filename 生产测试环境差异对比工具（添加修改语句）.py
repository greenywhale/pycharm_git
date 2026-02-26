"""
Created on Wed Feb 25 14:01:51 2026

@author: 32197
"""

import os
import pandas as pd
import re

# 路径配置
BASE_DIR = r'C:\Users\32197\Desktop\ljq\数据源表目录'
os.chdir(BASE_DIR)

TABLE_KEYS = ["业务数据库名", "表英文名"]
COLUMN_KEYS = ["业务数据库名", "表英文名", "字段英文名"]

# 字段属性差异判定项
ATTR_COLS = [
    "字段类型",
    "字段长度",
    "字段精度",
    "是否非空",
    "是否有索引",
    "是否主键",
    "是否外键",
    "字段默认值",
]


# 基础工具函数
def q_table(schema_name, table_name):
    return f"{schema_name}.{table_name}"


def q_col(schema_name, table_name, col_name):
    return f"{schema_name}.{table_name}.{col_name}"


def sql_quote(v):
    """Oracle字符串字面量；空值返回NULL"""
    if pd.isna(v) or v is None:
        return "NULL"
    s = str(v)
    return "'" + s.replace("'", "''") + "'"


def yn1(v) -> bool:
    """兼容 1/0、Y/N、是/否 """
    if pd.isna(v):
        return False
    s = str(v).strip().upper()
    return s in {"1", "Y", "YES", "TRUE", "是", "T"}


def build_oracle_type(row) -> str:
    """
    根据字段类型 + 长度 + 精度拼接Oracle类型表达式
    例：
      NUMBER + 20 + 0 -> NUMBER(20,0)
      VARCHAR2 + 60 + 空 -> VARCHAR2(60)
    """
    dtype = row.get("字段类型")
    length = row.get("字段长度")
    precision = row.get("字段精度")

    if pd.isna(dtype) or str(dtype).strip() == "":
        return "VARCHAR2(4000)"

    dtype = str(dtype).strip()

    l = None if pd.isna(length) or str(length).strip() == "" else str(length).strip()
    p = None if pd.isna(precision) or str(precision).strip() == "" else str(precision).strip()

    if l and p:
        return f"{dtype}({l},{p})"
    elif l:
        return f"{dtype}({l})"
    else:
        return dtype
def oracle_default_literal(v):
    """
    生成Oracle默认值字面量/表达式（不再一律按字符串处理）
    规则：
    - 空值 -> NULL
    - 纯数字 -> 原样
    - 常见Oracle表达式/函数 -> 原样
    - 其他 -> 按字符串加引号
    """
    if pd.isna(v) or v is None:
        return "NULL"

    s = str(v).strip()
    if s == "":
        return "NULL"

    s_upper = s.upper()

    # 1) 明确的NULL
    if s_upper == "NULL":
        return "NULL"

    # 2) 纯数字（整数/小数，支持正负号）
    if re.fullmatch(r"[+-]?\d+(\.\d+)?", s):
        return s

    # 3) 常见Oracle内置日期/时间/用户上下文表达式（可按需扩展）
    common_exprs = {
        "SYSDATE",
        "SYSTIMESTAMP",
        "CURRENT_DATE",
        "CURRENT_TIMESTAMP",
        "LOCALTIMESTAMP",
        "USER",
        "UID",
        "SYS_GUID()",
    }
    if s_upper in common_exprs:
        return s

    # 4) 看起来像函数调用/表达式：XXX(...)
    #    如 TO_CHAR(SYSDATE,'YYYYMMDD')、NVL(...), TRUNC(...)
    if re.match(r"^[A-Z_][A-Z0-9_$#]*\s*\(.*\)$", s_upper):
        return s

    # 5) 其他情况按字符串处理
    return sql_quote(s)

# SQL生成函数
def build_table_cn_sql(row) -> str:
    """表中文名差异 -> 将测试环境表中文名改为生产环境"""
    return f"COMMENT ON TABLE {q_table(row['业务数据库名'], row['表英文名'])} IS {sql_quote(row['生产环境表中文名'])};"


def build_col_cn_sql(row) -> str:
    """字段中文名差异 -> 将测试环境字段中文名改为生产环境"""
    return f"COMMENT ON COLUMN {q_col(row['业务数据库名'], row['表英文名'], row['字段英文名'])} IS {sql_quote(row['生产环境字段中文名'])};"


def build_add_col_sql(schema_name, table_name, col_name, prod_row) -> str:
    """
    字段缺失（TEST缺） -> 按生产环境字段属性生成 ADD COLUMN SQL
    只处理可确定属性：类型/长度/精度/默认值/非空 + 列注释
    """
    col_type = build_oracle_type(prod_row)
    col_def = f"{col_name} {col_type}"

    default_val = prod_row.get("字段默认值")
    if not pd.isna(default_val) and str(default_val).strip() != "":
        col_def += f" DEFAULT {oracle_default_literal(default_val)}"

    if yn1(prod_row.get("是否非空")):
        col_def += " NOT NULL"

    sqls = [f"ALTER TABLE {q_table(schema_name, table_name)} ADD ({col_def});"]

    # 列注释（字段中文名）
    col_cn = prod_row.get("字段中文名")
    if not pd.isna(col_cn) and str(col_cn) != "":
        sqls.append(f"COMMENT ON COLUMN {q_col(schema_name, table_name, col_name)} IS {sql_quote(col_cn)};")

    return "\n".join(sqls)


def build_drop_col_sql(row) -> str:
    """字段冗余（TEST多） -> 删除测试环境字段"""
    return f"ALTER TABLE {q_table(row['业务数据库名'], row['表英文名'])} DROP COLUMN {row['字段英文名']};"


def build_modify_col_sql(schema_name, table_name, col_name, prod_row, diff_attrs) -> str:
    """
    字段属性差异（一字段一行）-> 生成按生产环境修复的Oracle SQL
    - 可执行属性生成SQL
    - 索引/主键/外键差异不生成DDL，但在修改SQL中标注差异来源
    """
    sqls = []

    # 可执行属性（会生成SQL）
    executable_attrs = {"字段类型", "字段长度", "字段精度", "是否非空", "字段默认值"}
    exec_diff_attrs = [a for a in diff_attrs if a in executable_attrs]

    # 不可自动修复属性（仅标注）
    manual_attrs = []
    for a in ["是否有索引", "是否主键", "是否外键"]:
        if a in diff_attrs:
            manual_attrs.append(a)

    # 1) 类型/长度/精度/非空
    if any(a in exec_diff_attrs for a in ["字段类型", "字段长度", "字段精度", "是否非空"]):
        type_expr = build_oracle_type(prod_row)
        nullable_clause = "NOT NULL" if yn1(prod_row.get("是否非空")) else "NULL"
        sqls.append(
            f"ALTER TABLE {q_table(schema_name, table_name)} MODIFY ({col_name} {type_expr} {nullable_clause});"
        )

    # 2) 默认值（单独一条更稳）
    if "字段默认值" in exec_diff_attrs:
        default_val = prod_row.get("字段默认值")
        if pd.isna(default_val) or str(default_val).strip() == "":
            sqls.append(
                f"ALTER TABLE {q_table(schema_name, table_name)} MODIFY ({col_name} DEFAULT NULL);"
            )
        else:
            sqls.append(
                f"ALTER TABLE {q_table(schema_name, table_name)} MODIFY ({col_name} DEFAULT {oracle_default_literal(default_val)});"
            )

    # 3) 对索引/主键/外键差异进行标注（不生成DDL）
    if manual_attrs:
        sqls.append(f"-- 差异属性: {', '.join(manual_attrs)}（需人工处理）")

    # 去重，保持顺序
    out = []
    seen = set()
    for s in sqls:
        if s not in seen:
            out.append(s)
            seen.add(s)

    return "\n".join(out)

# 读取CSV（含过滤SMPPW/STATS）
def read_catalog_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)

    must = set(TABLE_KEYS + ["字段英文名"])
    missing = [c for c in must if c not in df.columns]
    if missing:
        raise ValueError(f"缺少关键列: {missing}，当前列: {list(df.columns)}")

    # 去掉关键键为空的行
    df = df.dropna(subset=COLUMN_KEYS)

    # 过滤不参与比对的业务数据库名
    exclude_dbs = {"SMPPW", "STATS"}
    before_rows = len(df)
    df = df[~df["业务数据库名"].isin(exclude_dbs)].copy()
    after_rows = len(df)

    print(f"{os.path.basename(path)} 过滤SMPPW/STATS: {before_rows} -> {after_rows}")
    return df


# 主逻辑
def compare_schema_csv(prod_csv: str, test_csv: str, out_xlsx: str = "测试生产环境差异.xlsx") -> None:
    prod = read_catalog_csv(prod_csv)
    test = read_catalog_csv(test_csv)

    # 1) 表级差异
    prod_tables = prod[TABLE_KEYS].drop_duplicates()
    test_tables = test[TABLE_KEYS].drop_duplicates()

    prod_table_set = set(map(tuple, prod_tables.values.tolist()))
    test_table_set = set(map(tuple, test_tables.values.tolist()))

    missing_tables_in_test = list(prod_table_set - test_table_set)
    extra_tables_in_test = list(test_table_set - prod_table_set)

    table_diff = pd.DataFrame(
        [{"差异类型": "表缺失", "业务数据库名": db, "表英文名": t} for db, t in missing_tables_in_test] +
        [{"差异类型": "表冗余", "业务数据库名": db, "表英文名": t} for db, t in extra_tables_in_test]
    )
    if not table_diff.empty:
        table_diff = table_diff.sort_values(by=["差异类型", "业务数据库名", "表英文名"], na_position="last")

    # 2) 共同表
    common_table_set = prod_table_set & test_table_set
    common_tables_df = pd.DataFrame(list(common_table_set), columns=TABLE_KEYS)

    # 初始化输出DataFrame
    table_cn_name_mismatch_df = pd.DataFrame(columns=[
        "差异类型", "业务数据库名", "表英文名", "生产环境表中文名", "测试环境表中文名", "修改SQL"
    ])
    col_cn_mismatch_df = pd.DataFrame(columns=[
        "差异类型", "业务数据库名", "表英文名", "字段英文名", "生产环境字段中文名", "测试环境字段中文名", "修改SQL"
    ])
    missing_col_df = pd.DataFrame(columns=["差异类型"] + COLUMN_KEYS + ["修改SQL"])
    extra_col_df = pd.DataFrame(columns=["差异类型"] + COLUMN_KEYS + ["修改SQL"])
    mismatch_df = pd.DataFrame(columns=[
        "差异类型", "业务数据库名", "表英文名", "字段英文名", "修改SQL"
    ])

    if not common_tables_df.empty:
        prod_common_tables = prod.merge(common_tables_df, on=TABLE_KEYS, how="inner")
        test_common_tables = test.merge(common_tables_df, on=TABLE_KEYS, how="inner")

        # 2.1 表中文名差异
        if "表中文名" in prod.columns and "表中文名" in test.columns:
            prod_table_cn = (
                prod_common_tables[TABLE_KEYS + ["表中文名"]]
                .drop_duplicates(subset=TABLE_KEYS, keep="first")
                .rename(columns={"表中文名": "生产环境表中文名"})
            )
            test_table_cn = (
                test_common_tables[TABLE_KEYS + ["表中文名"]]
                .drop_duplicates(subset=TABLE_KEYS, keep="first")
                .rename(columns={"表中文名": "测试环境表中文名"})
            )

            table_cn_compare = prod_table_cn.merge(test_table_cn, on=TABLE_KEYS, how="inner")

            mask = ~(
                (table_cn_compare["生产环境表中文名"].isna() & table_cn_compare["测试环境表中文名"].isna()) |
                (table_cn_compare["生产环境表中文名"] == table_cn_compare["测试环境表中文名"])
            )

            table_cn_name_mismatch_df = table_cn_compare.loc[mask].copy()
            if not table_cn_name_mismatch_df.empty:
                table_cn_name_mismatch_df.insert(0, "差异类型", "表中文名不一致")
                table_cn_name_mismatch_df["修改SQL"] = table_cn_name_mismatch_df.apply(build_table_cn_sql, axis=1)
                table_cn_name_mismatch_df = table_cn_name_mismatch_df.sort_values(
                    by=["业务数据库名", "表英文名"], na_position="last"
                )

        # 2.2 字段差异
        compare_attrs = [c for c in ATTR_COLS if c in prod.columns and c in test.columns]

        # 字段中文名单独比较，所以额外保留“字段中文名”
        extra_cols_for_compare = []
        if "字段中文名" in prod.columns and "字段中文名" in test.columns:
            extra_cols_for_compare.append("字段中文名")

        prod_field_base = (
            prod_common_tables[COLUMN_KEYS + extra_cols_for_compare + compare_attrs]
            .drop_duplicates(subset=COLUMN_KEYS, keep="first")
        )
        test_field_base = (
            test_common_tables[COLUMN_KEYS + extra_cols_for_compare + compare_attrs]
            .drop_duplicates(subset=COLUMN_KEYS, keep="first")
        )

        # outer merge 一次性拿到 缺失/冗余/共同字段
        field_compare = prod_field_base.merge(
            test_field_base,
            on=COLUMN_KEYS,
            how="outer",
            suffixes=("_PROD", "_TEST"),
            indicator=True
        )

        # 生产环境字段属性索引（用于生成SQL）
        prod_idx = prod_field_base.set_index(COLUMN_KEYS)

        # 字段中文名差异
        if "字段中文名" in prod.columns and "字段中文名" in test.columns:
            both_cn_df = field_compare[field_compare["_merge"] == "both"].copy()
            prod_cn_col = "字段中文名_PROD"
            test_cn_col = "字段中文名_TEST"

            if (not both_cn_df.empty) and (prod_cn_col in both_cn_df.columns) and (test_cn_col in both_cn_df.columns):
                cn_mask = ~(
                    (both_cn_df[prod_cn_col].isna() & both_cn_df[test_cn_col].isna()) |
                    (both_cn_df[prod_cn_col] == both_cn_df[test_cn_col])
                )

                col_cn_mismatch_df = both_cn_df.loc[cn_mask, COLUMN_KEYS + [prod_cn_col, test_cn_col]].copy()
                if not col_cn_mismatch_df.empty:
                    col_cn_mismatch_df = col_cn_mismatch_df.rename(columns={
                        prod_cn_col: "生产环境字段中文名",
                        test_cn_col: "测试环境字段中文名"
                    })
                    col_cn_mismatch_df.insert(0, "差异类型", "字段中文名不一致")
                    col_cn_mismatch_df["修改SQL"] = col_cn_mismatch_df.apply(build_col_cn_sql, axis=1)

        # 字段缺失
        missing_col_df = field_compare[field_compare["_merge"] == "left_only"][COLUMN_KEYS].copy()
        if not missing_col_df.empty:
            missing_col_df.insert(0, "差异类型", "字段缺失")
            missing_col_df["修改SQL"] = missing_col_df.apply(
                lambda r: build_add_col_sql(
                    r["业务数据库名"],
                    r["表英文名"],
                    r["字段英文名"],
                    prod_idx.loc[(r["业务数据库名"], r["表英文名"], r["字段英文名"])]
                ),
                axis=1
            )

        # 字段冗余
        extra_col_df = field_compare[field_compare["_merge"] == "right_only"][COLUMN_KEYS].copy()
        if not extra_col_df.empty:
            extra_col_df.insert(0, "差异类型", "字段冗余")
            extra_col_df["修改SQL"] = extra_col_df.apply(build_drop_col_sql, axis=1)

        # 字段属性差异
        both_df = field_compare[field_compare["_merge"] == "both"].copy()

        mismatch_rows = []
        if not both_df.empty:
            for _, row in both_df.iterrows():
                diff_attrs = []

                for attr in compare_attrs:
                    prod_col = f"{attr}_PROD"
                    test_col = f"{attr}_TEST"

                    pv = row.get(prod_col)
                    tv = row.get(test_col)

                    if pd.isna(pv) and pd.isna(tv):
                        continue
                    if pv != tv:
                        diff_attrs.append(attr)

                if diff_attrs:
                    key = (row["业务数据库名"], row["表英文名"], row["字段英文名"])
                    prod_row = prod_idx.loc[key]
                    if isinstance(prod_row, pd.DataFrame):
                        prod_row = prod_row.iloc[0]

                    sql_text = build_modify_col_sql(
                        row["业务数据库名"],
                        row["表英文名"],
                        row["字段英文名"],
                        prod_row,
                        diff_attrs
                    )

                    mismatch_rows.append({
                        "差异类型": "字段属性不一致",
                        "业务数据库名": row["业务数据库名"],
                        "表英文名": row["表英文名"],
                        "字段英文名": row["字段英文名"],
                        "修改SQL": sql_text
                    })

        if mismatch_rows:
            mismatch_df = pd.DataFrame(mismatch_rows, columns=[
                "差异类型", "业务数据库名", "表英文名", "字段英文名", "修改SQL"
            ])

    # 3) 输出
    with pd.ExcelWriter(out_xlsx, engine="openpyxl") as writer:
        table_diff.to_excel(writer, index=False, sheet_name="表格缺失或冗余")
        table_cn_name_mismatch_df.to_excel(writer, index=False, sheet_name="表中文名差异")
        col_cn_mismatch_df.to_excel(writer, index=False, sheet_name="字段中文名差异")
        missing_col_df.to_excel(writer, index=False, sheet_name="字段缺失")
        extra_col_df.to_excel(writer, index=False, sheet_name="字段冗余")
        mismatch_df.to_excel(writer, index=False, sheet_name="字段属性差异")

    print("运行目录:", os.getcwd())
    print("输出文件:", os.path.abspath(out_xlsx))
    print("共同表数量:", len(common_table_set))
    print("缺失表数量(TEST缺):", len(missing_tables_in_test))
    print("冗余表数量(TEST多):", len(extra_tables_in_test))
    print("表中文名差异数量:", len(table_cn_name_mismatch_df))
    print("字段中文名差异数量:", len(col_cn_mismatch_df))
    print("字段缺失数量(TEST缺):", len(missing_col_df))
    print("字段冗余数量(TEST多):", len(extra_col_df))
    print("字段属性差异数量:", len(mismatch_df))


if __name__ == "__main__":
    prod_csv = os.path.join(BASE_DIR, "生产环境.csv")
    test_csv = os.path.join(BASE_DIR, "测试环境.csv")
    out_xlsx = os.path.join(BASE_DIR, "测试生产环境差异.xlsx")

    compare_schema_csv(prod_csv, test_csv, out_xlsx=out_xlsx)