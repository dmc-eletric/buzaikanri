import os
import json
from datetime import datetime

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel

import gspread
from google.oauth2.service_account import Credentials

# =========================================================
# Google Credentials & Connection
# =========================================================

google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

if not google_creds:
    raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON environment variable")

creds_dict = json.loads(google_creds)
CREDS = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(CREDS)

# Dán cứng ID để tránh lỗi environment trên Render
SPREADSHEET_ID = "11bi2iI5oSZJ7TwGoBio4xLEC7MNOJ8qrDre9rt3iDeI"
spreadsheet = gc.open_by_key(SPREADSHEET_ID)

try:
    materials_sheet = spreadsheet.worksheet("stock")
except:
    materials_sheet = spreadsheet.worksheet("materials")

history_sheet = spreadsheet.worksheet("history")

# =========================================================
# FastAPI Configuration
# =========================================================

app = FastAPI(title="部材管理システム", version="2.5.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# Models
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
        action, hinban, hinmei, quantity, user,
        datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ])

# =========================================================
# API Endpoints (Khớp với index.html)
# =========================================================

@app.get("/")
def serve_index():
    return FileResponse("index.html")

@app.get("/materials")
@app.get("/stock/")
def materials_list():
    # Hỗ trợ cả 2 endpoint để khớp với index.html
    return get_all_materials()

@app.post("/add")
def add_material(data: AddMaterial):
    row_index, row = find_row_by_hinban(data.hinban)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if row:
        new_qty = int(row.get("数量", 0)) + int(data.quantity)
        materials_sheet.update(range_name=f"C{row_index}:D{row_index}", values=[[new_qty, now]])
    else:
        materials_sheet.append_row([data.hinban, data.hinmei, data.quantity, now])
    add_history(data.user, "追加", data.hinban, data.hinmei, data.quantity)
    return {"ok": True}

@app.post("/use")
def use_material(data: UseMaterial):
    row_index, row = find_row_by_hinban(data.hinban)
    if not row or int(row.get("数量", 0)) < data.quantity:
        return {"ok": False, "message": "在庫不足"}
    new_qty = int(row.get("数量", 0)) - data.quantity
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    materials_sheet.update(range_name=f"C{row_index}:D{row_index}", values=[[new_qty, now]])
    add_history(data.user, "使用", data.hinban, row.get("品名", ""), data.quantity)
    return {"ok": True}

@app.get("/search/hinban/{hinban}")
def search_hinban(hinban):
    row_index, row = find_row_by_hinban(hinban)
    return {"ok": True, "data": row} if row else {"ok": False}

@app.get("/history")
def get_history():
    return history_sheet.get_all_records()
