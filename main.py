"""
部材・補材管理システム — FastAPI バックエンド
Google Sheets をリアルタイムDBとして使用
Render Web Service にデプロイする構成
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
import gspread
from fastapi import Depends, FastAPI, HTTPException, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.security import OAuth2PasswordBearer
from google.oauth2.service_account import Credentials
from jose import JWTError, jwt
from passlib.context import CryptContext
from pydantic import BaseModel

# ══════════════════════════════════════════════════
# 環境変数
# ══════════════════════════════════════════════════
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]
GOOGLE_WORKSHEET = os.getenv("GOOGLE_WORKSHEET", "在庫")
SECRET_KEY       = os.environ["SECRET_KEY"]
ALGORITHM        = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7
GOOGLE_VISION_API_KEY = os.environ["GOOGLE_VISION_API_KEY"]  # ← 追加

CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON")

# ══════════════════════════════════════════════════
# Google Sheets 接続
# ══════════════════════════════════════════════════
SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

def get_gspread_client() -> gspread.Client:
    if CREDENTIALS_JSON:
        info = json.loads(CREDENTIALS_JSON)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
    return gspread.authorize(creds)

def open_sheet(sheet_name: str) -> gspread.Worksheet:
    gc = get_gspread_client()
    sh = gc.open_by_key(GOOGLE_SHEET_ID)
    try:
        return sh.worksheet(sheet_name)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=sheet_name, rows=1000, cols=20)
        return ws

# ──────────────────────────────────────────────────
# シート名定数
# ──────────────────────────────────────────────────
WS_STOCK   = os.getenv("GOOGLE_WORKSHEET", "在庫")
WS_HISTORY = "history"
WS_USERS   = "ユーザー"
WS_BOGAI   = "簿外"  # シート名: 簿外

# ══════════════════════════════════════════════════
# シートヘッダー初期化（「拠点」「保管場所」を含めた新ヘッダー構成）
# ══════════════════════════════════════════════════
STOCK_HEADERS   = ["品番", "品名", "数量", "拠点", "保管場所", "更新日時"]
HISTORY_HEADERS = ["操作日時", "操作ユーザー", "品番", "品名", "数量", "区分"]
USERS_HEADERS   = ["ユーザー名", "パスワードハッシュ", "管理者"]
BOGAI_HEADERS   = ["在庫品", "区分", "状態", "数量", "更新日", "保管場所", "備考"]

def ensure_headers(ws: gspread.Worksheet, headers: List[str]) -> None:
    existing = ws.row_values(1)
    if existing != headers:
        ws.update("A1", [headers])

def init_sheets() -> None:
    try:
        for name, headers in [
            (WS_STOCK,   STOCK_HEADERS),
            (WS_HISTORY, HISTORY_HEADERS),
            (WS_USERS,   USERS_HEADERS),
            (WS_BOGAI,   BOGAI_HEADERS),
        ]:
            ws = open_sheet(name)
            ensure_headers(ws, headers)
    except Exception as e:
        print(f"[WARN] init_sheets: {e}")

# ══════════════════════════════════════════════════
# 認証ユーティリティ
# ══════════════════════════════════════════════════
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login", auto_error=False)

def hash_password(plain: str) -> str: return pwd_context.hash(plain)
def verify_password(plain: str, hashed: str) -> bool: return pwd_context.verify(plain, hashed)
def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({**data, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)
def decode_token(token: str) -> dict:
    try: return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError: raise HTTPException(status_code=401, detail="無効なトークン")
def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    if not token: raise HTTPException(status_code=401, detail="認証が必要です")
    return decode_token(token)
def get_admin_user(token: str = Depends(oauth2_scheme)) -> dict:
    user = get_current_user(token)
    if not user.get("is_admin"): raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return user

def jst_now() -> str:
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")

def append_history(user: str, part_no: str, name: str, qty: int, action: str) -> None:
    ws = open_sheet(WS_HISTORY)
    ensure_headers(ws, HISTORY_HEADERS)
    ws.append_row([jst_now(), user, part_no, name, str(qty), action])

# ══════════════════════════════════════════════════
# 在庫 (STOCK) CRUD ヘルパー
# 拠点と品番を掛け合わせた判定処理
# ══════════════════════════════════════════════════
def find_stock_row(ws: gspread.Worksheet, part_no: str, base: str) -> Optional[int]:
    rows = ws.get_all_values()
    for i, row in enumerate(rows[1:], start=2):
        if len(row) >= 4:
            # 1番目の「品番」と4番目の「拠点」を両方突き合わせ
            if row[0].strip() == part_no.strip() and row[3].strip() == base.strip():
                return i
    return None

def get_stock_item(part_no: str, base: str) -> Optional[Dict[str, Any]]:
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no, base)
    if row_no is None: return None
    row = ws.row_values(row_no)
    return {
        "part_no": row[0] if len(row)>0 else "", 
        "name": row[1] if len(row)>1 else "",
        "qty": int(row[2]) if len(row)>2 and str(row[2]).isdigit() else 0, 
        "base": row[3] if len(row)>3 else "",
        "location": row[4] if len(row)>4 else "",
        "updated_at": row[5] if len(row)>5 else "",
    }

def get_all_stock() -> List[Dict[str, Any]]:
    ws = open_sheet(WS_STOCK)
    rows = ws.get_all_values()
    if len(rows) <= 1: return []
    result = []
    
    # 5大拠点の値チェック用定義
    valid_bases = {"川口", "仙台", "郡山", "名古屋", "大阪"}
    
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        
        # 旧データ形式で列がズレてしまっている場合のセーフティ保護ロジック
        base_val = row[3].strip() if len(row) > 3 else "川口"
        if base_val not in valid_bases:
            # 4番目のカラムが拠点名でなければ、旧スプレッドシートのタイムスタンプ等の可能性があるため
            # 自動的に「川口」拠点として引き当てを救済します
            base_val = "川口"
            
        loc_val = row[4].strip() if len(row) > 4 else ""
        
        result.append({
            "part_no": row[0].strip(), 
            "name": row[1].strip() if len(row)>1 else "",
            "qty": int(row[2]) if len(row)>2 and str(row[2]).lstrip("-").isdigit() else 0,
            "base": base_val,
            "location": loc_val,
            "updated_at": row[5] if len(row)>5 else "",
        })
    return result

def upsert_stock(part_no: str, name: str, delta: int, base: str, location: str, user: str, action: str) -> Dict[str, Any]:
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no, base)
    now = jst_now()
    if row_no:
        row = ws.row_values(row_no)
        current_qty = int(row[2]) if len(row)>2 and str(row[2]).lstrip("-").isdigit() else 0
        new_qty = current_qty + delta
        if new_qty < 0: raise HTTPException(status_code=400, detail=f"在庫不足: 現在 {current_qty}, 使用 {abs(delta)}")
        # C（数量）, D（拠点）, E（保管場所）, F（更新日時）を範囲更新
        ws.update(f"C{row_no}:F{row_no}", [[str(new_qty), base, location, now]])
        final_name = row[1] if len(row)>1 else name
    else:
        if delta < 0: raise HTTPException(status_code=404, detail="該当する品番と拠点の組み合わせが見つかりません")
        new_qty = delta; final_name = name
        # 6カラム構造で追加
        ws.append_row([part_no, final_name, str(new_qty), base, location, now])

    append_history(user, part_no, f"{final_name} ({base} - {location})", abs(delta), action)
    return {"part_no": part_no, "name": final_name, "qty": new_qty, "base": base, "location": location, "updated_at": now}

def set_stock_qty(part_no: str, name: str, qty: int, base: str, location: str, user: str) -> Dict[str, Any]:
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no, base)
    now = jst_now()
    if row_no:
        ws.update(f"C{row_no}:F{row_no}", [[str(qty), base, location, now]])
        final_name = ws.cell(row_no, 2).value or name
    else:
        final_name = name
        ws.append_row([part_no, final_name, str(qty), base, location, now])
    append_history(user, part_no, f"{final_name} ({base} - {location})", qty, "edit")
    return {"part_no": part_no, "name": final_name, "qty": qty, "base": base, "location": location, "updated_at": now}

def delete_stock_row(part_no: str, base: str) -> None:
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no, base)
    if row_no is None: raise HTTPException(status_code=404, detail="対象の品番・拠点が見つかりません")
    ws.delete_rows(row_no)

def get_all_history() -> List[Dict[str, Any]]:
    ws = open_sheet(WS_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1: return []
    result = []
    for row in rows[1:]:
        if not row: continue
        result.append({
            "timestamp": row[0] if len(row)>0 else "", "user": row[1] if len(row)>1 else "",
            "part_no": row[2] if len(row)>2 else "", "name": row[3] if len(row)>3 else "",
            "qty": int(row[4]) if len(row)>4 and str(row[4]).lstrip("-").isdigit() else 0,
            "action": row[5] if len(row)>5 else "",
        })
    return list(reversed(result))


# ══════════════════════════════════════════════════
# 簿外 (BOGAI) CRUD ヘルパー 
# ══════════════════════════════════════════════════
def find_bogai_row(ws: gspread.Worksheet, item_name: str) -> Optional[int]:
    col_a = ws.col_values(1)
    for i, val in enumerate(col_a[1:], start=2):
        if str(val).strip() == str(item_name).strip(): return i
    return None

def get_all_bogai() -> List[Dict[str, Any]]:
    ws = open_sheet(WS_BOGAI)
    rows = ws.get_all_values()
    if len(rows) <= 1: return []
    result = []
    for row in rows[1:]:
        if not row or not row[0].strip(): continue
        result.append({
            "item_name": row[0].strip(),
            "category":  row[1].strip() if len(row)>1 else "",
            "condition": row[2].strip() if len(row)>2 else "",
            "qty":       int(row[3]) if len(row)>3 and str(row[3]).lstrip("-").isdigit() else 0,
            "updated_at":row[4].strip() if len(row)>4 else "",
            "location":  row[5].strip() if len(row)>5 else "",
            "remarks":   row[6].strip() if len(row)>6 else ""
        })
    return result

def upsert_bogai(item_name: str, category: str, condition: str, delta: int, location: str, remarks: str, user: str, action: str):
    ws = open_sheet(WS_BOGAI)
    row_no = find_bogai_row(ws, item_name)
    now = jst_now()
    
    if row_no:
        row = ws.row_values(row_no)
        current_qty = int(row[3]) if len(row)>3 and str(row[3]).lstrip("-").isdigit() else 0
        new_qty = current_qty + delta
        if new_qty < 0: raise HTTPException(status_code=400, detail=f"簿外在庫不足: 現在 {current_qty}, 使用 {abs(delta)}")
        
        cat = category if category else (row[1] if len(row)>1 else "")
        cond = condition if condition else (row[2] if len(row)>2 else "")
        loc = location if location else (row[5] if len(row)>5 else "")
        rem = remarks if remarks else (row[6] if len(row)>6 else "")
        ws.update(f"B{row_no}:G{row_no}", [[cat, cond, str(new_qty), now, loc, rem]])
    else:
        if delta < 0: raise HTTPException(status_code=404, detail="アイテムが見つかりません")
        ws.append_row([item_name, category, condition, str(delta), now, location, remarks])

    append_history(user, "[簿外]", item_name, abs(delta), action)
    return {"message": "Success"}

def set_bogai_fields(item_name: str, category: str, condition: str, qty: int, location: str, remarks: str, user: str) -> Dict[str, Any]:
    ws = open_sheet(WS_BOGAI)
    row_no = find_bogai_row(ws, item_name)
    now = jst_now()
    if row_no:
        ws.update(f"B{row_no}:G{row_no}", [[category, condition, str(qty), now, location, remarks]])
    else:
        ws.append_row([item_name, category, condition, str(qty), now, location, remarks])
    append_history(user, "[簿外編集]", item_name, qty, "edit")
    return {"item_name": item_name, "qty": qty}

def delete_bogai_row(item_name: str) -> None:
    ws = open_sheet(WS_BOGAI)
    row_no = find_bogai_row(ws, item_name)
    if row_no is None: raise HTTPException(status_code=404, detail="アイテムが見つかりません")
    ws.delete_rows(row_no)

# ──────────────────────────────────────────────────
# ユーザー管理
# ──────────────────────────────────────────────────
def get_all_users_raw() -> List[List[str]]:
    ws = open_sheet(WS_USERS)
    rows = ws.get_all_values()
    return rows[1:] if len(rows) > 1 else []

def find_user(username: str) -> Optional[Dict[str, Any]]:
    for row in get_all_users_raw():
        if len(row) >= 1 and row[0].strip() == username.strip():
            return {
                "username": row[0],
                "hashed_password": row[1] if len(row)>1 else "",
                "is_admin": row[2].strip().lower() in ("true", "1", "yes", "管理者") if len(row)>2 else False,
            }
    return None

def find_user_row(username: str) -> Optional[int]:
    ws = open_sheet(WS_USERS)
    col = ws.col_values(1)
    for i, val in enumerate(col[1:], start=2):
        if val.strip() == username.strip(): return i
    return None

def create_user_record(username: str, password: str, is_admin: bool) -> None:
    if find_user(username): raise HTTPException(status_code=400, detail="ユーザーは既に存在します")
    ws = open_sheet(WS_USERS)
    ensure_headers(ws, USERS_HEADERS)
    ws.append_row([username, hash_password(password), str(is_admin)])

def delete_user_record(username: str) -> None:
    ws = open_sheet(WS_USERS)
    row_no = find_user_row(username)
    if row_no is None: raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    ws.delete_rows(row_no)

def ensure_admin_user() -> None:
    try:
        if not find_user("admin"):
            ws = open_sheet(WS_USERS)
            ensure_headers(ws, USERS_HEADERS)
            ws.append_row(["admin", hash_password("admin123"), "True"])
            print("[INFO] デフォルト admin ユーザーを作成しました（PW: admin123）")
    except Exception as e: print(f"[WARN] ensure_admin_user: {e}")

# ══════════════════════════════════════════════════
# Pydantic スキーマ
# ══════════════════════════════════════════════════
class LoginRequest(BaseModel): username: str; password: str
class AddRequest(BaseModel): part_no: str; name: str; qty: int; base: str = "川口"; location: str = ""; user: str = "ゲスト"
class UseRequest(BaseModel): part_no: str; qty: int; base: str = "川口"; user: str = "ゲスト"
class MultiSearchRequest(BaseModel): part_nos: List[str]; base: str = "川口"
class AdminEditRequest(BaseModel): part_no: str; name: str; qty: int; base: str = "川口"; location: str = ""; user: str = "admin"
class UserCreateRequest(BaseModel): username: str; password: str; is_admin: bool = False

# API BOGAI Models
class BogaiAddRequest(BaseModel):
    item_name: str
    category: str = ""
    condition: str = ""
    qty: int
    location: str = ""
    remarks: str = ""
    user: str = "ゲスト"

class BogaiUseRequest(BaseModel):
    item_name: str
    qty: int
    user: str = "ゲスト"

class AdminBogaiEditRequest(BaseModel):
    item_name: str
    category: str = ""
    condition: str = ""
    qty: int
    location: str = ""
    remarks: str = ""
    user: str = "admin"

# ══════════════════════════════════════════════════
# FastAPI アプリ
# ══════════════════════════════════════════════════
app = FastAPI(title="部材・補材管理システム API")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

@app.on_event("startup")
async def startup() -> None:
    init_sheets()
    ensure_admin_user()

@app.post("/auth/login")
def login(body: LoginRequest):
    user = find_user(body.username)
    if not user or not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが違います")
    token = create_access_token({"sub": user["username"], "is_admin": user["is_admin"]})
    return {"access_token": token, "token_type": "bearer", "is_admin": user["is_admin"]}

# ── STOCK API ──
@app.get("/stock")
def list_stock(): return get_all_stock()

@app.get("/stock/search")
def search_stock(part_no: Optional[str]=None, name: Optional[str]=None, base: Optional[str]=None, fuzzy: bool=False, low: bool=False):
    all_items = get_all_stock()
    if low: return [it for it in all_items if it["qty"] <= 5]
    if part_no:
        if base:
            if fuzzy: return [it for it in all_items if part_no.lower() in it["part_no"].lower() and it["base"] == base]
            else: return get_stock_item(part_no, base)
        else:
            if fuzzy: return [it for it in all_items if part_no.lower() in it["part_no"].lower()]
            else: return get_stock_item(part_no, "川口")
    if name: 
        if base:
            return [it for it in all_items if name.lower() in it["name"].lower() and it["base"] == base]
        return [it for it in all_items if name.lower() in it["name"].lower()]
    return all_items

@app.post("/stock/multi-search")
def multi_search(body: MultiSearchRequest):
    all_items = get_all_stock()
    stock_map = {f'{it["part_no"].strip()}_{it["base"]}': it for it in all_items}
    result = []
    for pn in body.part_nos:
        pn_s = pn.strip()
        key = f"{pn_s}_{body.base}"
        if key in stock_map: 
            result.append({**stock_map[key], "found": True})
        else: 
            result.append({"part_no": pn_s, "name": "", "qty": 0, "base": body.base, "location": "", "found": False})
    return result

@app.post("/stock/add")
def add_stock(body: AddRequest):
    if body.qty < 1: raise HTTPException(status_code=400, detail="数量は1以上を入力してください")
    return upsert_stock(body.part_no, body.name, body.qty, body.base, body.location, body.user, "add")

@app.post("/stock/use")
def use_stock(body: UseRequest):
    if body.qty < 1: raise HTTPException(status_code=400, detail="数量は1以上を入力してください")
    return upsert_stock(body.part_no, "", -body.qty, body.base, "", body.user, "use")

# ── BOGAI API ──
@app.post("/bogai/add")
def add_bogai(body: BogaiAddRequest):
    if body.qty < 1: raise HTTPException(status_code=400, detail="数量は1以上を入力してください")
    return upsert_bogai(body.item_name, body.category, body.condition, body.qty, body.location, body.remarks, body.user, "bogai_add")

@app.get("/bogai/search")
def search_bogai(query: str = ""):
    all_items = get_all_bogai()
    if not query: return all_items
    q = query.lower()
    
    # 全フィールドの文字列結合（あいまい検索）
    results = []
    for it in all_items:
        combined_text = " ".join([
            it.get("item_name", ""),
            it.get("category", ""),
            it.get("condition", ""),
            str(it.get("qty", "")),
            it.get("updated_at", ""),
            it.get("location", ""),
            it.get("remarks", "")
        ]).lower()
        if q in combined_text:
            results.append(it)
    return results

@app.post("/bogai/use")
def use_bogai(body: BogaiUseRequest):
    if body.qty < 1: raise HTTPException(status_code=400, detail="数量は1以上を入力してください")
    return upsert_bogai(body.item_name, "", "", -body.qty, "", "", body.user, "bogai_use")

# ── STATS & HISTORY ──
@app.get("/stats")
def get_stats():
    items = get_all_stock()
    return {"total": len(items), "low_stock": sum(1 for it in items if 0 < it["qty"] <= 5), "zero_stock": sum(1 for it in items if it["qty"] <= 0)}

@app.get("/history")
def list_history(): return get_all_history()

# ── ADMIN API ──
@app.put("/admin/stock/edit")
def admin_edit_stock(body: AdminEditRequest, _=Depends(get_admin_user)):
    if body.qty < 0: raise HTTPException(status_code=400, detail="数量は0以上を入力してください")
    return set_stock_qty(body.part_no, body.name, body.qty, body.base, body.location, body.user)

@app.delete("/admin/stock/{part_no}")
def admin_delete_stock(part_no: str, base: str, _=Depends(get_admin_user)):
    delete_stock_row(part_no, base); return {"message": "Deleted"}

@app.put("/admin/bogai/edit")
def admin_edit_bogai(body: AdminBogaiEditRequest, _=Depends(get_admin_user)):
    if body.qty < 0: raise HTTPException(status_code=400, detail="数量は0以上を入力してください")
    return set_bogai_fields(body.item_name, body.category, body.condition, body.qty, body.location, body.remarks, body.user)

@app.delete("/admin/bogai/{item_name}")
def admin_delete_bogai(item_name: str, _=Depends(get_admin_user)):
    delete_bogai_row(item_name); return {"message": "Deleted"}

@app.get("/admin/export/{data_type}")
def admin_export(data_type: str, _=Depends(get_admin_user)):
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    if data_type == "stock":
        writer.writerow(STOCK_HEADERS)
        for it in get_all_stock(): writer.writerow([it["part_no"], it["name"], it["qty"], it["base"], it["location"], it["updated_at"]])
        filename = f"stock_{jst_now()[:10]}.csv"
    elif data_type == "history":
        writer.writerow(HISTORY_HEADERS)
        for h in reversed(get_all_history()): writer.writerow([h["timestamp"], h["user"], h["part_no"], h["name"], h["qty"], h["action"]])
        filename = f"history_{jst_now()[:10]}.csv"
    else: raise HTTPException(status_code=400)
    output.seek(0)
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv; charset=utf-8-sig", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

@app.get("/admin/users")
def admin_list_users(_=Depends(get_admin_user)):
    return [{"username": row[0], "is_admin": row[2].strip().lower() in ("true","1","yes","管理者") if len(row)>2 else False} for row in get_all_users_raw() if len(row)>=1 and row[0].strip()]

@app.post("/admin/users")
def admin_create_user(body: UserCreateRequest, _=Depends(get_admin_user)):
    create_user_record(body.username, body.password, body.is_admin); return {"message": "Created"}

@app.delete("/admin/users/{username}")
def admin_delete_user(username: str, _=Depends(get_admin_user)):
    if username == "admin": raise HTTPException(status_code=400)
    delete_user_record(username); return {"message": "Deleted"}

@app.get("/health")
def health(): return {"status": "ok", "time": jst_now()}
@app.get("/")
def root(): return {"message": "API", "docs": "/docs"}

# ── OCR API (Google Vision) ──
class OcrRequest(BaseModel):
    image: str  # base64 JPEG

@app.post("/ocr/label")
def ocr_label(body: OcrRequest, _=Depends(get_current_user)):
    """Google Vision APIでラベル画像から品番・品名を抽出する"""
    try:
        # Google Vision API呼び出し
        url = f"https://vision.googleapis.com/v1/images:annotate?key={GOOGLE_VISION_API_KEY}"
        payload = {
            "requests": [{
                "image": {"content": body.image},
                "features": [{"type": "TEXT_DETECTION", "maxResults": 1}]
            }]
        }
        resp = requests.post(url, json=payload, timeout=10)
        resp.raise_for_status()
        data = resp.json()

        # テキスト全体を取得
        annotations = data.get("responses", [{}])[0].get("textAnnotations", [])
        if not annotations:
            raise HTTPException(status_code=422, detail="テキストが読み取れませんでした")

        full_text = annotations[0].get("description", "")
        lines = [l.strip() for l in full_text.splitlines() if l.strip()]
        print(f"[Vision OCR] 読み取り結果:\n{full_text}")

        # 品番を抽出: 「品番」の次の行、またはN/M/K始まりのパターン
        part_no = None
        name = None

        for i, line in enumerate(lines):
            # 「品番」ラベルの次の行を品番として取得
            if re.search(r'品番|PART\s*No', line, re.IGNORECASE):
                # 同じ行に品番がある場合
                inline = re.search(r'([NMKnmk][A-Z0-9]{3,})', line, re.IGNORECASE)
                if inline:
                    part_no = inline.group(1).upper()
                # 次の行に品番がある場合
                elif i + 1 < len(lines):
                    next_line = lines[i + 1]
                    m = re.search(r'([NMKnmk][A-Z0-9]{3,})', next_line, re.IGNORECASE)
                    if m:
                        part_no = m.group(1).upper()

            # 「品名」ラベルの次の行を品名として取得
            if re.search(r'品名', line):
                inline_name = re.sub(r'品名', '', line).strip()
                if inline_name:
                    name = inline_name
                elif i + 1 < len(lines):
                    name = lines[i + 1]

        # 品番が見つからない場合: 全テキストからN/M/K始まりを探す
        if not part_no:
            for line in lines:
                m = re.search(r'\b([NMKnmk][A-Z0-9]{5,})\b', line, re.IGNORECASE)
                if m:
                    part_no = m.group(1).upper()
                    break

        if not part_no:
            raise HTTPException(status_code=422, detail="品番が読み取れませんでした")

        return {"part_no": part_no, "name": name or ""}

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCRエラー: {str(e)}")