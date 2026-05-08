import os
import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import gspread
from google.oauth2.service_account import Credentials


# =========================================================
# Google Credentials 作成
# =========================================================

google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")

creds_dict = json.loads(google_creds)

CREDS = Credentials.from_service_account_info(
    creds_dict,
    scopes=SCOPES
)

# =========================================================
# FastAPI
# =========================================================

app = FastAPI(
    title="部材管理システム",
    version="2.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# index.html 配信用
app.mount("/static", StaticFiles(directory="."), name="static")


# =========================================================
# Google Sheets 接続
# =========================================================

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

CREDS = Credentials.from_service_account_file(
    "credentials.json",
    scopes=SCOPES
)

gc = gspread.authorize(CREDS)

SPREADSHEET_ID = os.getenv("11bi2iI5oSZJ7TwGoBio4xLEC7MNOJ8qrDre9rt3iDeI")

spreadsheet = gc.open_by_key(SPREADSHEET_ID)

materials_sheet = spreadsheet.worksheet("materials")
history_sheet = spreadsheet.worksheet("history")


# =========================================================
# Request Models
# =========================================================

class AddMaterial(BaseModel):
    hinban: str
    hinmei: str
    quantity: int
    user: str


class UseMaterial(BaseModel):
    hinban: str
    quantity: int
    user: str


# =========================================================
# Helper
# =========================================================

def get_all_materials():
    return materials_sheet.get_all_records()


def find_row_by_hinban(hinban):
    records = materials_sheet.get_all_records()

    for idx, row in enumerate(records, start=2):
        if str(row["品番"]).strip() == str(hinban).strip():
            return idx, row

    return None, None


def add_history(user, action, hinban, hinmei, quantity):
    history_sheet.append_row([
        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        user,
        action,
        hinban,
        hinmei,
        quantity
    ])


# =========================================================
# Root
# =========================================================

@app.get("/")
def root():
    return {
        "status": "部材管理システム API running"
    }


# =========================================================
# 在庫一覧
# =========================================================

@app.get("/materials")
def materials():

    return get_all_materials()


# =========================================================
# 部材追加
# =========================================================

@app.post("/add")
def add_material(data: AddMaterial):

    row_index, row = find_row_by_hinban(data.hinban)

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 既存
    if row:

        new_qty = int(row["数量"]) + int(data.quantity)

        materials_sheet.update(
            f"C{row_index}:D{row_index}",
            [[new_qty, now]]
        )

    # 新規
    else:

        materials_sheet.append_row([
            data.hinban,
            data.hinmei,
            data.quantity,
            now
        ])

    add_history(
        data.user,
        "追加",
        data.hinban,
        data.hinmei,
        data.quantity
    )

    return {
        "ok": True
    }


# =========================================================
# 部材使用
# =========================================================

@app.post("/use")
def use_material(data: UseMaterial):

    row_index, row = find_row_by_hinban(data.hinban)

    if not row:
        return {
            "ok": False,
            "message": "未登録"
        }

    current_qty = int(row["数量"])

    if data.quantity > current_qty:
        return {
            "ok": False,
            "message": "在庫不足"
        }

    new_qty = current_qty - data.quantity

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    materials_sheet.update(
        f"C{row_index}:D{row_index}",
        [[new_qty, now]]
    )

    add_history(
        data.user,
        "使用",
        data.hinban,
        row["品名"],
        data.quantity
    )

    return {
        "ok": True
    }


# =========================================================
# 品番検索
# =========================================================

@app.get("/search/hinban/{hinban}")
def search_hinban(hinban):

    row_index, row = find_row_by_hinban(hinban)

    if not row:
        return {
            "ok": False,
            "message": "未登録"
        }

    return {
        "ok": True,
        "data": row
    }


# =========================================================
# 品名検索
# =========================================================

@app.get("/search/hinmei/{keyword}")
def search_hinmei(keyword):

    records = get_all_materials()

    result = []

    for row in records:

        if keyword.lower() in str(row["品名"]).lower():

            result.append(row)

    return result


# =========================================================
# 履歴取得
# =========================================================

@app.get("/history")
def history():

    return history_sheet.get_all_records()


# =========================================================
# CSV Export
# =========================================================

@app.get("/export")
def export():

    data = get_all_materials()

    return {
        "data": data
    }