import os
import sqlite3
import uuid
from datetime import datetime, date
from pathlib import Path

import pandas as pd
import streamlit as st
import dropbox
from dropbox.files import WriteMode

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
    """Keep the SQLite database itself backed up in Dropbox after every write."""
    if not dropbox_ready() or not Path(DB_NAME).exists():
        return
    try:
        remote = f"{dropbox_root()}/System/mido_database.db"
        upload_bytes_to_dropbox(Path(DB_NAME).read_bytes(), remote)
    except Exception:
        # A document save should not crash only because backup failed.
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


# -------------------- Sidebar --------------------
st.sidebar.title("🤖 MIDO")
st.sidebar.caption("مساعد محمد التجاري — v4.2")
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
]
choice = st.sidebar.radio("القسم", menu)
st.sidebar.markdown("---")
if dropbox_ready():
    st.sidebar.success("☁️ Dropbox مربوط — الملفات الأصلية والنسخة الاحتياطية تُحفظ في MIDO")
else:
    st.sidebar.warning("ℹ️ Dropbox غير مربوط بعد — البرنامج يعمل بشكل طبيعي، والربط السحابي نفعّله لاحقاً")

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
                doc_type = st.selectbox("نوع المستند", ["Proforma Invoice","Commercial Invoice","Packing List","Bill of Lading","Certificate of Origin","Insurance","Contract","Bank Slip","Customs","Other"])
                order_label = st.selectbox("ربط بطلبية", list(order_opts.keys()))
                invoice_label = st.selectbox("ربط بفاتورة", list(invoice_opts.keys()))
                shipment_label = st.selectbox("ربط بشحنة", list(shipment_opts.keys()))
            with b:
                up = st.file_uploader("اختر PDF / صورة", type=["pdf","png","jpg","jpeg"])
                notes = st.text_area("ملاحظات")
            if st.form_submit_button("⬆️ رفع وحفظ المستند"):
                if up is None:
                    st.error("اختر ملفاً أولاً.")
                else:
                    try:
                        path = save_uploaded_file(up, cid, doc_type)
                        execute("""INSERT INTO documents
                            (company_id,order_id,shipment_id,invoice_id,document_type,file_name,file_path,upload_date,notes,storage_provider,dropbox_path,file_size)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (cid, order_opts[order_label], shipment_opts[shipment_label], invoice_opts[invoice_label],
                             doc_type, up.name, path, now_text(), notes, "dropbox", path, len(up.getvalue())))
                        st.success("✅ تم رفع النسخة الأصلية مباشرة إلى Dropbox داخل مجلد MIDO وربطها بالسجل.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"تعذر رفع الملف إلى Dropbox: {e}")

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
    st.title("🤖 ميدو AI")
    st.info("هذه النسخة تعمل كمساعد بحث ذكي داخل قاعدة البيانات. ربط نموذج AI خارجي لاحقاً سيجعل ميدو يفهم الرسائل الحرة ويحوّلها تلقائياً إلى شركات وطلبيات وشحنات وفواتير.")

    q = st.text_input("اكتب سؤالك لميدو", placeholder="مثال: شنو الشحنات بالطريق؟ / دفعات Linglong / أسعار 12R22.5")
    if st.button("اسأل ميدو", type="primary") and q.strip():
        ql = q.lower()
        if any(k in ql for k in ["شحن", "حاوي", "بالطريق", "eta"]):
            df = fetch_df("""SELECT c.company_name AS الشركة,s.container_number AS الحاوية,s.bl_number AS BL,
                                  s.destination_port AS الوجهة,s.eta AS ETA,s.status AS الحالة
                           FROM shipments s JOIN companies c ON c.id=s.company_id
                           WHERE COALESCE(s.status,'') NOT LIKE '%استلام%'
                             AND COALESCE(s.status,'') NOT LIKE '%مستلمة%'
                             AND COALESCE(s.status,'') NOT LIKE '%Delivered%'
                             AND COALESCE(s.status,'') NOT LIKE '%مغلقة%'
                           ORDER BY s.eta ASC""")
            st.write(f"وجدت {len(df)} شحنة نشطة:")
            st.dataframe(df, use_container_width=True, hide_index=True)
        elif any(k in ql for k in ["دفع", "دفعة", "مستحق"]):
            df = fetch_df("""SELECT c.company_name AS الشركة,p.payment_type AS النوع,p.amount AS المبلغ,p.currency AS العملة,
                                  p.due_date AS الاستحقاق,p.status AS الحالة
                           FROM payments p JOIN companies c ON c.id=p.company_id
                           WHERE p.status NOT IN ('مدفوعة','تم الدفع') ORDER BY p.due_date ASC""")
            st.write(f"عندك {len(df)} دفعة غير مدفوعة:")
            st.dataframe(df, use_container_width=True, hide_index=True)
        elif any(k in ql for k in ["سعر", "اسعار", "أسعار", "قارن", "مقارنة"]):
            df = fetch_df("""SELECT c.company_name AS الشركة,p.product_name AS المنتج,p.specification AS المواصفة,
                                  p.unit_price AS السعر,p.currency AS العملة,p.incoterm AS Incoterm,p.quote_date AS التاريخ
                           FROM prices p JOIN companies c ON c.id=p.company_id ORDER BY p.product_name,p.unit_price ASC""")
            st.dataframe(df, use_container_width=True, hide_index=True)
        elif any(k in ql for k in ["شركة", "مصنع", "معمل", "مورد"]):
            df = fetch_df("SELECT id,company_name AS الشركة,contact_person AS المسؤول,phone AS الهاتف,email AS الإيميل,brands AS البراندات,payment_terms AS شروط_الدفع FROM companies ORDER BY company_name")
            st.dataframe(df, use_container_width=True, hide_index=True)
        elif any(k in ql for k in ["طلب", "طلبية"]):
            df = fetch_df("""SELECT c.company_name AS الشركة,o.order_number AS الطلبية,o.product_summary AS المنتجات,
                                  o.total_amount AS الإجمالي,o.currency AS العملة,o.paid_amount AS المدفوع,o.status AS الحالة
                           FROM orders o JOIN companies c ON c.id=o.company_id ORDER BY o.id DESC""")
            st.dataframe(df, use_container_width=True, hide_index=True)
        else:
            # General database search across main business text fields
            term = f"%{q}%"
            comp = fetch_df("SELECT id,company_name,contact_person,email,brands,notes FROM companies WHERE company_name LIKE ? OR contact_person LIKE ? OR brands LIKE ? OR notes LIKE ?", (term,term,term,term))
            orders = fetch_df("SELECT id,order_number,product_summary,status,notes FROM orders WHERE order_number LIKE ? OR product_summary LIKE ? OR notes LIKE ?", (term,term,term))
            shipments = fetch_df("SELECT id,container_number,bl_number,status,notes FROM shipments WHERE container_number LIKE ? OR bl_number LIKE ? OR notes LIKE ?", (term,term,term))
            if comp.empty and orders.empty and shipments.empty:
                st.warning("ما لقيت نتيجة مباشرة. جرّب اسم شركة، رقم حاوية، BL، منتج، أو كلمة من الملاحظات.")
            else:
                if not comp.empty:
                    st.subheader("شركات")
                    st.dataframe(comp, use_container_width=True, hide_index=True)
                if not orders.empty:
                    st.subheader("طلبيات")
                    st.dataframe(orders, use_container_width=True, hide_index=True)
                if not shipments.empty:
                    st.subheader("شحنات")
                    st.dataframe(shipments, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("المرحلة القادمة: AI حقيقي")
    st.markdown("""
    عندما نربط API لنموذج ذكاء اصطناعي، ستقدر تكتب مثلاً:

    **Linglong عندهم طلبية 4 حاويات، PI رقم LL-258 قيمتها 85,000 دولار، دفعنا 30%، ETA أم قصر 15 سبتمبر.**

    وميدو سيحوّلها تلقائياً إلى: شركة + طلبية + فاتورة + دفعة + شحنة + موعد متابعة، ثم يطلب منك التأكيد قبل الحفظ.
    """)
