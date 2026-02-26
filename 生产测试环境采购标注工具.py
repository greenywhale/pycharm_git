# -*- coding: utf-8 -*-
"""
Created on Wed Feb 25 09:41:02 2026

@author: 32197
"""

import os
import pandas as pd

# 把生产环境、测试环境放在同一文件夹，选择对应路径
BASE_DIR = r'C:\Users\32197\Desktop\ljq\数据源表目录'
os.chdir(BASE_DIR)


# 业务数据库名供应商中文映射
DB_TO_VENDOR = {
    "DF": "蝶蜂数据",
    "CSCS": "中证信用",
    "CSCS_CORE": "中证信用",
    "JYDB": "恒生聚源",
    "NEWWIND": "万得数据",
    "ZYYX": "朝阳永续",
    "ZYYX_SMJJ3": "朝阳永续"
}


def read_purchase_xlsx(path: str, sheet_name=None) -> pd.DataFrame:
    df = pd.read_excel(
        path,
        sheet_name=sheet_name,
        usecols=["数据供应商", "采购状态", "产品英文名称（大写）"],
        dtype=str
    )
    
    df = df.drop_duplicates(subset=["数据供应商", "产品英文名称（大写）"], keep="first")

    return df


def read_catalog_csv_full(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, encoding="utf-8-sig", dtype=str)

    return df


def mark_purchase_status_on_field_catalog(
    purchase_xlsx: str,
    prod_catalog_csv: str,
    test_catalog_csv: str,
    prod_out_csv: str,
    test_out_csv: str,
    purchase_sheet=None
) -> None:
    purchase = read_purchase_xlsx(purchase_xlsx, sheet_name="采购清单")
    prod_full = read_catalog_csv_full(prod_catalog_csv)
    test_full = read_catalog_csv_full(test_catalog_csv)

    # 字段目录：把业务数据库名映射成“数据供应商”
    prod_full = prod_full.copy()
    test_full = test_full.copy()
    prod_full["数据供应商"] = prod_full["业务数据库名"].map(DB_TO_VENDOR)
    test_full["数据供应商"] = test_full["业务数据库名"].map(DB_TO_VENDOR)

    purchase_small = purchase[["数据供应商", "采购状态", "产品英文名称（大写）"]]

    prod_marked = prod_full.merge(
        purchase_small,
        how="left",
        left_on=["数据供应商", "表英文名"],
        right_on=["数据供应商", "产品英文名称（大写）"],
    ).drop(columns=["产品英文名称（大写）"])

    # TEST：on (数据供应商, 表名)
    test_marked = test_full.merge(
        purchase_small,
        how="left",
        left_on=["数据供应商", "表英文名"],
        right_on=["数据供应商", "产品英文名称（大写）"],
    ).drop(columns=["产品英文名称（大写）"])

    # 输出两个 CSV（保留字段目录全量列 + 新增“数据供应商”“采购状态”）
    prod_marked.to_csv(prod_out_csv, index=False, encoding="utf-8-sig")
    test_marked.to_csv(test_out_csv, index=False, encoding="utf-8-sig")

    print("PROD输出:", os.path.abspath(prod_out_csv), "行数:", len(prod_marked))
    print("TEST输出:", os.path.abspath(test_out_csv), "行数:", len(test_marked))


if __name__ == "__main__":
    purchase_xlsx = os.path.join(BASE_DIR, "采购清单-总.xlsx")
    prod_catalog_csv = os.path.join(BASE_DIR, "生产环境.csv")
    test_catalog_csv = os.path.join(BASE_DIR, "测试环境.csv")

    prod_out_csv = os.path.join(BASE_DIR, "生产环境_采购标注.csv")
    test_out_csv = os.path.join(BASE_DIR, "测试环境_采购标注.csv")

    mark_purchase_status_on_field_catalog(
        purchase_xlsx=purchase_xlsx,
        prod_catalog_csv=prod_catalog_csv,
        test_catalog_csv=test_catalog_csv,
        prod_out_csv=prod_out_csv,
        test_out_csv=test_out_csv,
        purchase_sheet=None
    )