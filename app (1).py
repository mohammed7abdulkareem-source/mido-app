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
import urllib.request
import urllib.error
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
import dropbox
from dropbox.files import WriteMode
from openai import OpenAI
from pypdf import PdfReader
import fitz

st.set_page_config(page_title="MIDO ERP", page_icon="🤖", layout="wide", initial_sidebar_state="expanded")

DB_NAME = "mido_database.db"
UPLOAD_DIR = Path("mido_files")  # legacy files from older MIDO versions
UPLOAD_DIR.mkdir(exist_ok=True)

# -------------------- Dropbox storage --------------------
def _secret(name, default=""):
    try:
        return st.secrets.get("dropbox", {}).get(name, default)
    except Exception:
        return default


def get_dropbox_client():
    """Create a Dropbox client without exposing credentials in app.py/GitHub."""
    app_key = _secret("app_key")
    app_secret = _secret("app_secret")
    refresh_token = _secret("refresh_token")
    access_token = _secret("access_token")

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
    root = (_secret("root_folder", "/MIDO") or "/MIDO").strip()
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
        # Folder already exists is safe to ignore.
        if "conflict" not in str(e).lower():
            raise


def upload_bytes_to_dropbox(data: bytes, remote_path: str):
    dbx = get_dropbox_client()
    if not dbx:
        raise RuntimeError("Dropbox غير مربوط بعد.")
    # Dropbox creates intermediate file only, so ensure parent folder exists.
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
    """Keep latest DB plus dated snapshots in Dropbox. Failures never block business saves."""
    if not dropbox_ready() or not Path(DB_NAME).exists():
        return
    try:
        data = Path(DB_NAME).read_bytes()
        upload_bytes_to_dropbox(data, f"{dropbox_root()}/System/mido_database.db")
        # at most one snapshot per hour per running instance
        hour_key = datetime.now().strftime("%Y%m%d_%H")
        if st.session_state.get("_mido_backup_hour") != hour_key:
            snap = f"{dropbox_root()}/System/Backups/mido_database_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
            upload_bytes_to_dropbox(data, snap)
            st.session_state["_mido_backup_hour"] = hour_key
    except Exception:
        pass


def restore_database_from_dropbox_if_needed():
    """On a fresh Streamlit instance, restore MIDO data from Dropbox if available."""
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

# -------------------- Database --------------------
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
    """Add missing columns safely without deleting old data."""
    existing = table_columns(conn, table_name)
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {name} {ddl}")


def migrate_legacy_suppliers(conn):
    """Import old MIDO supplier records safely even if the legacy table has a different schema."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if 'suppliers' not in tables:
        return

    conn.execute("CREATE TABLE IF NOT EXISTS legacy_import_map (supplier_id INTEGER PRIMARY KEY, company_id INTEGER, imported_at TEXT)")

    # Read whatever columns actually exist in the old suppliers table.
    legacy_cols = table_columns(conn, 'suppliers')
    rows = conn.execute("SELECT * FROM suppliers").fetchall()

    def old_value(row, name, default=''):
        if name not in legacy_cols:
            return default
        try:
            value = row[name]
        except Exception:
            return default
        return default if value is None else value

    for r in rows:
        sid = old_value(r, 'id', None)
        if sid is None:
            # Very old/partial DBs may not expose an id column; use rowid instead.
            continue

        done = conn.execute("SELECT 1 FROM legacy_import_map WHERE supplier_id=?", (sid,)).fetchone()
        if done:
            continue

        raw_name = old_value(r, 'company_name', '') or old_value(r, 'name', '')
        name = str(raw_name or f"Legacy Company {sid}").strip()

        existing = conn.execute("SELECT id FROM companies WHERE company_name=? ORDER BY id LIMIT 1", (name,)).fetchone()
        if existing:
            company_id = existing[0]
        else:
            cur = conn.execute(
                "INSERT INTO companies (company_name,country,notes,created_at) VALUES (?,?,?,?)",
                (name, 'China', 'تم استيراد هذا السجل تلقائياً من نسخة MIDO القديمة.', now_text())
            )
            company_id = cur.lastrowid

        bank = str(old_value(r, 'bank_account', '') or '')
        if bank.strip():
            conn.execute(
                "INSERT INTO bank_accounts (company_id,bank_name,account_number,notes,created_at) VALUES (?,?,?,?,?)",
                (company_id, 'Legacy bank details', bank, 'مستورد من MIDO القديم', now_text())
            )

        order_details = str(old_value(r, 'order_details', '') or '')
        try:
            total_amount = float(old_value(r, 'total_amount', 0) or 0)
        except Exception:
            total_amount = 0.0

        order_id = None
        if order_details.strip() or total_amount:
            cur = conn.execute(
                """INSERT INTO orders (company_id,order_number,order_date,product_summary,currency,total_amount,paid_amount,status,notes,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (company_id, f'LEGACY-{sid}', '', order_details, 'USD', total_amount, 0, 'مستوردة من النسخة القديمة', '', now_text())
            )
            order_id = cur.lastrowid

        pay_date = str(old_value(r, 'payment_date', '') or '')
        if pay_date:
            conn.execute(
                """INSERT INTO payments (company_id,order_id,payment_type,due_date,currency,amount,status,notes,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (company_id, order_id, 'Legacy', pay_date, 'USD', total_amount, 'مستحقة', 'مستوردة من MIDO القديم', now_text())
            )

        ship_status = str(old_value(r, 'shipment_status', '') or '')
        if ship_status:
            normalized = 'تم الاستلام' if ('استلام' in ship_status or 'بالكامل' in ship_status) else ship_status
            conn.execute(
                """INSERT INTO shipments (company_id,order_id,shipment_number,status,received_at,notes,created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (company_id, order_id, f'LEGACY-{sid}', normalized, now_text() if normalized == 'تم الاستلام' else '', 'مستوردة من MIDO القديم', now_text())
            )

        try:
            unit_price = float(old_value(r, 'unit_price', 0) or 0)
        except Exception:
            unit_price = 0.0
        if unit_price:
            conn.execute(
                "INSERT INTO prices (company_id,product_name,unit_price,currency,quote_date,notes,created_at) VALUES (?,?,?,?,?,?,?)",
                (company_id, order_details[:120] or 'Legacy item', unit_price, 'USD', '', 'مستورد من MIDO القديم', now_text())
            )

        conn.execute(
            "INSERT OR REPLACE INTO legacy_import_map (supplier_id,company_id,imported_at) VALUES (?,?,?)",
            (sid, company_id, now_text())
        )


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

    # Automatic schema migration: keeps all old data and only adds missing columns.
    ensure_columns(conn, "companies", {
        "country":"TEXT DEFAULT 'China'", "city":"TEXT", "contact_person":"TEXT", "phone":"TEXT",
        "whatsapp":"TEXT", "email":"TEXT", "website":"TEXT", "brands":"TEXT", "payment_terms":"TEXT",
        "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "bank_accounts", {
        "company_id":"INTEGER", "bank_name":"TEXT", "beneficiary_name":"TEXT", "account_number":"TEXT",
        "iban":"TEXT", "swift":"TEXT", "bank_address":"TEXT", "currency":"TEXT DEFAULT 'USD'",
        "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "orders", {
        "company_id":"INTEGER", "order_number":"TEXT", "order_date":"TEXT", "product_summary":"TEXT",
        "quantity":"REAL DEFAULT 0", "currency":"TEXT DEFAULT 'USD'", "total_amount":"REAL DEFAULT 0",
        "paid_amount":"REAL DEFAULT 0", "status":"TEXT", "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "invoices", {
        "company_id":"INTEGER", "order_id":"INTEGER", "invoice_number":"TEXT", "invoice_date":"TEXT",
        "due_date":"TEXT", "currency":"TEXT DEFAULT 'USD'", "amount":"REAL DEFAULT 0", "status":"TEXT",
        "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "payments", {
        "company_id":"INTEGER", "order_id":"INTEGER", "invoice_id":"INTEGER", "bank_account_id":"INTEGER",
        "payment_type":"TEXT", "due_date":"TEXT", "payment_date":"TEXT", "currency":"TEXT DEFAULT 'USD'",
        "amount":"REAL DEFAULT 0", "status":"TEXT", "reference":"TEXT", "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "shipments", {
        "company_id":"INTEGER", "order_id":"INTEGER", "shipment_number":"TEXT", "container_number":"TEXT",
        "bl_number":"TEXT", "shipping_line":"TEXT", "loading_port":"TEXT", "destination_port":"TEXT",
        "etd":"TEXT", "eta":"TEXT", "status":"TEXT", "quantity_containers":"INTEGER DEFAULT 1",
        "tracking_url":"TEXT", "received_at":"TEXT", "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "documents", {
        "company_id":"INTEGER", "order_id":"INTEGER", "shipment_id":"INTEGER", "invoice_id":"INTEGER",
        "document_type":"TEXT", "file_name":"TEXT", "file_path":"TEXT", "upload_date":"TEXT",
        "notes":"TEXT", "storage_provider":"TEXT DEFAULT 'local'", "dropbox_path":"TEXT", "file_size":"INTEGER DEFAULT 0"})
    ensure_columns(conn, "prices", {
        "company_id":"INTEGER", "product_name":"TEXT", "specification":"TEXT", "brand":"TEXT",
        "quantity":"REAL DEFAULT 0", "unit_price":"REAL DEFAULT 0", "currency":"TEXT DEFAULT 'USD'",
        "incoterm":"TEXT", "quote_date":"TEXT", "valid_until":"TEXT", "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "agencies", {
        "company_id":"INTEGER", "brand_name":"TEXT", "agency_holder":"TEXT", "territory":"TEXT DEFAULT 'Iraq'",
        "exclusivity":"TEXT", "start_date":"TEXT", "end_date":"TEXT", "notes":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "notes_tasks", {
        "company_id":"INTEGER", "title":"TEXT", "details":"TEXT", "due_date":"TEXT", "priority":"TEXT",
        "status":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "ai_ingestions", {
        "company_id":"INTEGER", "source_files":"TEXT", "user_message":"TEXT", "analysis_json":"TEXT",
        "status":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "development_requests", {"request_text":"TEXT", "ai_plan":"TEXT", "status":"TEXT", "created_at":"TEXT"})
    ensure_columns(conn, "chat_history", {"role":"TEXT", "message":"TEXT", "created_at":"TEXT"})

    conn.commit()
    # Import records from the earliest MIDO schema once, if present.
    migrate_legacy_suppliers(conn)
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
    """Upload the original file directly to Dropbox under MIDO/Companies/..."""
    if not dropbox_ready():
        raise RuntimeError("Dropbox غير مربوط. افتح إعدادات Streamlit Secrets وأضف بيانات Dropbox أولاً.")

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



def shipment_folder_name(shipment_id):
    df = fetch_df("""SELECT s.id,s.shipment_number,s.container_number,s.bl_number,c.company_name
                   FROM shipments s JOIN companies c ON c.id=s.company_id WHERE s.id=?""", (shipment_id,))
    if df.empty:
        return f"Shipment_{shipment_id}", f"Company_{shipment_id}", None
    r = df.iloc[0]
    label = r.get("container_number") or r.get("bl_number") or r.get("shipment_number") or f"Shipment_{shipment_id}"
    return safe_path_part(label), safe_path_part(r.get("company_name") or "Unknown"), int(r["id"])


def save_uploaded_file_for_shipment(uploaded_file, company_id, shipment_id, document_type="Other"):
    """Store one original shipment document in the shipment's own Dropbox folder."""
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
    """Create a ZIP containing every original document linked to a shipment, upload it to Dropbox, and return bytes/path."""
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


# -------------------- Real AI ingestion --------------------
def _ai_secret(name, default=""):
    try:
        return st.secrets.get("ai", {}).get(name, default)
    except Exception:
        return default


def ai_ready():
    return bool(_ai_secret("api_key") and _ai_secret("model"))


def get_ai_client():
    api_key = _ai_secret("api_key")
    model = _ai_secret("model")
    base_url = _ai_secret("base_url")
    if not api_key or not model:
        return None, None
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs), model


def _clean_json_text(text):
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end+1]
    return text


def _read_pdf(upload):
    data = upload.getvalue()
    text = ""
    try:
        reader = PdfReader(io.BytesIO(data))
        chunks = []
        for page in reader.pages[:30]:
            try:
                chunks.append(page.extract_text() or "")
            except Exception:
                pass
        text = "\n".join(chunks).strip()
    except Exception:
        text = ""
    images = []
    if len(text) < 250:
        try:
            doc = fitz.open(stream=data, filetype="pdf")
            for i in range(min(4, len(doc))):
                pix = doc[i].get_pixmap(matrix=fitz.Matrix(1.4, 1.4), alpha=False)
                png = pix.tobytes("png")
                images.append("data:image/png;base64," + base64.b64encode(png).decode())
        except Exception:
            pass
    return text[:50000], images


def _file_to_ai_parts(file_record):
    name = file_record["name"]
    mime = file_record.get("type") or "application/octet-stream"
    data = file_record["bytes"]
    parts = [{"type":"text", "text":f"\n--- FILE: {name} ({mime}) ---\n"}]
    lower = name.lower()
    if mime.startswith("image/") or lower.endswith((".png", ".jpg", ".jpeg", ".webp")):
        b64 = base64.b64encode(data).decode()
        parts.append({"type":"image_url", "image_url":{"url":f"data:{mime};base64,{b64}"}})
    elif lower.endswith(".pdf") or mime == "application/pdf":
        class F:
            def __init__(self, n, b): self.name=n; self._b=b
            def getvalue(self): return self._b
        text, images = _read_pdf(F(name, data))
        if text:
            parts.append({"type":"text", "text":"PDF extracted text:\n" + text})
        for img in images:
            parts.append({"type":"image_url", "image_url":{"url":img}})
    elif lower.endswith(".csv"):
        try:
            txt = data.decode("utf-8", errors="ignore")[:50000]
            parts.append({"type":"text", "text":"CSV content:\n" + txt})
        except Exception:
            pass
    elif lower.endswith((".xlsx", ".xls")):
        try:
            xls = pd.ExcelFile(io.BytesIO(data))
            chunks = []
            for sheet in xls.sheet_names[:10]:
                df = pd.read_excel(xls, sheet_name=sheet, nrows=300)
                chunks.append(f"SHEET: {sheet}\n" + df.to_csv(index=False))
            parts.append({"type":"text", "text":"Excel workbook content:\n" + "\n\n".join(chunks)[:70000]})
        except Exception as e:
            parts.append({"type":"text", "text":f"Excel file could not be parsed locally: {e}"})
    else:
        try:
            txt = data.decode("utf-8", errors="ignore")[:30000]
            if txt.strip():
                parts.append({"type":"text", "text":"File text:\n" + txt})
        except Exception:
            parts.append({"type":"text", "text":"Binary file; classify mainly from filename and user message."})
    return parts


def analyze_business_files(file_records, user_message=""):
    client, model = get_ai_client()
    if not client:
        raise RuntimeError("AI غير مربوط بعد. أضف [ai] api_key و model داخل Streamlit Secrets.")
    companies = fetch_df("SELECT id,company_name,brands FROM companies ORDER BY company_name")
    known = companies.to_dict("records") if not companies.empty else []
    system_prompt = """You are MIDO, an ERP document analyst for an import/export business.
Analyze the user's business documents and message. Extract facts only; never invent missing values.
Match the company to known companies when possible. Dates must be YYYY-MM-DD when confidently known. Numbers must be plain numbers, no commas or symbols.
Classify EACH uploaded document separately and put each filename in documents[]. Use these types when possible: Proforma Invoice, Commercial Invoice, Packing List, Bill of Lading, Certificate of Origin, QR Code, Payment Proof, Price List, Contract, Bank Details, Insurance, Customs, Other.
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
}
If a section is not present, use empty strings/zero/empty arrays. Confidence is 0 to 1."""
    content = [{"type":"text", "text":f"User message: {user_message or '(none)'}\nKnown companies in MIDO: {json.dumps(known, ensure_ascii=False)}"}]
    for fr in file_records:
        content.extend(_file_to_ai_parts(fr))
    response = client.chat.completions.create(
        model=model,
        messages=[{"role":"system","content":system_prompt},{"role":"user","content":content}],
        temperature=0,
    )
    raw = response.choices[0].message.content
    return json.loads(_clean_json_text(raw))


def _nonempty(v):
    return v is not None and str(v).strip() not in ("", "0", "0.0", "None")


def _f(v, default=0.0):
    try:
        return float(v or 0)
    except Exception:
        return default


def _i(v, default=0):
    try:
        return int(float(v or 0))
    except Exception:
        return default


def get_or_create_company_ai(cdata):
    name = str((cdata or {}).get("name") or "").strip()
    if not name:
        raise ValueError("AI لم يحدد اسم الشركة. اكتب اسم الشركة برسالتك أو ارفع مستند أوضح.")
    row = fetch_df("SELECT id FROM companies WHERE lower(company_name)=lower(?) ORDER BY id LIMIT 1", (name,))
    if not row.empty:
        cid = int(row.iloc[0]["id"])
        current = fetch_df("SELECT * FROM companies WHERE id=?", (cid,)).iloc[0]
        mapping = {"country":"country","contact_person":"contact_person","phone":"phone","email":"email","brands":"brands","payment_terms":"payment_terms"}
        updates, vals = [], []
        for src, col in mapping.items():
            nv = str((cdata or {}).get(src) or "").strip()
            cv = str(current[col] if col in current.index and current[col] is not None else "").strip()
            if nv and not cv:
                updates.append(f"{col}=?")
                vals.append(nv)
        if updates:
            vals.append(cid)
            execute(f"UPDATE companies SET {', '.join(updates)} WHERE id=?", tuple(vals))
        return cid
    return execute(
        "INSERT INTO companies (company_name,country,contact_person,phone,email,brands,payment_terms,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (name, cdata.get("country") or "China", cdata.get("contact_person") or "", cdata.get("phone") or "", cdata.get("email") or "", cdata.get("brands") or "", cdata.get("payment_terms") or "", "أضيفت بواسطة MIDO AI", now_text())
    )


def save_ai_analysis(analysis, file_records, user_message):
    cdata = analysis.get("company") or {}
    cid = get_or_create_company_ai(cdata)
    order_id = invoice_id = shipment_id = None

    o = analysis.get("order") or {}
    if any(_nonempty(o.get(k)) for k in ["order_number","product_summary","total_amount","quantity"]):
        on = str(o.get("order_number") or "").strip()
        existing = fetch_df("SELECT id FROM orders WHERE company_id=? AND order_number=? ORDER BY id LIMIT 1", (cid, on)) if on else pd.DataFrame()
        if not existing.empty:
            order_id = int(existing.iloc[0]["id"])
        else:
            order_id = execute(
                "INSERT INTO orders (company_id,order_number,order_date,product_summary,quantity,currency,total_amount,paid_amount,status,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (cid,on,o.get("order_date") or "",o.get("product_summary") or "",_f(o.get("quantity")),o.get("currency") or "USD",_f(o.get("total_amount")),_f(o.get("paid_amount")),o.get("status") or "جديدة","أضيفت بواسطة MIDO AI",now_text())
            )

    inv = analysis.get("invoice") or {}
    if any(_nonempty(inv.get(k)) for k in ["invoice_number","amount","invoice_date"]):
        ino = str(inv.get("invoice_number") or "").strip()
        existing = fetch_df("SELECT id FROM invoices WHERE company_id=? AND invoice_number=? ORDER BY id LIMIT 1", (cid, ino)) if ino else pd.DataFrame()
        if not existing.empty:
            invoice_id = int(existing.iloc[0]["id"])
        else:
            invoice_id = execute(
                "INSERT INTO invoices (company_id,order_id,invoice_number,invoice_date,due_date,currency,amount,status,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (cid,order_id,ino,inv.get("invoice_date") or "",inv.get("due_date") or "",inv.get("currency") or "USD",_f(inv.get("amount")),inv.get("status") or "غير مدفوعة","أضيفت بواسطة MIDO AI",now_text())
            )

    sh = analysis.get("shipment") or {}
    if any(_nonempty(sh.get(k)) for k in ["container_number","bl_number","shipment_number","eta"]):
        shipment_id = execute(
            "INSERT INTO shipments (company_id,order_id,shipment_number,container_number,bl_number,shipping_line,loading_port,destination_port,etd,eta,status,quantity_containers,tracking_url,received_at,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid,order_id,sh.get("shipment_number") or "",sh.get("container_number") or "",sh.get("bl_number") or "",sh.get("shipping_line") or "",sh.get("loading_port") or "",sh.get("destination_port") or "",sh.get("etd") or "",sh.get("eta") or "",sh.get("status") or "بالطريق",max(1,_i(sh.get("quantity_containers"),1)),"","","أضيفت بواسطة MIDO AI",now_text())
        )

    pay = analysis.get("payment") or {}
    if any(_nonempty(pay.get(k)) for k in ["amount","due_date","payment_date","reference"]):
        execute(
            "INSERT INTO payments (company_id,order_id,invoice_id,bank_account_id,payment_type,due_date,payment_date,currency,amount,status,reference,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid,order_id,invoice_id,None,pay.get("payment_type") or "AI",pay.get("due_date") or "",pay.get("payment_date") or "",pay.get("currency") or "USD",_f(pay.get("amount")),pay.get("status") or "مستحقة",pay.get("reference") or "","أضيفت بواسطة MIDO AI",now_text())
        )

    bank = analysis.get("bank_account") or {}
    if any(_nonempty(bank.get(k)) for k in ["bank_name","account_number","iban","swift"]):
        execute(
            "INSERT INTO bank_accounts (company_id,bank_name,beneficiary_name,account_number,iban,swift,bank_address,currency,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (cid,bank.get("bank_name") or "",bank.get("beneficiary_name") or "",bank.get("account_number") or "",bank.get("iban") or "",bank.get("swift") or "",bank.get("bank_address") or "",bank.get("currency") or "USD","أضيف بواسطة MIDO AI",now_text())
        )

    for pq in (analysis.get("price_quotes") or []):
        if _nonempty(pq.get("product_name")) and _f(pq.get("unit_price")):
            execute(
                "INSERT INTO prices (company_id,product_name,specification,brand,quantity,unit_price,currency,incoterm,quote_date,valid_until,notes,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (cid,pq.get("product_name") or "",pq.get("specification") or "",pq.get("brand") or "",_f(pq.get("quantity")),_f(pq.get("unit_price")),pq.get("currency") or "USD",pq.get("incoterm") or "",pq.get("quote_date") or "",pq.get("valid_until") or "","أضيف بواسطة MIDO AI",now_text())
            )

    for task in (analysis.get("tasks") or []):
        if _nonempty(task.get("title")):
            execute(
                "INSERT INTO notes_tasks (company_id,title,details,due_date,priority,status,created_at) VALUES (?,?,?,?,?,?,?)",
                (cid,task.get("title"),task.get("details") or "",task.get("due_date") or "",task.get("priority") or "متوسطة","مفتوحة",now_text())
            )

    dtype = analysis.get("document_type") or "Other"
    doc_map = {}
    for d in (analysis.get("documents") or []):
        if d.get("file_name"):
            doc_map[str(d.get("file_name"))] = d.get("document_type") or dtype
    saved_files = []
    for fr in file_records:
        class UploadObj:
            def __init__(self,n,b): self.name=n; self._b=b
            def getvalue(self): return self._b
        up = UploadObj(fr["name"], fr["bytes"])
        file_dtype = doc_map.get(fr["name"]) or infer_document_type(fr["name"]) or dtype
        if shipment_id:
            remote = save_uploaded_file_for_shipment(up, cid, shipment_id, file_dtype)
        else:
            remote = save_uploaded_file(up, cid, file_dtype)
        doc_id = execute(
            "INSERT INTO documents (company_id,order_id,shipment_id,invoice_id,document_type,file_name,file_path,upload_date,notes,storage_provider,dropbox_path,file_size) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid,order_id,shipment_id,invoice_id,file_dtype,fr["name"],remote,now_text(),analysis.get("summary") or "","dropbox",remote,len(fr["bytes"]))
        )
        saved_files.append((doc_id, remote))
    if shipment_id and saved_files:
        try:
            build_shipment_package(shipment_id)
        except Exception:
            pass

    execute(
        "INSERT INTO ai_ingestions (company_id,source_files,user_message,analysis_json,status,created_at) VALUES (?,?,?,?,?,?)",
        (cid,", ".join(fr["name"] for fr in file_records),user_message,json.dumps(analysis,ensure_ascii=False),"saved",now_text())
    )
    return {"company_id":cid,"order_id":order_id,"invoice_id":invoice_id,"shipment_id":shipment_id,"files":saved_files}


def database_context_for_ai():
    return {
        "companies": fetch_df("SELECT id,company_name,brands,payment_terms,notes FROM companies ORDER BY id DESC LIMIT 100").to_dict("records"),
        "orders": fetch_df("SELECT id,company_id,order_number,product_summary,total_amount,currency,paid_amount,status FROM orders ORDER BY id DESC LIMIT 100").to_dict("records"),
        "shipments": fetch_df("SELECT id,company_id,container_number,bl_number,destination_port,eta,status FROM shipments ORDER BY id DESC LIMIT 100").to_dict("records"),
        "payments": fetch_df("SELECT id,company_id,amount,currency,due_date,status FROM payments ORDER BY id DESC LIMIT 100").to_dict("records"),
        "prices": fetch_df("SELECT company_id,product_name,specification,unit_price,currency,quote_date FROM prices ORDER BY id DESC LIMIT 120").to_dict("records"),
    }


def ask_real_mido(question):
    client, model = get_ai_client()
    if not client:
        raise RuntimeError("AI غير مربوط بعد.")
    ctx = database_context_for_ai()
    prompt = "You are MIDO, the user's private business assistant. Answer in Iraqi Arabic, concise and factual, using ONLY the supplied ERP data. If the data is insufficient, say what is missing. Received/delivered shipments should not be described as active. Never invent values.\nERP DATA:\n" + json.dumps(ctx, ensure_ascii=False, default=str)
    r = client.chat.completions.create(model=model, messages=[{"role":"system","content":prompt},{"role":"user","content":question}], temperature=0)
    return r.choices[0].message.content



def voice_ready():
    # Gemini can transcribe the audio directly with the same API key/model already used by MIDO.
    # For non-Gemini providers we keep the optional OpenAI-style transcription_model fallback.
    base_url = (_ai_secret("base_url") or "").lower()
    if "generativelanguage.googleapis.com" in base_url:
        return ai_ready()
    return bool(ai_ready() and _ai_secret("transcription_model"))


def tts_ready():
    # Spoken replies use the browser's built-in Arabic speech engine, so no paid TTS API is required.
    return True


def _gemini_native_url(model):
    api_key = _ai_secret("api_key")
    if not api_key or not model:
        raise RuntimeError("مفتاح Gemini أو اسم الموديل غير موجود داخل [ai].")
    return (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        + str(model).strip()
        + ":generateContent?key="
        + str(api_key).strip()
    )


def transcribe_audio_bytes(audio_bytes, filename="voice.wav"):
    base_url = (_ai_secret("base_url") or "").lower()

    # Gemini path: send the recorded WAV directly to Gemini's native multimodal endpoint.
    if "generativelanguage.googleapis.com" in base_url:
        model = _ai_secret("model")
        payload = {
            "contents": [{
                "parts": [
                    {
                        "text": (
                            "Transcribe this audio accurately. The speaker may use Iraqi Arabic, "
                            "Arabic, Kurdish, or English. Return ONLY the spoken text, with no "
                            "explanation, labels, markdown, or quotation marks."
                        )
                    },
                    {
                        "inline_data": {
                            "mime_type": "audio/wav",
                            "data": base64.b64encode(audio_bytes).decode("ascii"),
                        }
                    },
                ]
            }],
            "generationConfig": {"temperature": 0.0},
        }
        req = urllib.request.Request(
            _gemini_native_url(model),
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Gemini transcription HTTP {e.code}: {detail[:700]}")
        except Exception as e:
            raise RuntimeError(f"تعذر الاتصال بخدمة Gemini الصوتية: {e}")

        try:
            parts = data["candidates"][0]["content"]["parts"]
            result_text = "".join(str(p.get("text", "")) for p in parts).strip()
        except Exception:
            result_text = ""
        if not result_text:
            raise RuntimeError("Gemini لم يرجع نصاً من التسجيل.")
        return result_text

    # Fallback for an OpenAI-compatible provider that supports audio transcriptions.
    client, _model = get_ai_client()
    model = _ai_secret("transcription_model")
    if not client or not model:
        raise RuntimeError("فعّل transcription_model داخل [ai] في Streamlit Secrets.")
    f = io.BytesIO(audio_bytes)
    f.name = filename
    result = client.audio.transcriptions.create(model=model, file=f)
    return getattr(result, "text", None) or str(result)


def render_browser_speech(text, key="mido_speech"):
    """Render an in-browser Arabic speech button without sending text to a TTS provider."""
    safe_text = json.dumps(str(text), ensure_ascii=False)
    html = f"""
    <div style="font-family:system-ui;direction:rtl;text-align:right">
      <button id="speak" style="padding:9px 14px;border:1px solid #bbb;border-radius:10px;
      background:white;cursor:pointer;font-size:15px">🔊 تشغيل الرد الصوتي</button>
      <button id="stop" style="padding:9px 14px;border:1px solid #bbb;border-radius:10px;
      background:white;cursor:pointer;font-size:15px;margin-right:6px">⏹ إيقاف</button>
    </div>
    <script>
      const midoText = {safe_text};
      document.getElementById("speak").onclick = function() {{
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(midoText);
        u.lang = "ar-IQ";
        u.rate = 0.95;
        u.pitch = 1.0;
        const voices = window.speechSynthesis.getVoices();
        const ar = voices.find(v => (v.lang || "").toLowerCase().startsWith("ar"));
        if (ar) u.voice = ar;
        window.speechSynthesis.speak(u);
      }};
      document.getElementById("stop").onclick = function() {{
        window.speechSynthesis.cancel();
      }};
    </script>
    """
    components.html(html, height=55, scrolling=False)


def generate_development_plan(request_text):
    client, model = get_ai_client()
    if not client:
        raise RuntimeError("AI غير مربوط.")
    prompt = """You are MIDO's software product architect. The user describes a feature they want in their private Streamlit ERP.
Return an Arabic implementation plan that is concrete and safe: UI changes, database migration, Dropbox storage impact, AI impact, risks, and test checklist.
Do NOT claim the live app has been modified. Do NOT include secrets. Keep the plan practical for a Python/Streamlit/SQLite/Dropbox codebase."""
    r = client.chat.completions.create(model=model, messages=[{"role":"system","content":prompt},{"role":"user","content":request_text}], temperature=0.1)
    return r.choices[0].message.content

# -------------------- Sidebar --------------------
st.sidebar.title("🤖 MIDO")
st.sidebar.caption("مساعد محمد التجاري — v6.1 Gemini Voice")
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
    st.sidebar.success("☁️ Dropbox مربوط — الملفات الأصلية والنسخة الاحتياطية تُحفظ في MIDO")
else:
    st.sidebar.warning("ℹ️ Dropbox غير مربوط بعد — البرنامج يعمل بشكل طبيعي، والربط السحابي نفعّله لاحقاً")
if ai_ready():
    st.sidebar.success("🧠 MIDO AI مربوط وجاهز")
else:
    st.sidebar.info("🧠 MIDO AI يحتاج مفتاح AI داخل Secrets")

# -------------------- Dashboard --------------------
if choice == "📊 لوحة التحكم":
    st.markdown("<div class='mido-hero'><h2>مرحباً محمد 👋</h2><div>كل شركاتك وطلبياتك وشحناتك ودفعاتك في مكان واحد.</div></div>", unsafe_allow_html=True)

    counts = {
        "companies": fetch_df("SELECT COUNT(*) n FROM companies").iloc[0, 0],
        "orders": fetch_df("SELECT COUNT(*) n FROM orders").iloc[0, 0],
        "shipments": fetch_df("SELECT COUNT(*) n FROM shipments WHERE COALESCE(status,'') NOT LIKE '%استلام%' AND COALESCE(status,'') NOT LIKE '%مستلمة%' AND COALESCE(status,'') NOT LIKE '%Delivered%' AND COALESCE(status,'') NOT LIKE '%مغلقة%'").iloc[0, 0],
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
            WHERE COALESCE(s.status,'') NOT LIKE '%استلام%'
              AND COALESCE(s.status,'') NOT LIKE '%مستلمة%'
              AND COALESCE(s.status,'') NOT LIKE '%Delivered%'
              AND COALESCE(s.status,'') NOT LIKE '%مغلقة%'
            ORDER BY CASE WHEN s.eta IS NULL OR s.eta='' THEN 1 ELSE 0 END, s.eta ASC LIMIT 10
        """)
        if shp.empty:
            st.info("لا توجد شحنات مسجلة.")
        else:
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
        if pay.empty:
            st.info("لا توجد دفعات قادمة.")
        else:
            st.dataframe(pay, use_container_width=True, hide_index=True)

    st.subheader("📝 مهام تحتاج متابعة")
    tasks = fetch_df("""
        SELECT n.id, COALESCE(c.company_name,'عام') AS الشركة, n.title AS المهمة,
               n.due_date AS الموعد, n.priority AS الأولوية, n.status AS الحالة
        FROM notes_tasks n LEFT JOIN companies c ON c.id=n.company_id
        WHERE n.status NOT IN ('مكتملة','تم')
        ORDER BY CASE WHEN n.due_date IS NULL OR n.due_date='' THEN 1 ELSE 0 END, n.due_date ASC LIMIT 12
    """)
    if tasks.empty:
        st.info("لا توجد مهام مفتوحة.")
    else:
        st.dataframe(tasks, use_container_width=True, hide_index=True)

# -------------------- Companies --------------------
elif choice == "🏭 الشركات الصينية":
    st.title("🏭 الشركات الصينية")
    tab1, tab2 = st.tabs(["📋 الشركات", "➕ إضافة شركة"])

    with tab2:
        with st.form("add_company"):
            a, b = st.columns(2)
            with a:
                name = st.text_input("اسم الشركة / المصنع *")
                country = st.text_input("الدولة", value="China")
                city = st.text_input("المدينة")
                contact = st.text_input("الشخص المسؤول")
                phone = st.text_input("الهاتف")
                whatsapp = st.text_input("WhatsApp")
            with b:
                email = st.text_input("الإيميل")
                website = st.text_input("الموقع")
                brands = st.text_input("البراندات", placeholder="Linglong, Botrian, ...")
                terms = st.text_area("شروط الدفع")
                notes = st.text_area("ملاحظات")
            if st.form_submit_button("💾 حفظ الشركة", use_container_width=True):
                if not name.strip():
                    st.error("اسم الشركة مطلوب.")
                else:
                    execute("""INSERT INTO companies
                        (company_name,country,city,contact_person,phone,whatsapp,email,website,brands,payment_terms,notes,created_at)
                        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (name.strip(), country, city, contact, phone, whatsapp, email, website, brands, terms, notes, now_text()))
                    st.success("تمت إضافة الشركة.")
                    st.rerun()

    with tab1:
        companies = fetch_df("SELECT * FROM companies ORDER BY company_name")
        if companies.empty:
            st.info("لا توجد شركات بعد.")
        else:
            search = st.text_input("🔎 ابحث باسم الشركة أو البراند")
            filtered = companies.copy()
            if search:
                mask = filtered.astype(str).apply(lambda row: row.str.contains(search, case=False, na=False).any(), axis=1)
                filtered = filtered[mask]
            st.dataframe(filtered[["id","company_name","contact_person","phone","email","brands","payment_terms"]], use_container_width=True, hide_index=True)

            labels = {f"{r.company_name} (#{int(r.id)})": int(r.id) for r in companies.itertuples()}
            selected_label = st.selectbox("افتح ملف شركة", list(labels.keys()))
            cid = labels[selected_label]
            company = fetch_df("SELECT * FROM companies WHERE id=?", (cid,)).iloc[0]
            st.markdown(f"### 📂 ملف {company['company_name']}")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("الطلبيات", int(fetch_df("SELECT COUNT(*) n FROM orders WHERE company_id=?", (cid,)).iloc[0,0]))
            m2.metric("الفواتير", int(fetch_df("SELECT COUNT(*) n FROM invoices WHERE company_id=?", (cid,)).iloc[0,0]))
            m3.metric("الشحنات", int(fetch_df("SELECT COUNT(*) n FROM shipments WHERE company_id=?", (cid,)).iloc[0,0]))
            m4.metric("المستندات", int(fetch_df("SELECT COUNT(*) n FROM documents WHERE company_id=?", (cid,)).iloc[0,0]))

            ctabs = st.tabs(["معلومات", "طلبيات", "فواتير", "شحنات", "دفعات", "حسابات بنكية", "مستندات", "أسعار"])
            with ctabs[0]:
                st.markdown("#### ✏️ تعديل بيانات الشركة")
                with st.form(f"edit_company_{cid}"):
                    e1, e2 = st.columns(2)
                    with e1:
                        e_name = st.text_input("اسم الشركة / المصنع *", value=company["company_name"] or "")
                        e_country = st.text_input("الدولة", value=company["country"] or "China")
                        e_city = st.text_input("المدينة", value=company["city"] or "")
                        e_contact = st.text_input("الشخص المسؤول", value=company["contact_person"] or "")
                        e_phone = st.text_input("الهاتف", value=company["phone"] or "")
                        e_whatsapp = st.text_input("WhatsApp", value=company["whatsapp"] or "")
                    with e2:
                        e_email = st.text_input("الإيميل", value=company["email"] or "")
                        e_website = st.text_input("الموقع", value=company["website"] or "")
                        e_brands = st.text_input("البراندات", value=company["brands"] or "")
                        e_terms = st.text_area("شروط الدفع", value=company["payment_terms"] or "")
                        e_notes = st.text_area("ملاحظات", value=company["notes"] or "")

                    if st.form_submit_button("💾 حفظ التعديلات", use_container_width=True):
                        if not e_name.strip():
                            st.error("اسم الشركة مطلوب.")
                        else:
                            execute(
                                """UPDATE companies SET company_name=?,country=?,city=?,contact_person=?,phone=?,whatsapp=?,email=?,website=?,brands=?,payment_terms=?,notes=? WHERE id=?""",
                                (e_name.strip(), e_country, e_city, e_contact, e_phone, e_whatsapp, e_email, e_website, e_brands, e_terms, e_notes, cid),
                            )
                            st.success("تم تعديل بيانات الشركة بنجاح.")
                            st.rerun()

                st.markdown("---")
                st.markdown("#### 🗑️ حذف الشركة")
                st.caption("للحماية من الحذف بالخطأ: اكتب اسم الشركة بالضبط ثم اضغط حذف. حذف الشركة سيحذف سجلاتها المرتبطة من قاعدة MIDO، لكن ملفات Dropbox الأصلية لن تُحذف تلقائياً.")
                confirm_name = st.text_input("اكتب اسم الشركة للتأكيد", key=f"delete_company_name_{cid}")
                if st.button("🗑️ حذف الشركة نهائياً", key=f"delete_company_{cid}", type="secondary", use_container_width=True):
                    if confirm_name.strip() != str(company["company_name"]).strip():
                        st.error("اسم الشركة غير مطابق. لم يتم الحذف.")
                    else:
                        execute("DELETE FROM companies WHERE id=?", (cid,))
                        st.success("تم حذف الشركة وسجلاتها المرتبطة من MIDO.")
                        st.rerun()
            with ctabs[1]:
                st.dataframe(fetch_df("SELECT id, order_number, order_date, product_summary, quantity, total_amount, currency, paid_amount, status FROM orders WHERE company_id=? ORDER BY id DESC", (cid,)), use_container_width=True, hide_index=True)
            with ctabs[2]:
                st.dataframe(fetch_df("SELECT id, invoice_number, invoice_date, due_date, amount, currency, status FROM invoices WHERE company_id=? ORDER BY id DESC", (cid,)), use_container_width=True, hide_index=True)
            with ctabs[3]:
                st.dataframe(fetch_df("SELECT id, shipment_number, container_number, bl_number, shipping_line, loading_port, destination_port, etd, eta, status FROM shipments WHERE company_id=? ORDER BY id DESC", (cid,)), use_container_width=True, hide_index=True)
            with ctabs[4]:
                st.dataframe(fetch_df("SELECT id, payment_type, due_date, payment_date, amount, currency, status, reference FROM payments WHERE company_id=? ORDER BY id DESC", (cid,)), use_container_width=True, hide_index=True)
            with ctabs[5]:
                st.dataframe(fetch_df("SELECT id, bank_name, beneficiary_name, account_number, iban, swift, currency FROM bank_accounts WHERE company_id=? ORDER BY id DESC", (cid,)), use_container_width=True, hide_index=True)
            with ctabs[6]:
                docs = fetch_df("SELECT id, document_type, file_name, file_path, storage_provider, dropbox_path, upload_date FROM documents WHERE company_id=? ORDER BY id DESC", (cid,))
                if docs.empty:
                    st.info("لا توجد مستندات.")
                else:
                    for r in docs.itertuples():
                        col1, col2 = st.columns([4,1])
                        col1.write(f"📄 **{r.file_name}** — {r.document_type or 'مستند'} — {r.upload_date}")
                        try:
                            data = get_document_bytes(r)
                            col2.download_button("تنزيل", data=data, file_name=r.file_name, key=f"co_doc_{r.id}")
                        except Exception as e:
                            col2.caption("غير متاح")
            with ctabs[7]:
                st.dataframe(fetch_df("SELECT product_name, specification, brand, quantity, unit_price, currency, incoterm, quote_date, valid_until FROM prices WHERE company_id=? ORDER BY id DESC", (cid,)), use_container_width=True, hide_index=True)

# -------------------- Orders --------------------
elif choice == "🧾 الطلبيات":
    st.title("🧾 الطلبيات")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        with st.expander("➕ إضافة طلبية", expanded=True):
            with st.form("add_order"):
                c1, c2 = st.columns(2)
                with c1:
                    comp_label = st.selectbox("الشركة", list(opts.keys()))
                    order_no = st.text_input("رقم الطلبية / PO")
                    order_date = st.date_input("تاريخ الطلبية", value=date.today())
                    product = st.text_area("المنتجات / التفاصيل")
                    quantity = st.number_input("الكمية", min_value=0.0, step=1.0)
                with c2:
                    currency = st.selectbox("العملة", ["USD","RMB","EUR","AED","IQD"])
                    total = st.number_input("إجمالي الطلبية", min_value=0.0, step=100.0)
                    paid = st.number_input("المدفوع", min_value=0.0, step=100.0)
                    status = st.selectbox("الحالة", ["جديدة","قيد التصنيع","جاهزة للشحن","شحن جزئي","مكتملة","ملغاة"])
                    notes = st.text_area("ملاحظات")
                if st.form_submit_button("💾 حفظ الطلبية"):
                    execute("""INSERT INTO orders (company_id,order_number,order_date,product_summary,quantity,currency,total_amount,paid_amount,status,notes,created_at)
                             VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                            (opts[comp_label], order_no, str(order_date), product, quantity, currency, total, paid, status, notes, now_text()))
                    st.success("تم حفظ الطلبية.")
                    st.rerun()

        df = fetch_df("""SELECT o.id, c.company_name AS الشركة, o.order_number AS الطلبية, o.order_date AS التاريخ,
                              o.product_summary AS المنتجات, o.total_amount AS الإجمالي, o.currency AS العملة,
                              o.paid_amount AS المدفوع, (o.total_amount-o.paid_amount) AS المتبقي, o.status AS الحالة
                       FROM orders o JOIN companies c ON c.id=o.company_id ORDER BY o.id DESC""")
        st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- Invoices --------------------
elif choice == "💵 الفواتير":
    st.title("💵 الفواتير")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        with st.expander("➕ إضافة فاتورة", expanded=True):
            comp_label = st.selectbox("الشركة", list(opts.keys()), key="inv_company")
            cid = opts[comp_label]
            order_opts = optional_order_options(cid)
            with st.form("add_invoice"):
                c1, c2 = st.columns(2)
                with c1:
                    order_label = st.selectbox("ربط بالطلبية", list(order_opts.keys()))
                    inv_no = st.text_input("رقم الفاتورة / PI / CI")
                    inv_date = st.date_input("تاريخ الفاتورة", value=date.today())
                    due = st.date_input("تاريخ الاستحقاق", value=date.today())
                with c2:
                    currency = st.selectbox("العملة", ["USD","RMB","EUR","AED","IQD"], key="inv_curr")
                    amount = st.number_input("المبلغ", min_value=0.0, step=100.0)
                    status = st.selectbox("الحالة", ["غير مدفوعة","مدفوعة جزئياً","مدفوعة","ملغاة"])
                    notes = st.text_area("ملاحظات")
                if st.form_submit_button("💾 حفظ الفاتورة"):
                    execute("""INSERT INTO invoices (company_id,order_id,invoice_number,invoice_date,due_date,currency,amount,status,notes,created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (cid, order_opts[order_label], inv_no, str(inv_date), str(due), currency, amount, status, notes, now_text()))
                    st.success("تم حفظ الفاتورة.")
                    st.rerun()

        df = fetch_df("""SELECT i.id,c.company_name AS الشركة,i.invoice_number AS الفاتورة,o.order_number AS الطلبية,
                              i.invoice_date AS التاريخ,i.due_date AS الاستحقاق,i.amount AS المبلغ,i.currency AS العملة,i.status AS الحالة
                       FROM invoices i JOIN companies c ON c.id=i.company_id LEFT JOIN orders o ON o.id=i.order_id ORDER BY i.id DESC""")
        st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- Shipments --------------------
elif choice == "🚢 الشحنات":
    st.title("🚢 الشحنات والحاويات")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        comp_label = st.selectbox("اختر الشركة للشحنة الجديدة", list(opts.keys()), key="ship_company")
        cid = opts[comp_label]
        order_opts = optional_order_options(cid)
        with st.expander("➕ إضافة شحنة", expanded=True):
            with st.form("add_shipment"):
                a,b = st.columns(2)
                with a:
                    order_label = st.selectbox("ربط بالطلبية", list(order_opts.keys()))
                    ship_no = st.text_input("رقم الشحنة")
                    container = st.text_input("رقم الحاوية")
                    bl = st.text_input("رقم Bill of Lading")
                    line = st.text_input("شركة الشحن")
                    qty_cont = st.number_input("عدد الحاويات", min_value=1, step=1)
                with b:
                    loading = st.text_input("ميناء التحميل", value="China")
                    destination = st.text_input("ميناء الوصول", value="Umm Qasr")
                    etd = st.date_input("ETD", value=date.today())
                    eta = st.date_input("ETA", value=date.today())
                    status = st.selectbox("الحالة", ["في المعمل","جاهزة للشحن","بالطريق","Transshipment","وصلت الميناء","بالجمارك","تم الاستلام"])
                    tracking = st.text_input("رابط التتبع")
                    notes = st.text_area("ملاحظات")
                if st.form_submit_button("💾 حفظ الشحنة"):
                    received_at = now_text() if status == "تم الاستلام" else ""
                    execute("""INSERT INTO shipments (company_id,order_id,shipment_number,container_number,bl_number,shipping_line,loading_port,destination_port,etd,eta,status,quantity_containers,tracking_url,received_at,notes,created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (cid, order_opts[order_label], ship_no, container, bl, line, loading, destination, str(etd), str(eta), status, int(qty_cont), tracking, received_at, notes, now_text()))
                    st.success("تم حفظ الشحنة.")
                    st.rerun()

        st.markdown("### تحديث حالة شحنة")
        ship_manage = fetch_df("""SELECT s.id,c.company_name AS company_name,s.container_number,s.bl_number,s.status,s.received_at
                                  FROM shipments s JOIN companies c ON c.id=s.company_id ORDER BY s.id DESC""")
        if not ship_manage.empty:
            labels = {f"#{int(r.id)} — {r.company_name} — {r.container_number or r.bl_number or 'شحنة'} — {r.status or '-'}": int(r.id) for r in ship_manage.itertuples()}
            pick = st.selectbox("اختر الشحنة", list(labels.keys()), key="ship_status_pick")
            selected_id = labels[pick]
            current = ship_manage[ship_manage["id"] == selected_id].iloc[0]
            statuses = ["في المعمل","جاهزة للشحن","بالطريق","Transshipment","وصلت الميناء","بالجمارك","تم الاستلام","مغلقة"]
            current_status = current["status"] if current["status"] in statuses else "بالطريق"
            new_status = st.selectbox("الحالة الجديدة", statuses, index=statuses.index(current_status), key="ship_status_new")
            if st.button("💾 تحديث حالة الشحنة", type="primary"):
                received_at = current["received_at"] or ""
                if new_status == "تم الاستلام" and not received_at:
                    received_at = now_text()
                if new_status != "تم الاستلام" and current["status"] == "تم الاستلام":
                    received_at = ""
                execute("UPDATE shipments SET status=?, received_at=? WHERE id=?", (new_status, received_at, selected_id))
                st.success("تم تحديث الحالة. إذا كانت الشحنة مستلمة فلن تظهر ضمن الشحنات المهمة أو بالطريق.")
                st.rerun()

        st.markdown("### 📎 أوراق الشحنة — رفع عدة ملفات دفعة واحدة")
        ship_docs = fetch_df("""SELECT s.id,c.company_name,s.company_id,s.order_id,s.container_number,s.bl_number,s.shipment_number
                              FROM shipments s JOIN companies c ON c.id=s.company_id ORDER BY s.id DESC""")
        if not ship_docs.empty:
            ship_doc_labels = {
                f"#{int(r.id)} — {r.company_name} — {r.container_number or r.bl_number or r.shipment_number or 'شحنة'}": int(r.id)
                for r in ship_docs.itertuples()
            }
            ship_doc_pick = st.selectbox("اختر الشحنة لرفع أوراقها", list(ship_doc_labels.keys()), key="ship_docs_pick")
            ship_doc_id = ship_doc_labels[ship_doc_pick]
            sr = ship_docs[ship_docs["id"] == ship_doc_id].iloc[0]
            ship_files = st.file_uploader(
                "ارفع Invoice + Packing List + Certificate of Origin + BL + QR Code وأي ملفات إضافية",
                type=["pdf","png","jpg","jpeg","webp","xlsx","xls","csv","txt"],
                accept_multiple_files=True,
                key="shipment_multi_files",
            )
            use_ai_classification = st.checkbox("🧠 خلّي MIDO AI يصنف الملفات ويستخرج المعلومات قبل الحفظ", value=True, key="ship_ai_classify")
            if st.button("⬆️ رفع كل أوراق الشحنة", type="primary", key="ship_multi_upload"):
                if not ship_files:
                    st.error("ارفع ملفاً واحداً على الأقل.")
                else:
                    try:
                        records = [{"name":u.name,"type":u.type,"bytes":u.getvalue()} for u in ship_files]
                        type_map = {}
                        analysis = None
                        if use_ai_classification and ai_ready():
                            with st.spinner("ميدو يحلل أوراق الشحنة ويصنفها..."):
                                analysis = analyze_business_files(records, f"هذه الأوراق مرتبطة بالشحنة رقم {ship_doc_id}. صنّف كل ملف واستخرج معلومات الشحنة والفاتورة.")
                            for d in analysis.get("documents") or []:
                                if d.get("file_name"):
                                    type_map[d["file_name"]] = d.get("document_type") or "Other"
                        for u in ship_files:
                            dtype = type_map.get(u.name) or infer_document_type(u.name)
                            remote = save_uploaded_file_for_shipment(u, int(sr["company_id"]), ship_doc_id, dtype)
                            execute("""INSERT INTO documents
                                (company_id,order_id,shipment_id,invoice_id,document_type,file_name,file_path,upload_date,notes,storage_provider,dropbox_path,file_size)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (int(sr["company_id"]), int(sr["order_id"]) if pd.notna(sr["order_id"]) else None, ship_doc_id, None,
                                 dtype, u.name, remote, now_text(), (analysis or {}).get("summary", ""), "dropbox", remote, len(u.getvalue())))
                        package_bytes, package_remote = build_shipment_package(ship_doc_id)
                        st.success(f"✅ تم رفع {len(ship_files)} ملف/ملفات وربطها بالشحنة وإنشاء ملف ZIP شامل تلقائياً.")
                        st.download_button("⬇️ تنزيل الملف الكامل للشحنة", data=package_bytes, file_name=f"Shipment_{ship_doc_id}_MIDO.zip", mime="application/zip")
                        st.caption(f"☁️ Package: {package_remote}")
                        if analysis:
                            with st.expander("🧠 المعلومات التي استخرجها MIDO AI"):
                                st.json(analysis)
                    except Exception as e:
                        st.error(f"تعذر رفع أوراق الشحنة: {e}")

        active_tab, received_tab = st.tabs(["🚢 الشحنات النشطة", "✅ الشحنات المستلمة / الأرشيف"])
        with active_tab:
            df = fetch_df("""SELECT s.id,c.company_name AS الشركة,o.order_number AS الطلبية,s.container_number AS الحاوية,
                                  s.bl_number AS BL,s.shipping_line AS الناقل,s.loading_port AS من,s.destination_port AS إلى,
                                  s.etd AS ETD,s.eta AS ETA,s.status AS الحالة,s.quantity_containers AS الحاويات
                           FROM shipments s JOIN companies c ON c.id=s.company_id LEFT JOIN orders o ON o.id=s.order_id
                           WHERE COALESCE(s.status,'') NOT LIKE '%استلام%'
                             AND COALESCE(s.status,'') NOT LIKE '%مستلمة%'
                             AND COALESCE(s.status,'') NOT LIKE '%Delivered%'
                             AND COALESCE(s.status,'') NOT LIKE '%مغلقة%'
                           ORDER BY CASE WHEN s.eta IS NULL OR s.eta='' THEN 1 ELSE 0 END, s.eta ASC, s.id DESC""")
            if df.empty:
                st.success("لا توجد شحنات نشطة حالياً.")
            else:
                st.dataframe(df, use_container_width=True, hide_index=True)
        with received_tab:
            received = fetch_df("""SELECT s.id,c.company_name AS الشركة,o.order_number AS الطلبية,s.container_number AS الحاوية,
                                        s.bl_number AS BL,s.destination_port AS الوجهة,s.eta AS ETA,s.status AS الحالة,
                                        s.received_at AS تاريخ_الاستلام
                                 FROM shipments s JOIN companies c ON c.id=s.company_id LEFT JOIN orders o ON o.id=s.order_id
                                 WHERE COALESCE(s.status,'') LIKE '%استلام%' OR COALESCE(s.status,'') LIKE '%مستلمة%'
                                    OR COALESCE(s.status,'') LIKE '%Delivered%' OR COALESCE(s.status,'') LIKE '%مغلقة%'
                                 ORDER BY s.id DESC""")
            if received.empty:
                st.info("لا توجد شحنات مستلمة في الأرشيف بعد.")
            else:
                st.dataframe(received, use_container_width=True, hide_index=True)

# -------------------- Payments --------------------
elif choice == "💳 الدفعات":
    st.title("💳 الدفعات ومواعيد الاستحقاق")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        comp_label = st.selectbox("الشركة", list(opts.keys()), key="pay_company")
        cid = opts[comp_label]
        order_opts = optional_order_options(cid)
        invoice_opts = optional_invoice_options(cid)
        bank_opts = optional_bank_options(cid)
        with st.form("add_payment"):
            a,b = st.columns(2)
            with a:
                order_label = st.selectbox("الطلبية", list(order_opts.keys()))
                invoice_label = st.selectbox("الفاتورة", list(invoice_opts.keys()))
                bank_label = st.selectbox("الحساب البنكي", list(bank_opts.keys()))
                ptype = st.selectbox("نوع الدفعة", ["Advance","Balance","Deposit","Freight","Customs","Other"])
                amount = st.number_input("المبلغ", min_value=0.0, step=100.0)
            with b:
                currency = st.selectbox("العملة", ["USD","RMB","EUR","AED","IQD"], key="pay_curr")
                due = st.date_input("موعد الاستحقاق", value=date.today())
                pdate = st.text_input("تاريخ الدفع الفعلي", placeholder="اتركه فارغاً إذا لم تُدفع")
                status = st.selectbox("الحالة", ["مستحقة","مستحقة قريباً","مدفوعة جزئياً","مدفوعة","مؤجلة"])
                ref = st.text_input("رقم التحويل / المرجع")
                notes = st.text_area("ملاحظات")
            if st.form_submit_button("💾 حفظ الدفعة"):
                execute("""INSERT INTO payments (company_id,order_id,invoice_id,bank_account_id,payment_type,due_date,payment_date,currency,amount,status,reference,notes,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (cid, order_opts[order_label], invoice_opts[invoice_label], bank_opts[bank_label], ptype, str(due), pdate, currency, amount, status, ref, notes, now_text()))
                st.success("تم حفظ الدفعة.")
                st.rerun()

        df = fetch_df("""SELECT p.id,c.company_name AS الشركة,p.payment_type AS النوع,p.amount AS المبلغ,p.currency AS العملة,
                              p.due_date AS الاستحقاق,p.payment_date AS تاريخ_الدفع,p.status AS الحالة,p.reference AS المرجع
                       FROM payments p JOIN companies c ON c.id=p.company_id ORDER BY p.id DESC""")
        st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- Bank Accounts --------------------
elif choice == "🏦 الحسابات البنكية":
    st.title("🏦 الحسابات البنكية للمصانع")
    st.warning("هذه بيانات مالية حساسة. النسخة الحالية محلية؛ عند نشر البرنامج على الإنترنت استخدم تسجيل دخول قوي، تشفير، وصلاحيات مستخدمين.")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        with st.form("add_bank"):
            a,b = st.columns(2)
            with a:
                comp_label = st.selectbox("الشركة", list(opts.keys()), key="bank_company")
                bank_name = st.text_input("اسم البنك")
                beneficiary = st.text_input("اسم المستفيد")
                account = st.text_input("رقم الحساب")
                iban = st.text_input("IBAN")
            with b:
                swift = st.text_input("SWIFT / BIC")
                address = st.text_input("عنوان البنك")
                currency = st.selectbox("العملة", ["USD","RMB","EUR","AED","IQD"], key="bank_curr")
                notes = st.text_area("ملاحظات")
            if st.form_submit_button("💾 حفظ الحساب"):
                execute("""INSERT INTO bank_accounts (company_id,bank_name,beneficiary_name,account_number,iban,swift,bank_address,currency,notes,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?)""",
                        (opts[comp_label],bank_name,beneficiary,account,iban,swift,address,currency,notes,now_text()))
                st.success("تم حفظ الحساب البنكي.")
                st.rerun()

        df = fetch_df("""SELECT b.id,c.company_name AS الشركة,b.bank_name AS البنك,b.beneficiary_name AS المستفيد,
                              b.account_number AS الحساب,b.iban AS IBAN,b.swift AS SWIFT,b.currency AS العملة
                       FROM bank_accounts b JOIN companies c ON c.id=b.company_id ORDER BY b.id DESC""")
        st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- Documents --------------------
elif choice == "📁 المستندات الأصلية":
    st.title("📁 المستندات الأصلية PDF والصور")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        comp_label = st.selectbox("الشركة", list(opts.keys()), key="doc_company")
        cid = opts[comp_label]
        order_opts = optional_order_options(cid)
        invoice_opts = optional_invoice_options(cid)
        shipment_opts = optional_shipment_options(cid)

        with st.form("upload_doc", clear_on_submit=True):
            a,b = st.columns(2)
            with a:
                doc_type = st.selectbox("نوع المستند", ["Proforma Invoice","Commercial Invoice","Packing List","Bill of Lading","Certificate of Origin","QR Code","Payment Proof","Price List","Insurance","Contract","Bank Slip","Customs","Other"])
                order_label = st.selectbox("ربط بطلبية", list(order_opts.keys()))
                invoice_label = st.selectbox("ربط بفاتورة", list(invoice_opts.keys()))
                shipment_label = st.selectbox("ربط بشحنة", list(shipment_opts.keys()))
            with b:
                ups = st.file_uploader("اختر ملفاً أو عدة ملفات", type=["pdf","png","jpg","jpeg","webp","xlsx","xls","csv","txt"], accept_multiple_files=True)
                notes = st.text_area("ملاحظات")
            if st.form_submit_button("⬆️ رفع وحفظ المستندات"):
                if not ups:
                    st.error("اختر ملفاً واحداً على الأقل.")
                else:
                    try:
                        shipment_id = shipment_opts[shipment_label]
                        for up in ups:
                            dtype = doc_type if doc_type != "Other" else infer_document_type(up.name)
                            path = save_uploaded_file_for_shipment(up, cid, shipment_id, dtype) if shipment_id else save_uploaded_file(up, cid, dtype)
                            execute("""INSERT INTO documents
                                (company_id,order_id,shipment_id,invoice_id,document_type,file_name,file_path,upload_date,notes,storage_provider,dropbox_path,file_size)
                                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (cid, order_opts[order_label], shipment_id, invoice_opts[invoice_label],
                                 dtype, up.name, path, now_text(), notes, "dropbox", path, len(up.getvalue())))
                        if shipment_id:
                            try: build_shipment_package(shipment_id)
                            except Exception: pass
                        st.success(f"✅ تم رفع {len(ups)} ملف/ملفات أصلية إلى Dropbox وربطها بالسجل.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"تعذر رفع الملفات إلى Dropbox: {e}")

        docs = fetch_df("""SELECT d.id,c.company_name AS الشركة,d.document_type AS النوع,d.file_name AS الملف,
                                o.order_number AS الطلبية,s.container_number AS الحاوية,i.invoice_number AS الفاتورة,
                                d.file_path,d.storage_provider,d.dropbox_path,d.file_size,d.upload_date AS تاريخ_الرفع
                         FROM documents d JOIN companies c ON c.id=d.company_id
                         LEFT JOIN orders o ON o.id=d.order_id LEFT JOIN shipments s ON s.id=d.shipment_id
                         LEFT JOIN invoices i ON i.id=d.invoice_id ORDER BY d.id DESC""")
        if docs.empty:
            st.info("لا توجد مستندات بعد.")
        else:
            hidden_cols = [c for c in ["file_path","dropbox_path"] if c in docs.columns]
            st.dataframe(docs.drop(columns=hidden_cols), use_container_width=True, hide_index=True)
            st.markdown("### تنزيل مستند")
            doc_map = {f"#{int(r.id)} - {r.الملف} - {r.الشركة}": r for r in docs.itertuples()}
            pick = st.selectbox("المستند", list(doc_map.keys()))
            row = doc_map[pick]
            try:
                original = get_document_bytes(row)
                st.download_button("⬇️ تنزيل النسخة الأصلية من Dropbox", data=original, file_name=row.الملف)
                if getattr(row, "dropbox_path", None):
                    st.caption(f"☁️ محفوظ في Dropbox: {row.dropbox_path}")
            except Exception as e:
                st.error(f"تعذر جلب الملف الأصلي: {e}")

# -------------------- Prices --------------------
elif choice == "📈 مقارنة الأسعار":
    st.title("📈 مقارنة أسعار المصانع والوكالات")
    opts = company_options()
    if not opts:
        st.warning("أضف شركة أولاً.")
    else:
        with st.form("add_price"):
            a,b = st.columns(2)
            with a:
                comp_label = st.selectbox("المصنع / الشركة", list(opts.keys()), key="price_company")
                product = st.text_input("المنتج / القياس *", placeholder="مثال: 12R22.5")
                spec = st.text_input("المواصفة / Pattern")
                brand = st.text_input("البراند")
                qty = st.number_input("الكمية", min_value=0.0, step=1.0)
            with b:
                unit_price = st.number_input("سعر الوحدة", min_value=0.0, step=0.01)
                currency = st.selectbox("العملة", ["USD","RMB","EUR"], key="price_curr")
                incoterm = st.selectbox("Incoterm", ["EXW","FOB","CFR","CIF","DAP","DDP","Other"])
                quote_date = st.date_input("تاريخ العرض", value=date.today())
                valid_until = st.text_input("صالح لغاية")
                notes = st.text_area("ملاحظات")
            if st.form_submit_button("💾 حفظ عرض السعر"):
                if not product.strip():
                    st.error("اسم المنتج أو القياس مطلوب.")
                else:
                    execute("""INSERT INTO prices (company_id,product_name,specification,brand,quantity,unit_price,currency,incoterm,quote_date,valid_until,notes,created_at)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (opts[comp_label],product,spec,brand,qty,unit_price,currency,incoterm,str(quote_date),valid_until,notes,now_text()))
                    st.success("تم حفظ السعر.")
                    st.rerun()

        prices = fetch_df("""SELECT p.id,c.company_name AS الشركة,p.product_name AS المنتج,p.specification AS المواصفة,
                                  p.brand AS البراند,p.quantity AS الكمية,p.unit_price AS السعر,p.currency AS العملة,
                                  p.incoterm AS Incoterm,p.quote_date AS التاريخ,p.valid_until AS الصلاحية
                           FROM prices p JOIN companies c ON c.id=p.company_id ORDER BY p.id DESC""")
        if prices.empty:
            st.info("لا توجد عروض أسعار بعد.")
        else:
            st.dataframe(prices, use_container_width=True, hide_index=True)
            products = sorted([x for x in prices["المنتج"].dropna().unique() if str(x).strip()])
            if products:
                selected_product = st.selectbox("قارن منتجاً", products)
                comp = prices[prices["المنتج"] == selected_product].copy().sort_values("السعر")
                st.subheader(f"أفضل الأسعار لـ {selected_product}")
                st.dataframe(comp, use_container_width=True, hide_index=True)
                if not comp.empty:
                    best = comp.iloc[0]
                    st.success(f"أفضل سعر مسجل: {best['الشركة']} — {best['السعر']:,.2f} {best['العملة']} ({best['Incoterm']})")
                    chart_df = comp[["الشركة","السعر"]].set_index("الشركة")
                    st.bar_chart(chart_df)

# -------------------- Agencies --------------------
elif choice == "🤝 الوكالات":
    st.title("🤝 الوكالات والبراندات")
    opts = company_options()
    opts_with_none = {"بدون ربط بمصنع": None, **opts}
    with st.form("add_agency"):
        a,b = st.columns(2)
        with a:
            comp_label = st.selectbox("المصنع المرتبط", list(opts_with_none.keys()))
            brand = st.text_input("اسم البراند / الوكالة *")
            holder = st.text_input("صاحب الوكالة / الشركة")
            territory = st.text_input("المنطقة", value="Iraq")
        with b:
            exclusivity = st.selectbox("نوع الوكالة", ["حصرية","غير حصرية","قيد التفاوض","منتهية"])
            start = st.text_input("بداية الوكالة")
            end = st.text_input("نهاية الوكالة")
            notes = st.text_area("ملاحظات")
        if st.form_submit_button("💾 حفظ الوكالة"):
            if not brand.strip():
                st.error("اسم البراند مطلوب.")
            else:
                execute("""INSERT INTO agencies (company_id,brand_name,agency_holder,territory,exclusivity,start_date,end_date,notes,created_at)
                           VALUES (?,?,?,?,?,?,?,?,?)""",
                        (opts_with_none[comp_label],brand,holder,territory,exclusivity,start,end,notes,now_text()))
                st.success("تم حفظ الوكالة.")
                st.rerun()
    df = fetch_df("""SELECT a.id,COALESCE(c.company_name,'-') AS المصنع,a.brand_name AS البراند,a.agency_holder AS صاحب_الوكالة,
                          a.territory AS المنطقة,a.exclusivity AS النوع,a.start_date AS البداية,a.end_date AS النهاية,a.notes AS ملاحظات
                   FROM agencies a LEFT JOIN companies c ON c.id=a.company_id ORDER BY a.id DESC""")
    st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- Tasks --------------------
elif choice == "📝 المتابعة والمهام":
    st.title("📝 المتابعة والمهام")
    opts = {"عام": None, **company_options()}
    with st.form("add_task"):
        a,b = st.columns(2)
        with a:
            comp_label = st.selectbox("الشركة", list(opts.keys()))
            title = st.text_input("المهمة *", placeholder="مثال: متابعة أوراق BL الأصلية")
            details = st.text_area("التفاصيل")
        with b:
            due = st.date_input("موعد المتابعة", value=date.today())
            priority = st.selectbox("الأولوية", ["عالية","متوسطة","منخفضة"])
            status = st.selectbox("الحالة", ["مفتوحة","قيد المتابعة","مكتملة"])
        if st.form_submit_button("💾 حفظ المهمة"):
            if not title.strip():
                st.error("عنوان المهمة مطلوب.")
            else:
                execute("INSERT INTO notes_tasks (company_id,title,details,due_date,priority,status,created_at) VALUES (?,?,?,?,?,?,?)",
                        (opts[comp_label],title,details,str(due),priority,status,now_text()))
                st.success("تم حفظ المهمة.")
                st.rerun()
    df = fetch_df("""SELECT n.id,COALESCE(c.company_name,'عام') AS الشركة,n.title AS المهمة,n.details AS التفاصيل,
                          n.due_date AS الموعد,n.priority AS الأولوية,n.status AS الحالة
                   FROM notes_tasks n LEFT JOIN companies c ON c.id=n.company_id ORDER BY n.id DESC""")
    st.dataframe(df, use_container_width=True, hide_index=True)

# -------------------- MIDO AI --------------------
elif choice == "🤖 ميدو AI":
    st.title("🤖 ميدو AI الحقيقي")
    if not ai_ready():
        st.warning("AI بعده غير مربوط. Dropbox شغال، لكن حتى يحلل المستندات نحتاج مفتاح AI في Streamlit Secrets.")
        st.code('[ai]\napi_key = "YOUR_AI_API_KEY"\nmodel = "YOUR_MODEL_NAME"\n# base_url = "OPTIONAL_PROVIDER_URL"', language="toml")
        st.caption("لا تضع المفتاح في GitHub ولا ترسله داخل المحادثة. ضعه فقط في Streamlit Secrets.")
    else:
        st.success("🧠 MIDO AI جاهز للتحليل والحفظ في Dropbox")

    tab_files, tab_chat, tab_voice, tab_dev, tab_history = st.tabs(["📎 الملفات الذكية", "💬 محادثة", "🎙️ صوت ميدو", "🛠️ مطور ميدو", "🕘 سجل AI"])

    with tab_files:
        st.markdown("### ارفع الملفات فقط — ميدو يقرأها ويرتبها")
        st.caption("ارفع Invoice / PI / Packing List / BL / Certificate / Payment proof / Price list أو صورة. ميدو يحللها، يعرض النتيجة، وبعد تأكيدك يحفظ الأصل في Dropbox ويعبّي بيانات ERP.")
        user_msg = st.text_area("ملاحظة اختيارية لميدو", placeholder="مثال: هاي أوراق طلبية Linglong الجديدة، رتبها وخلي موعد الدفعة حسب الفاتورة.")
        uploads = st.file_uploader("ارفع ملف أو عدة ملفات", type=["pdf","png","jpg","jpeg","webp","xlsx","xls","csv","txt"], accept_multiple_files=True)
        if st.button("🧠 حلل ورتب الملفات", type="primary", disabled=not ai_ready()):
            if not uploads:
                st.error("ارفع ملف واحد على الأقل.")
            else:
                records = [{"name":u.name,"type":u.type,"bytes":u.getvalue()} for u in uploads]
                try:
                    with st.spinner("ميدو يقرأ المستندات ويستخرج المعلومات..."):
                        analysis = analyze_business_files(records, user_msg)
                    st.session_state["mido_ai_analysis"] = analysis
                    st.session_state["mido_ai_files"] = records
                    st.session_state["mido_ai_message"] = user_msg
                    st.success("تم التحليل. راجع النتيجة أدناه.")
                except Exception as e:
                    st.error(f"تعذر تحليل الملفات: {e}")

        analysis = st.session_state.get("mido_ai_analysis")
        records = st.session_state.get("mido_ai_files") or []
        if analysis:
            st.markdown("---")
            c1, c2, c3 = st.columns(3)
            c1.metric("الشركة", (analysis.get("company") or {}).get("name") or "غير محددة")
            c2.metric("نوع المستند", analysis.get("document_type") or "Other")
            c3.metric("ثقة AI", f"{float(analysis.get('confidence') or 0)*100:.0f}%")
            st.info(analysis.get("summary") or "تم التحليل.")
            warnings = analysis.get("warnings") or []
            if warnings:
                st.warning("تنبيهات ميدو: " + " | ".join(map(str, warnings)))
            with st.expander("🔍 كل المعلومات المستخرجة", expanded=True):
                st.json(analysis)
            if st.button("✅ تأكيد وحفظ كل المعلومات + النسخ الأصلية في Dropbox", type="primary"):
                if not dropbox_ready():
                    st.error("Dropbox غير مربوط، لذلك لا أقدر أحفظ النسخ الأصلية.")
                else:
                    try:
                        with st.spinner("جاري حفظ البيانات والملفات الأصلية..."):
                            result = save_ai_analysis(analysis, records, st.session_state.get("mido_ai_message", ""))
                        st.success("✅ تم الحفظ. ميدو ربط السجلات وحفظ الملفات الأصلية في Dropbox.")
                        st.write(result)
                        st.session_state.pop("mido_ai_analysis", None)
                        st.session_state.pop("mido_ai_files", None)
                        st.session_state.pop("mido_ai_message", None)
                    except Exception as e:
                        st.error(f"صار خطأ أثناء الحفظ: {e}")

    with tab_chat:
        st.markdown("### محادثة ميدو مع ذاكرة بياناتك")
        st.caption("تقدر تسأل عن الشركات والدفعات والشحنات والأسعار، وترفع ملفات ضمن المحادثة للتحليل.")
        chat_uploads = st.file_uploader("ملفات للمحادثة (اختياري)", type=["pdf","png","jpg","jpeg","webp","xlsx","xls","csv","txt"], accept_multiple_files=True, key="chat_files")
        q = st.text_area("احچي ويا ميدو", placeholder="مثال: هاي أوراق الشحنة الجديدة. حللها وقلّي شنو الناقص، أو شنو الدفعات الجاية؟", key="mido_chat_text")
        if st.button("💬 إرسال لميدو", type="primary", disabled=not ai_ready(), key="mido_chat_send"):
            if not q.strip() and not chat_uploads:
                st.error("اكتب رسالة أو ارفع ملفات.")
            else:
                try:
                    if chat_uploads:
                        records = [{"name":u.name,"type":u.type,"bytes":u.getvalue()} for u in chat_uploads]
                        with st.spinner("ميدو يحلل الملفات داخل المحادثة..."):
                            analysis = analyze_business_files(records, q)
                        st.session_state["mido_ai_analysis"] = analysis
                        st.session_state["mido_ai_files"] = records
                        st.session_state["mido_ai_message"] = q
                        ans = analysis.get("summary") or "تم تحليل الملفات. راجع نتيجة التحليل في تبويب الملفات الذكية واضغط تأكيد للحفظ."
                    else:
                        with st.spinner("ميدو يبحث ويفكر..."):
                            ans = ask_real_mido(q.strip())
                    execute("INSERT INTO chat_history (role,message,created_at) VALUES (?,?,?)", ("user",q,now_text()))
                    execute("INSERT INTO chat_history (role,message,created_at) VALUES (?,?,?)", ("assistant",ans,now_text()))
                    st.session_state["last_mido_answer"] = ans
                except Exception as e:
                    st.error(f"تعذر تشغيل MIDO AI: {e}")
        if st.session_state.get("last_mido_answer"):
            st.chat_message("assistant").markdown(st.session_state["last_mido_answer"])
            render_browser_speech(st.session_state["last_mido_answer"], key="speak_chat")
        hist = fetch_df("SELECT role,message,created_at FROM chat_history ORDER BY id DESC LIMIT 20")
        if not hist.empty:
            with st.expander("آخر المحادثات"):
                st.dataframe(hist, use_container_width=True, hide_index=True)

    with tab_voice:
        st.markdown("### 🎙️ احچي ويا ميدو بصوتك")
        if voice_ready():
            st.success("🎙️ الصوت جاهز — سجل كلامك وميدو يحوله إلى نص ويجاوب من قاعدة البيانات.")
        else:
            st.info("الصوت يحتاج AI مربوط. مع Gemini يستخدم نفس api_key و model الحاليين بدون مفتاح صوت إضافي.")
        audio = st.audio_input("اضغط وسجل كلامك", key="mido_audio_input")
        if audio is not None and ai_ready() and voice_ready():
            raw = audio.getvalue()
            digest = hashlib.sha256(raw).hexdigest()
            if st.session_state.get("_last_voice_digest") != digest:
                try:
                    with st.spinner("ميدو يسمع كلامك..."):
                        voice_text = transcribe_audio_bytes(raw, getattr(audio, "name", "voice.wav"))
                    st.session_state["_last_voice_digest"] = digest
                    st.session_state["_last_voice_text"] = voice_text
                except Exception as e:
                    st.error(f"تعذر تحويل الصوت إلى نص: {e}")
        voice_text = st.session_state.get("_last_voice_text", "")
        if voice_text:
            st.write("**أنت قلت:**", voice_text)
            if st.button("🤖 خلّي ميدو يجاوب", type="primary", key="voice_ask"):
                try:
                    ans = ask_real_mido(voice_text)
                    st.session_state["_last_voice_answer"] = ans
                    execute("INSERT INTO chat_history (role,message,created_at) VALUES (?,?,?)", ("user",voice_text,now_text()))
                    execute("INSERT INTO chat_history (role,message,created_at) VALUES (?,?,?)", ("assistant",ans,now_text()))
                except Exception as e:
                    st.error(f"تعذر جواب ميدو: {e}")
            if st.session_state.get("_last_voice_answer"):
                st.success(st.session_state["_last_voice_answer"])
                render_browser_speech(st.session_state["_last_voice_answer"], key="speak_voice")
        st.caption("ملاحظة: كلمة تنبيه دائمة مثل Alexa تحتاج خدمة Android تعمل بالخلفية؛ داخل Streamlit نستخدم زر التسجيل حتى لا يبقى المايك مفتوحاً دائماً.")

    with tab_dev:
        st.markdown("### 🛠️ مطور ميدو AI")
        st.caption("اكتب الميزة التي تريدها. ميدو يحولها فوراً إلى خطة تطوير واختبارات ويحفظ الطلب حتى نكمل عليه. حفاظاً على بياناتك لا يستبدل كود الإنتاج وحده بدون مراجعة واعتماد.")
        dev_req = st.text_area("شنو تريد تطور؟", placeholder="مثال: أريد ميدو يتابع ETA تلقائياً ويرسل تنبيه قبل الوصول بثلاث أيام.", key="dev_req")
        if st.button("🧠 حلل طلب التطوير", type="primary", disabled=not ai_ready(), key="dev_plan_btn"):
            if dev_req.strip():
                try:
                    with st.spinner("ميدو يجهز خطة التطوير..."):
                        plan = generate_development_plan(dev_req.strip())
                    rid = execute("INSERT INTO development_requests (request_text,ai_plan,status,created_at) VALUES (?,?,?,?)", (dev_req.strip(),plan,"جاهز للمراجعة",now_text()))
                    st.session_state["dev_plan"] = plan
                    st.success(f"تم حفظ طلب التطوير #{rid}.")
                except Exception as e:
                    st.error(f"تعذر إعداد خطة التطوير: {e}")
        if st.session_state.get("dev_plan"):
            st.markdown(st.session_state["dev_plan"])
        reqs = fetch_df("SELECT id,request_text,ai_plan,status,created_at FROM development_requests ORDER BY id DESC LIMIT 20")
        if not reqs.empty:
            st.dataframe(reqs[["id","request_text","status","created_at"]], use_container_width=True, hide_index=True)

    with tab_history:
        hist = fetch_df("SELECT a.id,a.created_at AS الوقت,COALESCE(c.company_name,'') AS الشركة,a.source_files AS الملفات,a.user_message AS الملاحظة,a.status AS الحالة FROM ai_ingestions a LEFT JOIN companies c ON c.id=a.company_id ORDER BY a.id DESC LIMIT 100")
        if hist.empty:
            st.info("ماكو عمليات AI محفوظة بعد.")
        else:
            st.dataframe(hist, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.caption("لأمان بياناتك، MIDO AI لا يغيّر كود البرنامج بنفسه. يقدر يفهم طلبات التطوير ويسجلها، لكن تعديل app.py يبقى تحديثاً واضحاً ومراجعاً.")



# -------------------- System status --------------------
elif choice == "🛡️ حالة النظام والنسخ الاحتياطية":
    st.title("🛡️ حالة النظام والنسخ الاحتياطية")
    c1,c2,c3 = st.columns(3)
    c1.metric("Dropbox", "مربوط ✅" if dropbox_ready() else "غير مربوط")
    c2.metric("MIDO AI", "جاهز ✅" if ai_ready() else "غير مربوط")
    c3.metric("Voice AI", "جاهز ✅" if voice_ready() else "غير مفعل")
    st.write("قاعدة البيانات المحلية:", DB_NAME, "—", f"{Path(DB_NAME).stat().st_size/1024:.1f} KB" if Path(DB_NAME).exists() else "غير موجودة")
    st.info("بعد كل تعديل، MIDO يرفع أحدث قاعدة بيانات إلى Dropbox. ويحتفظ أيضاً بلقطة احتياطية دورية داخل System/Backups.")
    if st.button("☁️ إنشاء Backup الآن", type="primary"):
        try:
            backup_database_to_dropbox()
            st.success("تم رفع أحدث نسخة من قاعدة البيانات إلى Dropbox.")
        except Exception as e:
            st.error(str(e))
    st.markdown("### ملفات الشحنات الكاملة")
    ships = fetch_df("""SELECT s.id,c.company_name,s.container_number,s.bl_number,s.status
                      FROM shipments s JOIN companies c ON c.id=s.company_id ORDER BY s.id DESC LIMIT 100""")
    if not ships.empty:
        labels = {f"#{int(r.id)} — {r.company_name} — {r.container_number or r.bl_number or 'شحنة'}": int(r.id) for r in ships.itertuples()}
        pick = st.selectbox("اختر شحنة لإنشاء ملف ZIP شامل", list(labels.keys()), key="sys_ship_zip")
        if st.button("📦 إنشاء/تحديث Shipment Package ZIP"):
            try:
                data, remote = build_shipment_package(labels[pick])
                st.success("تم إنشاء ملف واحد يحتوي كل النسخ الأصلية للشحنة وحفظه في Dropbox.")
                st.download_button("⬇️ تنزيل Shipment Package", data=data, file_name="MIDO_Shipment_Package.zip", mime="application/zip")
                st.caption(remote)
            except Exception as e:
                st.error(str(e))
