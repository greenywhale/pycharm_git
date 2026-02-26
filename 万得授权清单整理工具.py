# -*- coding: utf-8 -*-
"""
Created on Wed Feb 25 16:22:41 2026

@author: 32197
"""

import re
import pandas as pd


# 配置区
# 主表和输出表
main_file = "万得授权清单.xlsx"
output_file = "万得授权清单_整理.xlsx"
sheet_main = "Sheet1"

# 表中文名字典
table_dict_file = "万得数据字典.xlsx"
sheet_table_dict = "表"

# 用户中文名字典
user_dict_file = "万得授权清单.xlsx"
sheet_user_dict = "role&user"


SPLIT_PATTERN = re.compile(r"[,，]+")


# 统一清洗文本列
def _normalize_text_series(series):
    return series.fillna("").astype(str).str.strip()


# 仅空值转空字符串，保留原有文本格式
def _preserve_text_series(series):
    return series.fillna("").astype(str)


# 将role/user字段按中英文逗号拆分为列表
def _split_accounts(text):
    text = text.strip()
    if not text:
        return []
    return [item.strip() for item in SPLIT_PATTERN.split(text) if item.strip()]


# 合并role与user列表并按出现顺序去重，得到单一账号列表
def _merge_accounts(role_list, user_list):
    merged = []
    seen = set()
    for account in role_list + user_list:
        if account and account not in seen:
            seen.add(account)
            merged.append(account)
    return merged


# 构建英文表名到中文表名的映射
def _build_table_map():
    df_table_dict = pd.read_excel(table_dict_file, sheet_name=sheet_table_dict)
    df_table_dict.columns = [str(c).strip() for c in df_table_dict.columns]
    df_table_dict["表英文名"] = _normalize_text_series(df_table_dict["表英文名"]).str.upper()
    df_table_dict["表中文名"] = _normalize_text_series(df_table_dict["表中文名"])
    return (
        df_table_dict[["表英文名", "表中文名"]]
        .drop_duplicates(subset=["表英文名"], keep="first")
        .set_index("表英文名")["表中文名"]
        .to_dict()
    )


# 构建账号到用户中文名的映射
def _build_name_map():
    df_user_dict = pd.read_excel(user_dict_file, sheet_name=sheet_user_dict)
    df_user_dict.columns = [str(c).strip() for c in df_user_dict.columns]
    df_user_dict["Roles/user"] = _normalize_text_series(df_user_dict["Roles/user"])
    df_user_dict["用户中文名"] = _normalize_text_series(df_user_dict["用户中文名"])
    return (
        df_user_dict[["Roles/user", "用户中文名"]]
        .drop_duplicates(subset=["Roles/user"], keep="first")
        .set_index("Roles/user")["用户中文名"]
        .to_dict()
    )


# 主流程
def main():
    table_map = _build_table_map()
    name_map = _build_name_map()

    # 读取主表并清洗仅需字段
    df = pd.read_excel(main_file, sheet_name=sheet_main)
    df.columns = [str(c).strip() for c in df.columns]

    required_cols = ["Resources", "Roles", "Users", "Accesses"]
    missing_cols = [col for col in required_cols if col not in df.columns]
    if missing_cols:
        raise KeyError(f"主表缺少必要列: {missing_cols}")

    # Resources输出保留原格式；解析时使用清洗后的版本
    resources = _preserve_text_series(df["Resources"])
    resources_for_parse = resources.str.strip()
    roles_text = _normalize_text_series(df["Roles"])
    users_text = _normalize_text_series(df["Users"])
    accesses = _normalize_text_series(df["Accesses"])

    db_name = resources_for_parse.str.extract(r"database=\[(.*?)\]", flags=re.IGNORECASE, expand=False).fillna("")
    table_name = resources_for_parse.str.extract(r"table=\[(.*?)\]", flags=re.IGNORECASE, expand=False).fillna("").str.strip()
    en_table_name = table_name.str.upper().str.removeprefix("NEWWIND_")
    cn_table_name = en_table_name.map(table_map).fillna("")

    role_lists = roles_text.apply(_split_accounts)
    user_lists = users_text.apply(_split_accounts)

    max_roles = int(role_lists.str.len().max()) if len(role_lists) else 0
    max_users = int(user_lists.str.len().max()) if len(user_lists) else 0
    merged_account_lists = role_lists.combine(user_lists, _merge_accounts)
    max_role_user = int(merged_account_lists.str.len().max()) if len(merged_account_lists) else 0

    # 空列表补空字符串，保证无role/user时仍能保留原记录
    merged_for_expand = merged_account_lists.apply(lambda x: x if x else [""])

    result = pd.DataFrame(
        {
            "Resources": resources,
            "库名": db_name,
            "表名": table_name,
            "英文表名": en_table_name,
            "中文表名": cn_table_name,
            "role/user": merged_for_expand,
            "accesses": accesses,
        }
    )

    # 将role和user并集账号展开为多行
    result = result.explode("role/user", ignore_index=True)
    result["role/user"] = _normalize_text_series(result["role/user"])
    result["用户中文名"] = result["role/user"].map(name_map).fillna("")

    final_cols = [
        "Resources",
        "库名",
        "表名",
        "英文表名",
        "中文表名",
        "role/user",
        "用户中文名",
        "accesses",
    ]
    result = result.reindex(columns=final_cols, fill_value="")

    # 输出
    result.to_excel(output_file, index=False)

    print("处理完成：", output_file)
    print(f"最大角色数 max_roles = {max_roles}, 最大用户数 max_users = {max_users}")
    print(f"单行最大role/user并集数 max_role_user = {max_role_user}")
    print("输出行数：", len(result))


if __name__ == "__main__":
    main()
