import os
import sqlite3
import uuid
import json
import base64
import io
import re
import hashlib
import zipfile
import tempfile
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import dropbox
from dropbox.files import WriteMode
from google import genai
from google.genai import types
from pypdf import PdfReader
import fitz

# -------------------- Page Configuration --------------------
st.set_page_config(
    page_title="MIDO ERP - Gemini Powered",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded"
)

DB_NAME = "mido_database.db"
UPLOAD_DIR = Path("mido_files")
UPLOAD_DIR.mkdir(exist_ok=True)

# -------------------- Dropbox Storage --------------------
def _secret(section, name, default=""):
    try:
        return st.secrets.get(section, {}).get(name, default)
    except Exception:
        return default

def get_dropbox_client():
    app_key = _secret("dropbox", "app_key")
    app_secret = _secret("dropbox", "app_secret")
    refresh_token = _secret("dropbox", "refresh_token")
    access_token = _secret("dropbox", "access_token")

    if app_key and app_secret and refresh_token:
        return dropbox.Dropbox(
            oauth2_refresh_token=refresh_token,
            app_key=app_key,
            app_secret=app_secret,
            timeout=60,
        )
    if access_token:
        return dropbox.Dropbox(access_token, timeout=60)
    return None

def dropbox_root():
    root = (_secret("dropbox", "root_folder", "/MIDO") or "/MIDO").strip()
    if not root.startswith("/"):
        root = "/" + root
    return root.rstrip("/") or "/MIDO"

def dropbox_ready():
    return get_dropbox_client() is not None

def safe_path_part(value):
    value = str(value or "Unknown").strip() or "Unknown"
    for ch in ["/", "\\", "\0"]:
        value = value.replace(ch, "-")
    return value[:120]

def ensure_dropbox_folder(path):
    dbx = get_dropbox_client()
    if not dbx:
        raise RuntimeError("Dropbox غير مربوط بعد. أضف مفاتيح Dropbox داخل Streamlit Secrets.")
    try:
        dbx.files_create_folder_v2(path)
    except dropbox.exceptions.ApiError as e:
        if "conflict" not in str(e).lower():
            raise

def upload_bytes_to_dropbox(data: bytes, remote_path: str):
    dbx = get_dropbox_client()
    if not dbx:
        raise RuntimeError("Dropbox غير مربوط بعد.")
    parts = remote_path.strip("/").split("/")[:-1]
    current = ""
    for part in parts:
        current += "/" + part
        ensure_dropbox_folder(current)
    dbx.files_upload(data, remote_path, mode=WriteMode.overwrite, mute=True)
    return remote_path

def download_bytes_from_dropbox(remote_path: str):
    dbx = get_dropbox_client()
    if not dbx:
        raise RuntimeError("Dropbox غير مربوط بعد.")
    _meta, response = dbx.files_download(remote_path)
    return response.content

def backup_database_to_dropbox():
    if not dropbox_ready() or not Path(DB_NAME).exists():
        return
    try:
        data = Path(DB_NAME).read_bytes()
        upload_bytes_to_dropbox(data, f"{dropbox_root()}/System/mido_database.db")
        hour_key = datetime.now().strftime("%Y%m%d_%H")
        if st.session_state.get("_mido_backup_hour") != hour_key:
            snap = f"{dropbox_root()}/System/Backups/mido_database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            upload_bytes_to_dropbox(data, snap)
            st.session_state["_mido_backup_hour"] = hour_key
    except Exception:
        pass

def restore_database_from_dropbox_if_needed():
    if Path(DB_NAME).exists() or not dropbox_ready():
        return
    try:
        remote = f"{dropbox_root()}/System/mido_database.db"
        Path(DB_NAME).write_bytes(download_bytes_from_dropbox(remote))
    except Exception:
        pass

restore_database_from_dropbox_if_needed()

# -------------------- Styling --------------------
st.markdown(
    """
    <style>
    .block-container {padding-top: 1.4rem; padding-bottom: 2rem;}
    .mido-hero {background:linear-gradient(135deg,#0b132b,#24375f); color:white; padding:24px; border-radius:18px; margin-bottom:18px;}
    .mido-hero h2 {margin:0 0 8px 0;}
    .soft-card {background:#ffffff; border:1px solid #e7ebf2; border-radius:14px; padding:16px; margin-bottom:12px;}
    .muted {color:#6b7280; font-size:0.92rem;}
    .small {font-size:0.85rem;}
    div[data-testid="stMetric"] {background:white; border:1px solid #e7ebf2; border-radius:14px; padding:14px;}
    </style>
    """,
    unsafe_allow_html=True,
)

# -------------------- Database Setup --------------------
def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M")

def get_conn():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def table_columns(conn, table_name):
    try:
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()}
    except Exception:
        return set()

def ensure_columns(conn, table_name, columns):
    existing = table_columns(conn, table_name)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}")

def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT NOT NULL,
            country TEXT DEFAULT 'China',
            city TEXT,
            contact_person TEXT,
            phone TEXT,
            whatsapp TEXT,
            email TEXT,
            website TEXT,
            brands TEXT,
            payment_terms TEXT,
            notes TEXT,
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS bank_accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            bank_name TEXT,
            beneficiary_name TEXT,
            account_number TEXT,
            iban TEXT,
            swift TEXT,
            bank_address TEXT,
            currency TEXT DEFAULT 'USD',
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            order_number TEXT,
            order_date TEXT,
            product_summary TEXT,
            quantity REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            total_amount REAL DEFAULT 0,
            paid_amount REAL DEFAULT 0,
            status TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS invoices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            order_id INTEGER,
            invoice_number TEXT,
            invoice_date TEXT,
            due_date TEXT,
            currency TEXT DEFAULT 'USD',
            amount REAL DEFAULT 0,
            status TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            order_id INTEGER,
            invoice_id INTEGER,
            bank_account_id INTEGER,
            payment_type TEXT,
            due_date TEXT,
            payment_date TEXT,
            currency TEXT DEFAULT 'USD',
            amount REAL DEFAULT 0,
            status TEXT,
            reference TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE SET NULL,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE SET NULL,
            FOREIGN KEY(bank_account_id) REFERENCES bank_accounts(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            order_id INTEGER,
            shipment_number TEXT,
            container_number TEXT,
            bl_number TEXT,
            shipping_line TEXT,
            loading_port TEXT,
            destination_port TEXT,
            etd TEXT,
            eta TEXT,
            status TEXT,
            quantity_containers INTEGER DEFAULT 1,
            tracking_url TEXT,
            received_at TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            order_id INTEGER,
            shipment_id INTEGER,
            invoice_id INTEGER,
            document_type TEXT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            upload_date TEXT NOT NULL,
            notes TEXT,
            storage_provider TEXT DEFAULT 'local',
            dropbox_path TEXT,
            file_size INTEGER DEFAULT 0,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE,
            FOREIGN KEY(order_id) REFERENCES orders(id) ON DELETE SET NULL,
            FOREIGN KEY(shipment_id) REFERENCES shipments(id) ON DELETE SET NULL,
            FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            specification TEXT,
            brand TEXT,
            quantity REAL DEFAULT 0,
            unit_price REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            incoterm TEXT,
            quote_date TEXT,
            valid_until TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS agencies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            brand_name TEXT NOT NULL,
            agency_holder TEXT,
            territory TEXT DEFAULT 'Iraq',
            exclusivity TEXT,
            start_date TEXT,
            end_date TEXT,
            notes TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS notes_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            title TEXT,
            details TEXT,
            due_date TEXT,
            priority TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE CASCADE
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS ai_ingestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            source_files TEXT,
            user_message TEXT,
            analysis_json TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(company_id) REFERENCES companies(id) ON DELETE SET NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS development_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            request_text TEXT NOT NULL,
            ai_plan TEXT,
            status TEXT DEFAULT 'مقترح',
            created_at TEXT NOT NULL
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        )
    """)

    conn.commit()
    conn.close()

init_db()

def fetch_df(sql, params=()):
    conn = get_conn()
    try:
        return pd.read_sql_query(sql, conn, params=params)
    finally:
        conn.close()

def execute(sql, params=()):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute(sql, params)
        conn.commit()
        last_id = cur.lastrowid
    finally:
        conn.close()
    backup_database_to_dropbox()
    return last_id

def company_options():
    df = fetch_df("SELECT id, company_name FROM companies ORDER BY company_name")
    return {f"{r.company_name} (#{int(r.id)})": int(r.id) for r in df.itertuples()}

def optional_order_options(company_id=None):
    if company_id:
        df = fetch_df("SELECT id, order_number FROM orders WHERE company_id=? ORDER BY id DESC", (company_id,))
    else:
        df = fetch_df("SELECT id, order_number FROM orders ORDER BY id DESC")
    opts = {"بدون ربط": None}
    for r in df.itertuples():
        opts[f"{r.order_number or 'طلبية'} (#{int(r.id)})"] = int(r.id)
    return opts

def optional_invoice_options(company_id=None):
    if company_id:
        df = fetch_df("SELECT id, invoice_number FROM invoices WHERE company_id=? ORDER BY id DESC", (company_id,))
    else:
        df = fetch_df("SELECT id, invoice_number FROM invoices ORDER BY id DESC")
    opts = {"بدون ربط": None}
    for r in df.itertuples():
        opts[f"{r.invoice_number or 'فاتورة'} (#{int(r.id)})"] = int(r.id)
    return opts

def optional_shipment_options(company_id=None):
    if company_id:
        df = fetch_df("SELECT id, container_number, shipment_number FROM shipments WHERE company_id=? ORDER BY id DESC", (company_id,))
    else:
        df = fetch_df("SELECT id, container_number, shipment_number FROM shipments ORDER BY id DESC")
    opts = {"بدون ربط": None}
    for r in df.itertuples():
        label = r.container_number or r.shipment_number or "شحنة"
        opts[f"{label} (#{int(r.id)})"] = int(r.id)
    return opts

def optional_bank_options(company_id=None):
    if company_id:
        df = fetch_df("SELECT id, bank_name, beneficiary_name FROM bank_accounts WHERE company_id=? ORDER BY id DESC", (company_id,))
    else:
        df = fetch_df("SELECT id, bank_name, beneficiary_name FROM bank_accounts ORDER BY id DESC")
    opts = {"بدون ربط": None}
    for r in df.itertuples():
        opts[f"{r.bank_name or 'Bank'} - {r.beneficiary_name or ''} (#{int(r.id)})"] = int(r.id)
    return opts

def save_uploaded_file(uploaded_file, company_id, document_type="Other"):
    if not dropbox_ready():
        raise RuntimeError("Dropbox غير مربوط. أضف بيانات Dropbox أولاً.")
    company_df = fetch_df("SELECT company_name FROM companies WHERE id=?", (company_id,))
    company_name = company_df.iloc[0]["company_name"] if not company_df.empty else f"Company_{company_id}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_path_part(uploaded_file.name)}"
    remote_path = (
        f"{dropbox_root()}/Companies/{safe_path_part(company_name)}/"
        f"Documents/{safe_path_part(document_type)}/{safe_name}"
    )
    upload_bytes_to_dropbox(uploaded_file.getvalue(), remote_path)
    return remote_path

def save_uploaded_file_for_shipment(uploaded_file, company_id, shipment_id, document_type="Other"):
    if not dropbox_ready():
        raise RuntimeError("Dropbox غير مربوط.")
    company_df = fetch_df("SELECT company_name FROM companies WHERE id=?", (company_id,))
    company_name = company_df.iloc[0]["company_name"] if not company_df.empty else f"Company_{company_id}"
    ship_df = fetch_df("SELECT shipment_number,container_number,bl_number FROM shipments WHERE id=?", (shipment_id,))
    if ship_df.empty:
        ship_label = f"Shipment_{shipment_id}"
    else:
        r = ship_df.iloc[0]
        ship_label = r.get("container_number") or r.get("bl_number") or r.get("shipment_number") or f"Shipment_{shipment_id}"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = f"{timestamp}_{uuid.uuid4().hex[:8]}_{safe_path_part(uploaded_file.name)}"
    remote_path = (
        f"{dropbox_root()}/Companies/{safe_path_part(company_name)}/Shipments/"
        f"{safe_path_part(ship_label)}/{safe_path_part(document_type)}/{safe_name}"
    )
    upload_bytes_to_dropbox(uploaded_file.getvalue(), remote_path)
    return remote_path

def infer_document_type(filename):
    n = (filename or "").lower()
    rules = [
        (("packing", "p/list", "packinglist"), "Packing List"),
        (("bill of lading", "b/l", "_bl", " bl", "bol"), "Bill of Lading"),
        (("certificate of origin", "origin", "coo"), "Certificate of Origin"),
        (("commercial invoice", "invoice", "inv", "pi"), "Commercial Invoice"),
        (("qr", "qrcode", "qr_code"), "QR Code"),
        (("payment", "bank slip", "swift", "tt copy"), "Payment Proof"),
        (("price", "quotation", "quote"), "Price List"),
        (("contract", "agreement"), "Contract"),
    ]
    for keys, dtype in rules:
        if any(k in n for k in keys):
            return dtype
    return "Other"

def build_shipment_package(shipment_id):
    ship = fetch_df("""SELECT s.*,c.company_name FROM shipments s JOIN companies c ON c.id=s.company_id WHERE s.id=?""", (shipment_id,))
    if ship.empty:
        raise ValueError("الشحنة غير موجودة.")
    docs = fetch_df("SELECT * FROM documents WHERE shipment_id=? ORDER BY id", (shipment_id,))
    if docs.empty:
        raise ValueError("لا توجد مستندات مرتبطة بهذه الشحنة بعد.")
    sr = ship.iloc[0]
    ship_label = sr.get("container_number") or sr.get("bl_number") or sr.get("shipment_number") or f"Shipment_{shipment_id}"
    company_name = sr.get("company_name") or "Unknown"
    bio = io.BytesIO()
    manifest = []
    with zipfile.ZipFile(bio, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for row in docs.itertuples():
            try:
                data = get_document_bytes(row)
            except Exception as e:
                manifest.append(f"MISSING: {getattr(row,'file_name','')} — {e}")
                continue
            dtype = safe_path_part(getattr(row, "document_type", None) or "Other")
            fname = safe_path_part(getattr(row, "file_name", None) or f"document_{getattr(row,'id','')}")
            z.writestr(f"{dtype}/{fname}", data)
            manifest.append(f"{dtype}: {fname}")
        summary = [
            f"MIDO Shipment Package",
            f"Company: {company_name}",
            f"Shipment: {ship_label}",
            f"Container: {sr.get('container_number') or ''}",
            f"BL: {sr.get('bl_number') or ''}",
            f"ETA: {sr.get('eta') or ''}",
            "",
            "Documents:", *manifest,
        ]
        z.writestr("MIDO_MANIFEST.txt", "\n".join(summary).encode("utf-8"))
    package_bytes = bio.getvalue()
    remote = (
        f"{dropbox_root()}/Companies/{safe_path_part(company_name)}/Shipments/"
        f"{safe_path_part(ship_label)}/Shipment_Packages/"
        f"{safe_path_part(ship_label)}_MIDO_PACKAGE.zip"
    )
    upload_bytes_to_dropbox(package_bytes, remote)
    return package_bytes, remote

def get_document_bytes(row):
    provider = getattr(row, "storage_provider", None) or "local"
    dbx_path = getattr(row, "dropbox_path", None)
    file_path = getattr(row, "file_path", None)
    if provider == "dropbox" or dbx_path:
        return download_bytes_from_dropbox(dbx_path or file_path)
    if file_path and os.path.exists(file_path):
        return Path(file_path).read_bytes()
    raise FileNotFoundError("الملف غير موجود في التخزين.")

def money(v, currency="USD"):
    try:
        return f"{float(v):,.2f} {currency}"
    except Exception:
        return f"0.00 {currency}"

# -------------------- Gemini AI Native Client --------------------
def ai_ready():
    return bool(_secret("ai", "api_key"))

def get_gemini_client():
    api_key = _secret("ai", "api_key")
    if not api_key:
        return None
    return genai.Client(api_key=api_key)

def get_gemini_model():
    return _secret("ai", "model", "gemini-2.5-flash")

def _clean_json_text(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end+1]
    return text

def _file_to_gemini_part(file_record):
    name = file_record["name"]
    mime = file_record.get("type") or "application/octet-stream"
    data = file_record["bytes"]

    if mime.startswith("image/") or mime in ["application/pdf", "audio/wav", "audio/mp3", "audio/ogg"]:
        return types.Part.from_bytes(data=data, mime_type=mime)
    
    # Text or structured fallback
    try:
        txt = data.decode("utf-8", errors="ignore")[:50000]
        return types.Part.from_text(text=f"Content of {name}:\n{txt}")
    except Exception:
        return types.Part.from_text(text=f"Filename: {name}")

def analyze_business_files(file_records, user_message=""):
    client = get_gemini_client()
    if not client:
        raise RuntimeError("مفتاح Gemini AI غير موجود داخل Streamlit Secrets.")
    
    companies = fetch_df("SELECT id,company_name,brands FROM companies ORDER BY company_name")
    known = companies.to_dict("records") if not companies.empty else []

    system_prompt = """You are MIDO, an ERP document analyst for an import/export business.
Analyze the user's business documents and message. Extract facts only; never invent missing values.
Match the company to known companies when possible. Dates must be YYYY-MM-DD when confidently known. Numbers must be plain numbers, no commas or symbols.
Classify EACH uploaded document separately and put each filename in documents[]. Use these types: Proforma Invoice, Commercial Invoice, Packing List, Bill of Lading, Certificate of Origin, QR Code, Payment Proof, Price List, Contract, Bank Details, Insurance, Customs, Other.
Return ONLY one valid JSON object using this schema:
{
  "summary":"short Arabic summary",
  "company":{"name":"","country":"","contact_person":"","phone":"","email":"","brands":"","payment_terms":""},
  "document_type":"Other",
  "documents":[{"file_name":"","document_type":"Other","notes":""}],
  "order":{"order_number":"","order_date":"","product_summary":"","quantity":0,"currency":"USD","total_amount":0,"paid_amount":0,"status":""},
  "invoice":{"invoice_number":"","invoice_date":"","due_date":"","currency":"USD","amount":0,"status":""},
  "shipment":{"shipment_number":"","container_number":"","bl_number":"","shipping_line":"","loading_port":"","destination_port":"","etd":"","eta":"","status":"","quantity_containers":0},
  "payment":{"payment_type":"","due_date":"","payment_date":"","currency":"USD","amount":0,"status":"","reference":""},
  "bank_account":{"bank_name":"","beneficiary_name":"","account_number":"","iban":"","swift":"","bank_address":"","currency":"USD"},
  "price_quotes":[{"product_name":"","specification":"","brand":"","quantity":0,"unit_price":0,"currency":"USD","incoterm":"","quote_date":"","valid_until":""}],
  "tasks":[{"title":"","details":"","due_date":"","priority":"متوسطة"}],
  "confidence":0.0,
  "warnings":[]
}"""

    contents = [
        types.Part.from_text(text=f"User message: {user_message or 'None'}\nKnown Companies: {json.dumps(known, ensure_ascii=False)}")
    ]
    for fr in file_records:
        contents.append(_file_to_gemini_part(fr))

    response = client.models.generate_content(
        model=get_gemini_model(),
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=0.1,
            response_mime_type="application/json"
        )
    )
    
    return json.loads(_clean_json_text(response.text))

def ask_real_mido(question):
    client = get_gemini_client()
    if not client:
        raise RuntimeError("Gemini AI غير مربوط بعد.")
    
    ctx = {
        "companies": fetch_df("SELECT id,company_name,brands,payment_terms,notes FROM companies ORDER BY id DESC LIMIT 100").to_dict("records"),
        "orders": fetch_df("SELECT id,company_id,order_number,product_summary,total_amount,currency,paid_amount,status FROM orders ORDER BY id DESC LIMIT 100").to_dict("records"),
        "shipments": fetch_df("SELECT id,company_id,container_number,bl_number,destination_port,eta,status FROM shipments ORDER BY id DESC LIMIT 100").to_dict("records"),
        "payments": fetch_df("SELECT id,company_id,amount,currency,due_date,status FROM payments ORDER BY id DESC LIMIT 100").to_dict("records"),
        "prices": fetch_df("SELECT company_id,product_name,specification,unit_price,currency,quote_date FROM prices ORDER BY id DESC LIMIT 120").to_dict("records"),
    }
    
    prompt = f"""You are MIDO, the user's private business assistant.
Answer in Iraqi Arabic, concise and factual, using ONLY the supplied ERP data.
If the data is insufficient, say what is missing. Received/delivered shipments should not be described as active. Never invent values.

ERP DATA:
{json.dumps(ctx, ensure_ascii=False, default=str)}"""

    response = client.models.generate_content(
        model=get_gemini_model(),
        contents=[types.Part.from_text(text=question)],
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            temperature=0.2
        )
    )
    return response.text

def transcribe_audio_bytes(audio_bytes, filename="voice.wav"):
    client = get_gemini_client()
    if not client:
        raise RuntimeError("Gemini AI غير مربوط بعد.")

    part = types.Part.from_bytes(data=audio_bytes, mime_type="audio/wav")
    prompt = "Transcribe this audio accurately. The speaker may use Iraqi Arabic, Arabic, or English. Return ONLY the transcribed text."

    response = client.models.generate_content(
        model=get_gemini_model(),
        contents=[part, prompt],
        config=types.GenerateContentConfig(temperature=0.0)
    )
    return response.text.strip()

def render_browser_speech(text, key="mido_speech"):
    safe_text = json.dumps(str(text), ensure_ascii=False)
    html = f"""
    <div style="font-family:system-ui;direction:rtl;text-align:right">
      <button id="speak_{key}" style="padding:9px 14px;border:1px solid #bbb;border-radius:10px;background:white;cursor:pointer;font-size:15px">🔊 تشغيل الرد الصوتي</button>
      <button id="stop_{key}" style="padding:9px 14px;border:1px solid #bbb;border-radius:10px;background:white;cursor:pointer;font-size:15px;margin-right:6px">⏹ إيقاف</button>
    </div>
    <script>
      const midoText_{key} = {safe_text};
      document.getElementById("speak_{key}").onclick = function() {{
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(midoText_{key});
        u.lang = "ar-IQ";
        u.rate = 0.95;
        const voices = window.speechSynthesis.getVoices();
        const ar = voices.find(v => (v.lang || "").toLowerCase().startsWith("ar"));
        if (ar) u.voice = ar;
        window.speechSynthesis.speak(u);
      }};
      document.getElementById("stop_{key}").onclick = function() {{
        window.speechSynthesis.cancel();
      }};
    </script>
    """
    components.html(html, height=55, scrolling=False)

# -------------------- Sidebar Nav --------------------
st.sidebar.title("🤖 MIDO ERP")
st.sidebar.caption("المساعد التجاري والمالي الفائق — Gemini Native Engine")
st.sidebar.markdown("---")

menu = [
    "📊 لوحة التحكم",
    "🏭 الشركات الصينية",
    "🧾 الطلبيات",
    "💵 الفواتير",
    "🚢 الشحنات",
    "💳 الدفعات",
    "🏦 الحسابات البنكية",
    "📁 المستندات الأصلية",
    "📈 مقارنة الأسعار",
    "🤝 الوكالات",
    "📝 المتابعة والمهام",
    "🤖 ميدو AI",
    "🛡️ حالة النظام والنسخ الاحتياطية",
]
choice = st.sidebar.radio("القسم", menu)
st.sidebar.markdown("---")

if dropbox_ready():
    st.sidebar.success("☁️ Dropbox مربوط")
else:
    st.sidebar.warning("ℹ️ Dropbox غير مربوط")

if ai_ready():
    st.sidebar.success(f"🧠 Gemini AI جاهز ({get_gemini_model()})")
else:
    st.sidebar.info("🧠 يحتاج مفتاح Gemini في Secrets")

# -------------------- Main Routing --------------------
if choice == "📊 لوحة التحكم":
    st.markdown("<div class='mido-hero'><h2>مرحباً بك في نظام MIDO 👋</h2><div>إدارة شاملة وذكية للتجارة والشحن والدفعات.</div></div>", unsafe_allow_html=True)
    counts = {
        "companies": fetch_df("SELECT COUNT(*) n FROM companies").iloc[0, 0],
        "orders": fetch_df("SELECT COUNT(*) n FROM orders").iloc[0, 0],
        "shipments": fetch_df("SELECT COUNT(*) n FROM shipments WHERE COALESCE(status,'') NOT LIKE '%استلام%' AND COALESCE(status,'') NOT LIKE '%مستلمة%' AND COALESCE(status,'') NOT LIKE '%Delivered%'").iloc[0, 0],
        "docs": fetch_df("SELECT COUNT(*) n FROM documents").iloc[0, 0],
    }
    unpaid = fetch_df("SELECT COALESCE(SUM(amount),0) total FROM payments WHERE status NOT IN ('مدفوعة','تم الدفع')").iloc[0, 0]

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("الشركات", int(counts["companies"]))
    c2.metric("الطلبيات", int(counts["orders"]))
    c3.metric("الشحنات النشطة", int(counts["shipments"]))
    c4.metric("المستندات", int(counts["docs"]))
    c5.metric("دفعات غير مسددة", money(unpaid))

    st.markdown("---")
    left, right = st.columns(2)
    with left:
        st.subheader("🚢 الشحنات بالطريق")
        shp = fetch_df("""
            SELECT s.id, c.company_name AS الشركة, s.container_number AS الحاوية,
                   s.bl_number AS BL, s.destination_port AS الوجهة, s.eta AS ETA, s.status AS الحالة
            FROM shipments s JOIN companies c ON c.id=s.company_id
            WHERE COALESCE(s.status,'') NOT LIKE '%استلام%' AND COALESCE(s.status,'') NOT LIKE '%مستلمة%'
            ORDER BY CASE WHEN s.eta IS NULL OR s.eta='' THEN 1 ELSE 0 END, s.eta ASC LIMIT 10
        """)
        st.dataframe(shp, use_container_width=True, hide_index=True)

    with right:
        st.subheader("💳 الدفعات القادمة")
        pay = fetch_df("""
            SELECT p.id, c.company_name AS الشركة, p.amount AS المبلغ, p.currency AS العملة,
                   p.due_date AS الاستحقاق, p.status AS الحالة
            FROM payments p JOIN companies c ON c.id=p.company_id
            WHERE p.status NOT IN ('مدفوعة','تم الدفع')
            ORDER BY CASE WHEN p.due_date IS NULL OR p.due_date='' THEN 1 ELSE 0 END, p.due_date ASC LIMIT 10
        """)
        st.dataframe(pay, use_container_width=True, hide_index=True)

elif choice == "🤖 ميدو AI":
    st.title("🤖 ميدو AI (Powered by Gemini Engine)")
    
    tab_files, tab_chat, tab_voice = st.tabs(["📎 التحليل الذكي للمستندات", "💬 محادثة قواعد البيانات", "🎙️ المحادثة الصوتية"])

    with tab_files:
        st.markdown("### تحليل أوراق الشحن والفواتير دفعة واحدة")
        user_msg = st.text_area("ملاحظات إضافية لميدو", placeholder="مثال: يرجى استخراج الفواتير ومطابقتها مع قائمة التعبئة.")
        uploads = st.file_uploader("ارفع الملفات (PDF, الصور, Excel)", type=["pdf","png","jpg","jpeg","xlsx","csv"], accept_multiple_files=True)
        if st.button("🧠 تحليل واستخراج البيانات", type="primary", disabled=not ai_ready()):
            if uploads:
                records = [{"name": u.name, "type": u.type, "bytes": u.getvalue()} for u in uploads]
                with st.spinner("جاري التحليل بذكاء Gemini..."):
                    try:
                        res = analyze_business_files(records, user_msg)
                        st.json(res)
                    except Exception as e:
                        st.error(f"حدث خطأ في التحليل: {e}")

    with tab_chat:
        st.markdown("### اسأل ميدو عن بياناتك التجارية")
        q = st.text_input("اكتب سؤالك هنا:", placeholder="ما هي الشحنات المتوقع وصولها هذا الأسبوع؟")
        if st.button("إرسال", type="primary") and q:
            with st.spinner("جاري التفكير..."):
                answer = ask_real_mido(q)
                st.write(answer)
                render_browser_speech(answer, key="chat")

    with tab_voice:
        st.markdown("### المحادثة الصوتية المباشرة")
        audio = st.audio_input("سجل صوتك هنا")
        if audio and ai_ready():
            with st.spinner("جاري تحويل الصوت وفهمه..."):
                text = transcribe_audio_bytes(audio.getvalue())
                st.write(f"**صوتك:** {text}")
                reply = ask_real_mido(text)
                st.write(f"**ميدو:** {reply}")
                render_browser_speech(reply, key="voice")

elif choice == "🛡️ حالة النظام والنسخ الاحتياطية":
    st.title("🛡️ حالة النظام والنسخ الاحتياطية")
    c1, c2, c3 = st.columns(3)
    c1.metric("Dropbox Status", "مربوط ✅" if dropbox_ready() else "غير متصل ❌")
    c2.metric("Gemini Engine", "جاهز ✅" if ai_ready() else "غير متصل ❌")
    c3.metric("Database Size", f"{Path(DB_NAME).stat().st_size/1024:.1f} KB" if Path(DB_NAME).exists() else "0 KB")
    
    if st.button("☁️ رفع نسخة احتياطية فورية إلى Dropbox", type="primary"):
        backup_database_to_dropbox()
        st.success("تم الحفظ والرفع بنجاح!")

    st.markdown("---")
    st.subheader("📦 الشحنات وأرشيف التنزيل")
    ships = fetch_df("""
        SELECT s.id, c.company_name AS الشركة, s.container_number AS الحاوية, s.bl_number AS BL, s.status AS الحالة
        FROM shipments s JOIN companies c ON c.id=s.company_id ORDER BY s.id DESC
    """)
    if not ships.empty:
        st.dataframe(ships, use_container_width=True, hide_index=True)
        ship_id_to_zip = st.selectbox("اختر رقم الشحنة لتصدير حزمة المستندات بالكامل (ZIP):", ships["id"].tolist())
        if st.button("⬇️ تجهيز حزمة الشحنة"):
            try:
                pkg, _ = build_shipment_package(ship_id_to_zip)
                st.download_button("تنزيل الأرشيف", data=pkg, file_name=f"Shipment_{ship_id_to_zip}_Package.zip", mime="application/zip")
            except Exception as e:
                st.error(f"خطأ في تجهيز الحزمة: {e}")
