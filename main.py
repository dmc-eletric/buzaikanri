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
# Google Credentials
# =========================================================

# ĐÃ SỬA: Lấy từ biến môi trường (Render) thay vì file vật lý
google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

if not google_creds:
    # Nếu chạy local mà chưa có biến môi trường, code sẽ báo lỗi rõ ràng
    raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON environment variable")

creds_dict = json.loads(google_creds)
CREDS = Credentials.from_service_account_info(
    creds_dict,
    scopes=SCOPES
)

# Kết nối Google Sheets
gc = gspread.authorize(CREDS)

# ĐÃ SỬA: Lấy giá trị từ biến GOOGLE_SHEET_ID trên Render
# Không được dán trực tiếp ID vào hàm getenv()
SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")

if not SPREADSHEET_ID:
    raise RuntimeError("Missing GOOGLE_SHEET_ID environment variable")

spreadsheet = gc.open_by_key(SPREADSHEET_ID)

# ĐÃ SỬA: Đảm bảo tên worksheet khớp với hướng dẫn (mặc định là stock và history)
# Bạn hãy kiểm tra tên tab ở dưới cùng file Google Sheet của mình nhé
try:
    materials_sheet = spreadsheet.worksheet("stock")
except:
    materials_sheet = spreadsheet.worksheet("materials") # Fallback nếu bạn đặt là materials

history_sheet = spreadsheet.worksheet("history")


# =========================================================
# FastAPI Configuration
# =========================================================

app = FastAPI(
    title="部材管理システム",
    version="2.1.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# index.html 配信用
# Đảm bảo file index.html nằm cùng thư mục với main.py
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
    # Tìm hàng dựa trên cột "品番" (Cột A)
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
        "spreadsheet_connected": spreadsheet.title
    }


@app.get("/materials")
def materials():
    return get_all_materials()


@app.post("/add")
def add_material(data: AddMaterial):
    row_index, row = find_row_by_hinban(data.hinban)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if row:
        # Cập nhật số lượng hiện có
        current_qty = int(row.get("数量", 0))
        new_qty = current_qty + int(data.quantity)
        
        # Cập nhật cột C (Số lượng) và D (Thời gian)
        materials_sheet.update(
            range_name=f"C{row_index}:D{row_index}",
            values=[[new_qty, now]]
        )
    else:
        # Thêm mới nếu chưa có
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
        return {"ok": False, "message": "未登録 (Chưa đăng ký)"}

    current_qty = int(row.get("数量", 0))

    if data.quantity > current_qty:
        return {"ok": False, "message": "在庫不足 (Không đủ tồn kho)"}

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


@app.get("/search/hinmei/{keyword}")
def search_hinmei(keyword):
    records = get_all_materials()
    result = [row for row in records if keyword.lower() in str(row.get("品名", "")).lower()]
    return result


@app.get("/history")
def history():
    return history_sheet.get_all_records()


@app.get("/export")
def export():
    return {"data": get_all_materials()}
