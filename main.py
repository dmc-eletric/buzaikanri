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
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

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
GOOGLE_SHEET_ID  = os.environ["GOOGLE_SHEET_ID"]       # スプレッドシートID
GOOGLE_WORKSHEET = os.getenv("GOOGLE_WORKSHEET", "在庫")  # シート名（デフォルト: 在庫）
SECRET_KEY       = os.environ["SECRET_KEY"]             # JWT署名キー
ALGORITHM        = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7              # 7日間

# credentials.json は同ディレクトリに配置 or 環境変数 GOOGLE_CREDENTIALS_JSON で渡す
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
WS_STOCK   = GOOGLE_WORKSHEET          # 在庫シート
WS_HISTORY = "履歴"                    # 履歴シート
WS_USERS   = "ユーザー"               # ユーザーシート

# ══════════════════════════════════════════════════
# シートヘッダー初期化
# ══════════════════════════════════════════════════
STOCK_HEADERS   = ["品番", "品名", "数量", "更新日時"]
HISTORY_HEADERS = ["操作日時", "操作ユーザー", "品番", "品名", "数量", "区分"]
USERS_HEADERS   = ["ユーザー名", "パスワードハッシュ", "管理者"]

def ensure_headers(ws: gspread.Worksheet, headers: List[str]) -> None:
    existing = ws.row_values(1)
    if existing != headers:
        ws.update("A1", [headers])


def init_sheets() -> None:
    """アプリ起動時にシートとヘッダーを確認・作成する"""
    try:
        for name, headers in [
            (WS_STOCK,   STOCK_HEADERS),
            (WS_HISTORY, HISTORY_HEADERS),
            (WS_USERS,   USERS_HEADERS),
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


def hash_password(plain: str) -> str:
    return pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


def create_access_token(data: dict) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode({**data, "exp": expire}, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        raise HTTPException(status_code=401, detail="無効なトークン")


def get_current_user(token: str = Depends(oauth2_scheme)) -> dict:
    if not token:
        raise HTTPException(status_code=401, detail="認証が必要です")
    return decode_token(token)


def get_admin_user(token: str = Depends(oauth2_scheme)) -> dict:
    user = get_current_user(token)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="管理者権限が必要です")
    return user


# ══════════════════════════════════════════════════
# Google Sheets CRUD ヘルパー
# ══════════════════════════════════════════════════

def jst_now() -> str:
    """日本時間の現在時刻を文字列で返す"""
    jst = timezone(timedelta(hours=9))
    return datetime.now(jst).strftime("%Y-%m-%d %H:%M:%S")


def sheet_to_dicts(ws: gspread.Worksheet) -> List[Dict[str, Any]]:
    """シート全行を辞書リストに変換（1行目はヘッダー）"""
    records = ws.get_all_records(numericise_ignore=["all"])
    return records


def find_stock_row(ws: gspread.Worksheet, part_no: str) -> Optional[int]:
    """品番に一致する行番号（1始まり）を返す。なければ None"""
    col_a = ws.col_values(1)  # 品番カラム
    for i, val in enumerate(col_a[1:], start=2):   # 2行目から
        if str(val).strip() == str(part_no).strip():
            return i
    return None


def get_stock_item(part_no: str) -> Optional[Dict[str, Any]]:
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no)
    if row_no is None:
        return None
    row = ws.row_values(row_no)
    return {
        "part_no":    row[0] if len(row) > 0 else "",
        "name":       row[1] if len(row) > 1 else "",
        "qty":        int(row[2]) if len(row) > 2 and str(row[2]).isdigit() else 0,
        "updated_at": row[3] if len(row) > 3 else "",
    }


def get_all_stock() -> List[Dict[str, Any]]:
    ws = open_sheet(WS_STOCK)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    result = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        result.append({
            "part_no":    row[0].strip(),
            "name":       row[1].strip() if len(row) > 1 else "",
            "qty":        int(row[2]) if len(row) > 2 and str(row[2]).lstrip("-").isdigit() else 0,
            "updated_at": row[3] if len(row) > 3 else "",
        })
    return result


def upsert_stock(part_no: str, name: str, delta: int, user: str, action: str) -> Dict[str, Any]:
    """
    在庫に delta を加算（追加: +, 使用: -）
    既存品番なら更新、未登録なら新規追加
    """
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no)
    now = jst_now()

    if row_no:
        row = ws.row_values(row_no)
        current_qty = int(row[2]) if len(row) > 2 and str(row[2]).lstrip("-").isdigit() else 0
        new_qty = current_qty + delta
        if new_qty < 0:
            raise HTTPException(status_code=400, detail=f"在庫不足: 現在 {current_qty}, 使用 {abs(delta)}")
        ws.update(f"C{row_no}:D{row_no}", [[str(new_qty), now]])
        final_name = row[1] if len(row) > 1 else name
    else:
        if delta < 0:
            raise HTTPException(status_code=404, detail="品番が見つかりません")
        new_qty = delta
        final_name = name
        ws.append_row([part_no, final_name, str(new_qty), now])

    # 履歴記録
    append_history(user, part_no, final_name, abs(delta), action)
    return {"part_no": part_no, "name": final_name, "qty": new_qty, "updated_at": now}


def set_stock_qty(part_no: str, name: str, qty: int, user: str) -> Dict[str, Any]:
    """管理者による在庫数直接設定"""
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no)
    now = jst_now()

    if row_no:
        ws.update(f"C{row_no}:D{row_no}", [[str(qty), now]])
        final_name = ws.cell(row_no, 2).value or name
    else:
        final_name = name
        ws.append_row([part_no, final_name, str(qty), now])

    append_history(user, part_no, final_name, qty, "edit")
    return {"part_no": part_no, "name": final_name, "qty": qty, "updated_at": now}


def append_history(user: str, part_no: str, name: str, qty: int, action: str) -> None:
    ws = open_sheet(WS_HISTORY)
    ensure_headers(ws, HISTORY_HEADERS)
    ws.append_row([jst_now(), user, part_no, name, str(qty), action])


def delete_stock_row(part_no: str) -> None:
    ws = open_sheet(WS_STOCK)
    row_no = find_stock_row(ws, part_no)
    if row_no is None:
        raise HTTPException(status_code=404, detail="品番が見つかりません")
    ws.delete_rows(row_no)


def get_all_history() -> List[Dict[str, Any]]:
    ws = open_sheet(WS_HISTORY)
    rows = ws.get_all_values()
    if len(rows) <= 1:
        return []
    result = []
    for row in rows[1:]:
        if not row:
            continue
        result.append({
            "timestamp": row[0] if len(row) > 0 else "",
            "user":      row[1] if len(row) > 1 else "",
            "part_no":   row[2] if len(row) > 2 else "",
            "name":      row[3] if len(row) > 3 else "",
            "qty":       int(row[4]) if len(row) > 4 and str(row[4]).lstrip("-").isdigit() else 0,
            "action":    row[5] if len(row) > 5 else "",
        })
    return list(reversed(result))   # 新しい順


# ──────────────────────────────────────────────────
# ユーザーシート CRUD
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
                "hashed_password": row[1] if len(row) > 1 else "",
                "is_admin": row[2].strip().lower() in ("true", "1", "yes", "管理者") if len(row) > 2 else False,
            }
    return None


def find_user_row(username: str) -> Optional[int]:
    ws = open_sheet(WS_USERS)
    col = ws.col_values(1)
    for i, val in enumerate(col[1:], start=2):
        if val.strip() == username.strip():
            return i
    return None


def create_user_record(username: str, password: str, is_admin: bool) -> None:
    if find_user(username):
        raise HTTPException(status_code=400, detail="ユーザーは既に存在します")
    ws = open_sheet(WS_USERS)
    ensure_headers(ws, USERS_HEADERS)
    ws.append_row([username, hash_password(password), str(is_admin)])


def delete_user_record(username: str) -> None:
    ws = open_sheet(WS_USERS)
    row_no = find_user_row(username)
    if row_no is None:
        raise HTTPException(status_code=404, detail="ユーザーが見つかりません")
    ws.delete_rows(row_no)


def ensure_admin_user() -> None:
    """
    初回起動時：admin ユーザーが存在しなければ作成する
    デフォルトパスワード: admin123（本番では変更すること）
    """
    try:
        if not find_user("admin"):
            ws = open_sheet(WS_USERS)
            ensure_headers(ws, USERS_HEADERS)
            ws.append_row(["admin", hash_password("admin123"), "True"])
            print("[INFO] デフォルト admin ユーザーを作成しました（PW: admin123）")
    except Exception as e:
        print(f"[WARN] ensure_admin_user: {e}")


# ══════════════════════════════════════════════════
# Pydantic スキーマ
# ══════════════════════════════════════════════════

class LoginRequest(BaseModel):
    username: str
    password: str

class AddRequest(BaseModel):
    part_no: str
    name: str
    qty: int
    user: str = "ゲスト"

class UseRequest(BaseModel):
    part_no: str
    qty: int
    user: str = "ゲスト"

class MultiSearchRequest(BaseModel):
    part_nos: List[str]

class AdminEditRequest(BaseModel):
    part_no: str
    name: str
    qty: int
    user: str = "admin"

class UserCreateRequest(BaseModel):
    username: str
    password: str
    is_admin: bool = False


# ══════════════════════════════════════════════════
# FastAPI アプリ
# ══════════════════════════════════════════════════

app = FastAPI(
    title="部材・補材管理システム API",
    description="Google Sheets をバックエンドとした部材在庫管理 API",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup() -> None:
    init_sheets()
    ensure_admin_user()
    print("[INFO] 起動完了")


# ══════════════════════════════════════════════════
# 認証エンドポイント
# ══════════════════════════════════════════════════

@app.post("/auth/login")
def login(body: LoginRequest):
    user = find_user(body.username)
    if not user or not verify_password(body.password, user["hashed_password"]):
        raise HTTPException(status_code=401, detail="ユーザー名またはパスワードが違います")
    token = create_access_token({"sub": user["username"], "is_admin": user["is_admin"]})
    return {"access_token": token, "token_type": "bearer", "is_admin": user["is_admin"]}


# ══════════════════════════════════════════════════
# 在庫エンドポイント
# ══════════════════════════════════════════════════

@app.get("/stock")
def list_stock():
    """全在庫一覧を返す"""
    return get_all_stock()


@app.get("/stock/search")
def search_stock(
    part_no: Optional[str] = None,
    name: Optional[str] = None,
    fuzzy: bool = False,
    low: bool = False,
):
    """
    part_no: 完全一致検索（1件）または fuzzy=true で前方一致
    name: 部分一致検索（複数件）
    low: true のとき数量 ≤ 5 のみ返す
    """
    all_items = get_all_stock()

    if low:
        return [it for it in all_items if it["qty"] <= 5]

    if part_no:
        if fuzzy:
            matched = [it for it in all_items if part_no.lower() in it["part_no"].lower()]
            return matched
        else:
            item = get_stock_item(part_no)
            return item  # None or dict

    if name:
        matched = [it for it in all_items if name.lower() in it["name"].lower()]
        return matched

    return all_items


@app.post("/stock/multi-search")
def multi_search(body: MultiSearchRequest):
    """複数品番を一括検索。各品番の登録有無を返す"""
    all_items = get_all_stock()
    stock_map = {it["part_no"].strip(): it for it in all_items}
    result = []
    for pn in body.part_nos:
        pn_stripped = pn.strip()
        if pn_stripped in stock_map:
            it = stock_map[pn_stripped]
            result.append({**it, "found": True})
        else:
            result.append({"part_no": pn_stripped, "name": "", "qty": 0, "found": False})
    return result


@app.post("/stock/add")
def add_stock(body: AddRequest):
    """部材追加（既存品番は加算、未登録は新規追加）"""
    if body.qty < 1:
        raise HTTPException(status_code=400, detail="数量は1以上を入力してください")
    return upsert_stock(body.part_no, body.name, body.qty, body.user, "add")


@app.post("/stock/use")
def use_stock(body: UseRequest):
    """部材使用（在庫を減算）"""
    if body.qty < 1:
        raise HTTPException(status_code=400, detail="数量は1以上を入力してください")
    return upsert_stock(body.part_no, "", -body.qty, body.user, "use")


# ══════════════════════════════════════════════════
# 統計エンドポイント
# ══════════════════════════════════════════════════

@app.get("/stats")
def get_stats():
    items = get_all_stock()
    total     = len(items)
    low_stock = sum(1 for it in items if 0 < it["qty"] <= 5)
    zero_stock = sum(1 for it in items if it["qty"] <= 0)
    return {"total": total, "low_stock": low_stock, "zero_stock": zero_stock}


# ══════════════════════════════════════════════════
# 履歴エンドポイント
# ══════════════════════════════════════════════════

@app.get("/history")
def list_history():
    """全履歴を返す（新しい順）"""
    return get_all_history()


# ══════════════════════════════════════════════════
# 管理者エンドポイント
# ══════════════════════════════════════════════════

@app.put("/admin/stock/edit")
def admin_edit_stock(body: AdminEditRequest, _=Depends(get_admin_user)):
    """在庫数を直接設定（管理者専用）"""
    if body.qty < 0:
        raise HTTPException(status_code=400, detail="数量は0以上を入力してください")
    return set_stock_qty(body.part_no, body.name, body.qty, body.user)


@app.delete("/admin/stock/{part_no}")
def admin_delete_stock(part_no: str, _=Depends(get_admin_user)):
    """在庫データを削除（管理者専用）"""
    delete_stock_row(part_no)
    return {"message": f"{part_no} を削除しました"}


@app.get("/admin/export/{data_type}")
def admin_export(data_type: str, _=Depends(get_admin_user)):
    """
    CSVエクスポート（管理者専用）
    data_type: "stock" | "history"
    """
    output = io.StringIO()
    # BOM付きUTF-8でExcelでも文字化けしない
    output.write("\ufeff")
    writer = csv.writer(output)

    if data_type == "stock":
        writer.writerow(STOCK_HEADERS)
        for it in get_all_stock():
            writer.writerow([it["part_no"], it["name"], it["qty"], it["updated_at"]])
        filename = f"stock_{jst_now()[:10]}.csv"

    elif data_type == "history":
        writer.writerow(HISTORY_HEADERS)
        for h in reversed(get_all_history()):   # 古い順でエクスポート
            writer.writerow([h["timestamp"], h["user"], h["part_no"], h["name"], h["qty"], h["action"]])
        filename = f"history_{jst_now()[:10]}.csv"

    else:
        raise HTTPException(status_code=400, detail="data_type は stock または history を指定してください")

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv; charset=utf-8-sig",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ──────────────────────────────────────────────────
# ユーザー管理（管理者専用）
# ──────────────────────────────────────────────────

@app.get("/admin/users")
def admin_list_users(_=Depends(get_admin_user)):
    """ユーザー一覧（管理者専用）"""
    users = []
    for row in get_all_users_raw():
        if len(row) >= 1 and row[0].strip():
            users.append({
                "username": row[0],
                "is_admin": row[2].strip().lower() in ("true", "1", "yes", "管理者") if len(row) > 2 else False,
            })
    return users


@app.post("/admin/users")
def admin_create_user(body: UserCreateRequest, _=Depends(get_admin_user)):
    """ユーザー作成（管理者専用）"""
    create_user_record(body.username, body.password, body.is_admin)
    return {"message": f"{body.username} を作成しました"}


@app.delete("/admin/users/{username}")
def admin_delete_user(username: str, _=Depends(get_admin_user)):
    """ユーザー削除（管理者専用）"""
    if username == "admin":
        raise HTTPException(status_code=400, detail="admin ユーザーは削除できません")
    delete_user_record(username)
    return {"message": f"{username} を削除しました"}


# ══════════════════════════════════════════════════
# ヘルスチェック
# ══════════════════════════════════════════════════

@app.get("/health")
def health():
    return {"status": "ok", "time": jst_now()}


@app.get("/")
def root():
    return {"message": "部材・補材管理システム API", "docs": "/docs"}
