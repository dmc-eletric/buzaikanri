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
# Google Credentials & Connection
# =========================================================

# 1. Lấy thông tin xác thực từ biến môi trường trên Render
google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

if not google_creds:
    # Nếu chạy trên Render mà báo lỗi này, hãy kiểm tra lại tab Environment
    # Tuy nhiên, để đảm bảo code không crash, ta sẽ kiểm tra JSON sau
    pass

try:
    creds_dict = json.loads(google_creds)
    CREDS = Credentials.from_service_account_info(
        creds_dict,
        scopes=SCOPES
    )
    # Kết nối Google Sheets
    gc = gspread.authorize(CREDS)
except Exception as e:
    # Nếu lỗi JSON hoặc không có biến môi trường
    CREDS = None
    print(f"Lỗi xác thực Google: {e}")

# 2. ĐỊNH NGHĨA TRỰC TIẾP ID FILE SHEET (Để loại bỏ hoàn toàn lỗi Missing Env)
SPREADSHEET_ID = "11bi2iI5oSZJ7TwGoBio4xLEC7MNOJ8qrDre9rt3iDeI"

try:
    spreadsheet = gc.open_by_key(SPREADSHEET_ID)
    # Mở các Worksheet (Tab)
    # Ưu tiên tab tên 'stock' (viết thường)
    try:
        materials_sheet = spreadsheet.worksheet("stock")
    except:
        materials_sheet = spreadsheet.worksheet("materials")
    
    history_sheet = spreadsheet.worksheet("history")
except Exception as e:
    print(f"Lỗi kết nối Sheet: {e}")

# =========================================================
# FastAPI Configuration
# =========================================================

app = FastAPI(
    title="部材管理システム",
    version="2.2.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Phục vụ file giao diện index.html
if os.path.exists("index.html"):
    app.mount("/static", StaticFiles(directory="."), name="static")


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
# Helper Functions
# =========================================================

def get_all_materials():
    return materials_sheet.get_all_records()


def find_row_by_hinban(hinban):
    records = materials_sheet.get_all_records()
    for idx, row in enumerate(records, start=2):
        if str(row.get("品番", "")).strip() == str(hinban).strip():
            return idx, row
    return None, None


def add_history(user, action, hinban, hinmei, quantity):
    history_sheet.append_row([
        action,
        hinban,
        hinmei,
        quantity,
        user,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ])


# =========================================================
# API Endpoints
# =========================================================

@app.get("/")
def root():
    return {
        "status": "API is running",
        "spreadsheet_id": SPREADSHEET_ID
    }


@app.get("/materials")
def materials():
    return get_all_materials()


@app.post("/add")
def add_material(data: AddMaterial):
    row_index, row = find_row_by_hinban(data.hinban)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if row:
        current_qty = int(row.get("数量", 0))
        new_qty = current_qty + int(data.quantity)
        materials_sheet.update(
            range_name=f"C{row_index}:D{row_index}",
            values=[[new_qty, now]]
        )
    else:
        materials_sheet.append_row([
            data.hinban,
            data.hinmei,
            data.quantity,
            now
        ])

    add_history(data.user, "追加", data.hinban, data.hinmei, data.quantity)
    return {"ok": True}


@app.post("/use")
def use_material(data: UseMaterial):
    row_index, row = find_row_by_hinban(data.hinban)

    if not row:
        return {"ok": False, "message": "未登録"}

    current_qty = int(row.get("数量", 0))

    if data.quantity > current_qty:
        return {"ok": False, "message": "在庫不足"}

    new_qty = current_qty - data.quantity
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    materials_sheet.update(
        range_name=f"C{row_index}:D{row_index}",
        values=[[new_qty, now]]
    )

    add_history(data.user, "使用", data.hinban, row.get("品名", ""), data.quantity)
    return {"ok": True}


@app.get("/search/hinban/{hinban}")
def search_hinban(hinban):
    row_index, row = find_row_by_hinban(hinban)
    if not row:
        return {"ok": False, "message": "未登録"}
    return {"ok": True, "data": row}


@app.get("/history")
def history():
    return history_sheet.get_all_records()
