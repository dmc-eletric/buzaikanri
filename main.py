import os
import json
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import io
import csv

import gspread
from google.oauth2.service_account import Credentials

# =========================================================
# Google Credentials Setup
# =========================================================
google_creds = os.getenv("GOOGLE_CREDENTIALS_JSON")
if not google_creds:
    raise RuntimeError("Missing GOOGLE_CREDENTIALS_JSON environment variable")

creds_dict = json.loads(google_creds)
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
CREDS = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
gc = gspread.authorize(CREDS)

SPREADSHEET_ID = os.getenv("GOOGLE_SHEET_ID")
if not SPREADSHEET_ID:
    raise RuntimeError("Missing GOOGLE_SHEET_ID environment variable")

spreadsheet = gc.open_by_key(SPREADSHEET_ID)
try:
    stock_sheet = spreadsheet.worksheet("stock")
    history_sheet = spreadsheet.worksheet("history")
except gspread.exceptions.WorksheetNotFound:
    raise RuntimeError("Hãy đảm bảo Google Sheets có 2 sheet tên là 'stock' và 'history'")

# =========================================================
# FastAPI App
# =========================================================
app = FastAPI(title="部材管理システム API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Có thể thay bằng ALLOWED_ORIGINS từ env để bảo mật hơn
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================================================
# Request Models
# =========================================================
class LoginReq(BaseModel):
    username: str
    password: str

class AddStockReq(BaseModel):
    part_no: str
    part_name: str
    qty: int
    operator: str

class UseStockReq(BaseModel):
    id: int  # Tương ứng với row_index trong G-Sheets
    qty: int
    operator: str

class MultiSearchReq(BaseModel):
    part_numbers: List[str]

# =========================================================
# Helpers
# =========================================================
def get_stock_data():
    records = stock_sheet.get_all_records()
    for idx, row in enumerate(records):
        row["id"] = idx + 2  # Dòng 1 là Header, nên data bắt đầu từ dòng 2
        row["part_no"] = str(row.get("品番", ""))
        row["part_name"] = str(row.get("品名", ""))
        row["qty"] = int(row.get("数量", 0) if row.get("数量") else 0)
        row["updated_at"] = str(row.get("更新日時", ""))
    return records

def find_row_by_part_no(part_no: str):
    records = get_stock_data()
    for row in records:
        if row["part_no"].strip() == str(part_no).strip():
            return row["id"], row
    return None, None

def log_history(operator, action, part_no, part_name, qty):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    history_sheet.append_row([action, part_no, part_name, qty, operator, now])

# =========================================================
# API Endpoints
# =========================================================
@app.post("/auth/login")
def login(req: LoginReq):
    # Mock login vì không dùng DB User. Admin nếu gõ ID là "admin"
    is_admin = req.username.lower() == "admin"
    return {
        "access_token": "dummy-token-123",
        "display_name": req.username,
        "is_admin": is_admin
    }

@app.get("/master/by-barcode/{code}")
def get_by_barcode(code: str):
    row_idx, row = find_row_by_part_no(code)
    if row:
        return row
    raise HTTPException(status_code=404, detail="Không tìm thấy mã này")

@app.post("/stock/add")
def add_stock(req: AddStockReq):
    row_idx, row = find_row_by_part_no(req.part_no)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    if row:
        new_qty = row["qty"] + req.qty
        stock_sheet.update(f"C{row_idx}:D{row_idx}", [[new_qty, now]])
    else:
        stock_sheet.append_row([req.part_no, req.part_name, req.qty, now])
        
    log_history(req.operator, "追加", req.part_no, req.part_name, req.qty)
    return {"status": "success"}

@app.get("/stock/")
def get_all_stock(q: str = ""):
    records = get_stock_data()
    if q:
        q = q.lower()
        records = [r for r in records if q in r["part_no"].lower() or q in r["part_name"].lower()]
    # Sắp xếp mới nhất lên đầu
    return sorted(records, key=lambda x: str(x.get("updated_at", "")), reverse=True)

@app.get("/stock/search")
def search_stock(part_no: str = "", part_name: str = "", barcode: str = ""):
    records = get_stock_data()
    result = []
    for r in records:
        if part_no and part_no.lower() in r["part_no"].lower():
            result.append(r)
        elif part_name and part_name.lower() in r["part_name"].lower():
            result.append(r)
        elif barcode and barcode.lower() == r["part_no"].lower():
            result.append(r)
    return result

@app.post("/stock/multi-search")
def multi_search(req: MultiSearchReq):
    records = get_stock_data()
    # Tạo dictionary để tra cứu siêu nhanh
    stock_dict = {r["part_no"]: r for r in records} 
    
    results = []
    for pn in req.part_numbers:
        if pn in stock_dict:
            item = stock_dict[pn]
            results.append({
                "part_no": item["part_no"],
                "part_name": item["part_name"],
                "qty": item["qty"],
                "exists": True
            })
        else:
            results.append({
                "part_no": pn,
                "part_name": "",
                "qty": 0,
                "exists": False
            })
    return results

@app.put("/stock/use")
def use_stock(req: UseStockReq):
    # Fetch lại để đảm bảo tính realtime chính xác nhất lúc trừ kho
    records = get_stock_data()
    row = next((r for r in records if r["id"] == req.id), None)
    
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy dữ liệu")
        
    if req.qty > row["qty"]:
        raise HTTPException(status_code=400, detail="Vượt quá số lượng tồn kho")
        
    new_qty = row["qty"] - req.qty
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stock_sheet.update(f"C{req.id}:D{req.id}", [[new_qty, now]])
    
    log_history(req.operator, "使用", row["part_no"], row["part_name"], req.qty)
    return {"status": "success", "new_qty": new_qty}

@app.put("/stock/adjust")
def adjust_stock(req: UseStockReq):
    # Admin set cứng tồn kho mới
    records = get_stock_data()
    row = next((r for r in records if r["id"] == req.id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Không tìm thấy dữ liệu")
        
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    stock_sheet.update(f"C{req.id}:D{req.id}", [[req.qty, now]])
    log_history(req.operator, "管理者調整", row["part_no"], row["part_name"], req.qty)
    return {"status": "success"}

@app.get("/history/")
def get_history():
    records = history_sheet.get_all_records()
    result = []
    for r in reversed(records): # Đảo ngược để lịch sử mới nhất lên đầu
        result.append({
            "action": r.get("操作", ""),
            "part_no": r.get("品番", ""),
            "part_name": r.get("品名", ""),
            "qty": r.get("数量", ""),
            "operator": r.get("作業者", ""),
            "created_at": r.get("時間", "")
        })
    return result

@app.get("/export/csv")
def export_csv():
    records = stock_sheet.get_all_records()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["品番", "品名", "数量", "更新日時"])
    for r in records:
        writer.writerow([r.get("品番", ""), r.get("品名", ""), r.get("数量", 0), r.get("更新日時", "")])
    
    output.seek(0)
    response = StreamingResponse(iter([output.getvalue()]), media_type="text/csv")
    response.headers["Content-Disposition"] = "attachment; filename=stock_export.csv"
    return response

# =========================================================
# Serve Frontend
# =========================================================
@app.get("/")
def read_root():
    # Tự động serve file index.html cùng thư mục
    with open("index.html", "r", encoding="utf-8") as f:
        html_content = f.read()
    return HTMLResponse(content=html_content)