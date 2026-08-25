import streamlit as st
import sqlite3
from pathlib import Path
from datetime import datetime, date

DB_FILE = Path("mido.db")
UPLOAD_ROOT = Path("uploads")
FACTORY_UPLOAD_ROOT = UPLOAD_ROOT / "factories"
ORDER_UPLOAD_ROOT = UPLOAD_ROOT / "orders"

st.set_page_config(
    page_title="MIDO",
    page_icon="📦",
    layout="wide",
)

# =========================================================
# DATABASE HELPERS
# =========================================================
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def table_exists(table_name):
    conn = get_conn()
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,)
    ).fetchone()
    conn.close()
    return row is not None

def get_columns(table_name):
    conn = get_conn()
    rows = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    conn.close()
    return {r["name"] for r in rows}

def add_missing_columns(table_name, definitions):
    existing = get_columns(table_name)
    conn = get_conn()
    for definition in definitions:
        column_name = definition.split()[0]
        if column_name not in existing:
            conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {definition}")
    conn.commit()
    conn.close()

def init_database():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT UNIQUE NOT NULL,
            setting_value TEXT
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS factories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_name TEXT NOT NULL,
            chinese_name TEXT,
            brand_name TEXT,
            contact_person TEXT,
            phone TEXT,
            wechat TEXT,
            email TEXT,
            address TEXT,
            website TEXT,
            bank_info TEXT,
            payment_terms TEXT,
            monthly_order_deadline INTEGER DEFAULT 20,
            private_notes TEXT,
            public_notes TEXT,
            active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS factory_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_id INTEGER NOT NULL,
            document_type TEXT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_id INTEGER NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            size TEXT,
            pattern TEXT,
            description TEXT,
            quantity INTEGER DEFAULT 0,
            unit_price REAL DEFAULT 0,
            amount REAL DEFAULT 0,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            document_type TEXT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

    # IMPORTANT: migrate old database safely instead of deleting it
    add_missing_columns("factories", [
        "chinese_name TEXT",
        "brand_name TEXT",
        "contact_person TEXT",
        "phone TEXT",
        "wechat TEXT",
        "email TEXT",
        "address TEXT",
        "website TEXT",
        "bank_info TEXT",
        "payment_terms TEXT",
        "monthly_order_deadline INTEGER DEFAULT 20",
        "private_notes TEXT",
        "public_notes TEXT",
        "active INTEGER DEFAULT 1",
        "created_at TEXT",
        "updated_at TEXT",
    ])

    add_missing_columns("orders", [
        "factory_id INTEGER",
        "pi_no TEXT",
        "order_date TEXT",
        "production_due_date TEXT",
        "order_status TEXT DEFAULT 'Draft'",
        "currency TEXT DEFAULT 'USD'",
        "pi_amount REAL DEFAULT 0",
        "expected_containers REAL DEFAULT 0",
        "ordered_containers REAL DEFAULT 0",
        "remaining_containers REAL DEFAULT 0",
        "shipping_estimate REAL DEFAULT 0",
        "total_estimated_amount REAL DEFAULT 0",
        "destination TEXT",
        "private_notes TEXT",
        "public_notes TEXT",
        "created_at TEXT",
        "updated_at TEXT",
    ])

    add_missing_columns("factory_documents", [
        "factory_id INTEGER",
        "document_type TEXT",
        "file_name TEXT",
        "file_path TEXT",
        "uploaded_at TEXT",
    ])

    add_missing_columns("order_items", [
        "order_id INTEGER",
        "size TEXT",
        "pattern TEXT",
        "description TEXT",
        "quantity INTEGER DEFAULT 0",
        "unit_price REAL DEFAULT 0",
        "amount REAL DEFAULT 0",
        "notes TEXT",
        "created_at TEXT",
    ])

    add_missing_columns("order_documents", [
        "order_id INTEGER",
        "document_type TEXT",
        "file_name TEXT",
        "file_path TEXT",
        "uploaded_at TEXT",
    ])

init_database()

def fetchall(query, params=()):
    conn = get_conn()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows

def fetchone(query, params=()):
    conn = get_conn()
    row = conn.execute(query, params).fetchone()
    conn.close()
    return row

def execute(query, params=()):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute(query, params)
    conn.commit()
    last_id = cur.lastrowid
    conn.close()
    return last_id

def save_uploaded_files(uploaded_files, target_dir, table_name, entity_field, entity_id, document_type):
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = 0

    for uploaded in uploaded_files:
        filename = Path(uploaded.name).name
        target = target_dir / filename

        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            target = target_dir / f"{target.stem}_{stamp}{target.suffix}"

        target.write_bytes(uploaded.getbuffer())

        execute(
            f"""
            INSERT INTO {table_name}
            ({entity_field}, document_type, file_name, file_path)
            VALUES (?, ?, ?, ?)
            """,
            (entity_id, document_type, target.name, str(target)),
        )
        saved += 1

    return saved

# =========================================================
# SIDEBAR
# =========================================================
st.sidebar.title("MIDO")
st.sidebar.caption("Business & Shipment Management System")

page = st.sidebar.radio(
    "القائمة الرئيسية",
    ["الرئيسية", "المعامل", "الطلبيات", "المشحونات", "الدفعات", "المستندات", "التقارير"],
)

# =========================================================
# HOME
# =========================================================
if page == "الرئيسية":
    st.title("📦 MIDO")
    st.subheader("نظام إدارة المعامل والطلبيات والمشحونات")

    factory_count = fetchone("SELECT COUNT(*) AS n FROM factories")["n"]
    order_count = fetchone("SELECT COUNT(*) AS n FROM orders")["n"]
    expected_containers = fetchone(
        "SELECT COALESCE(SUM(expected_containers),0) AS n FROM orders"
    )["n"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("المعامل", factory_count)
    c2.metric("الطلبيات", order_count)
    c3.metric("الحاويات المطلوبة", f"{expected_containers:g}")
    c4.metric("الدفعات المعلقة", 0)

    st.success("قاعدة البيانات تم تحديثها تلقائياً بدون حذف بياناتك.")

    st.subheader("آخر الطلبيات")
    recent_orders = fetchall("""
        SELECT o.id, o.pi_no, f.factory_name, o.order_status,
               o.pi_amount, o.currency, o.expected_containers,
               o.production_due_date
        FROM orders o
        LEFT JOIN factories f ON f.id = o.factory_id
        ORDER BY o.id DESC
        LIMIT 10
    """)

    if recent_orders:
        st.dataframe(
            [
                {
                    "ID": r["id"],
                    "PI No.": r["pi_no"] or "-",
                    "المعمل": r["factory_name"] or "-",
                    "الحالة": r["order_status"] or "-",
                    "PI Amount": r["pi_amount"] or 0,
                    "Currency": r["currency"] or "USD",
                    "الحاويات المطلوبة": r["expected_containers"] or 0,
                    "موعد الإنتاج": r["production_due_date"] or "-",
                }
                for r in recent_orders
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.info("لا توجد طلبيات بعد.")

# =========================================================
# FACTORIES
# =========================================================
elif page == "المعامل":
    st.title("🏭 المعامل الصينية")
    tab1, tab2 = st.tabs(["➕ إضافة معمل", "📋 المعامل المسجلة"])

    with tab1:
        with st.form("add_factory_form", clear_on_submit=True):
            c1, c2, c3 = st.columns(3)
            factory_name = c1.text_input("اسم المعمل *")
            chinese_name = c2.text_input("اسم المعمل بالصيني")
            brand_name = c3.text_input("العلامة / البراند")

            contact_person = c1.text_input("الشخص المسؤول")
            phone = c2.text_input("رقم الهاتف")
            wechat = c3.text_input("WeChat")

            email = c1.text_input("Email")
            website = c2.text_input("Website")
            monthly_order_deadline = c3.number_input(
                "موعد تجهيز الطلبية الشهري",
                min_value=1,
                max_value=31,
                value=20,
                step=1,
            )

            address = st.text_area("عنوان المعمل")
            bank_info = st.text_area("معلومات الحساب البنكي")
            payment_terms = st.text_area("شروط الدفع")

            n1, n2 = st.columns(2)
            private_notes = n1.text_area("🔒 ملاحظات خاصة")
            public_notes = n2.text_area("📝 ملاحظات عامة")

            submitted = st.form_submit_button("💾 حفظ المعمل", type="primary")

            if submitted:
                if not factory_name.strip():
                    st.error("يجب كتابة اسم المعمل.")
                else:
                    execute("""
                        INSERT INTO factories (
                            factory_name, chinese_name, brand_name,
                            contact_person, phone, wechat, email,
                            address, website, bank_info, payment_terms,
                            monthly_order_deadline, private_notes, public_notes,
                            active, created_at, updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, (
                        factory_name.strip(), chinese_name.strip(), brand_name.strip(),
                        contact_person.strip(), phone.strip(), wechat.strip(), email.strip(),
                        address.strip(), website.strip(), bank_info.strip(), payment_terms.strip(),
                        int(monthly_order_deadline), private_notes.strip(), public_notes.strip(),
                    ))
                    st.success("تم حفظ المعمل.")

    with tab2:
        factories = fetchall("SELECT * FROM factories ORDER BY active DESC, factory_name")
        if not factories:
            st.info("لا توجد معامل بعد.")
        else:
            for factory in factories:
                title = f"{'🟢' if factory['active'] else '⚪'} {factory['factory_name']}"
                if factory["brand_name"]:
                    title += f" — {factory['brand_name']}"

                with st.expander(title):
                    c1, c2, c3 = st.columns(3)
                    c1.write(f"**الشخص المسؤول:** {factory['contact_person'] or '-'}")
                    c1.write(f"**الهاتف:** {factory['phone'] or '-'}")
                    c1.write(f"**WeChat:** {factory['wechat'] or '-'}")
                    c2.write(f"**Email:** {factory['email'] or '-'}")
                    c2.write(f"**Website:** {factory['website'] or '-'}")
                    c2.write(f"**موعد الطلب:** قبل يوم {factory['monthly_order_deadline'] or 20}")
                    c3.write(f"**البراند:** {factory['brand_name'] or '-'}")
                    c3.write(f"**رقم المعمل:** {factory['id']}")

                    n1, n2 = st.columns(2)
                    n1.info(factory["private_notes"] or "لا توجد ملاحظات خاصة.")
                    n2.info(factory["public_notes"] or "لا توجد ملاحظات عامة.")

                    st.markdown("### 📁 ملفات المعمل")
                    doc_type = st.selectbox(
                        "نوع الملف",
                        [
                            "Company Registration",
                            "Contract / Agreement",
                            "Agency / Authorization",
                            "Bank Details",
                            "Price List",
                            "Certificate",
                            "PI / Quotation",
                            "Other",
                        ],
                        key=f"factory_doc_type_{factory['id']}",
                    )
                    uploads = st.file_uploader(
                        "ارفع ملف أو أكثر",
                        accept_multiple_files=True,
                        type=["pdf", "xlsx", "xls", "docx", "doc", "png", "jpg", "jpeg", "webp"],
                        key=f"factory_upload_{factory['id']}",
                    )

                    if uploads and st.button("⬆️ حفظ الملفات", key=f"save_factory_{factory['id']}"):
                        saved = save_uploaded_files(
                            uploads,
                            FACTORY_UPLOAD_ROOT / str(factory["id"]),
                            "factory_documents",
                            "factory_id",
                            factory["id"],
                            doc_type,
                        )
                        st.success(f"تم حفظ {saved} ملف/ملفات.")

# =========================================================
# ORDERS
# =========================================================
elif page == "الطلبيات":
    st.title("📋 الطلبيات")

    factories = fetchall("""
        SELECT id, factory_name, brand_name
        FROM factories
        WHERE COALESCE(active,1)=1
        ORDER BY factory_name
    """)

    if not factories:
        st.warning("أضف معمل أولاً.")
    else:
        factory_map = {
            f"{r['factory_name']} — {r['brand_name'] or 'بدون براند'}": r["id"]
            for r in factories
        }

        tab1, tab2 = st.tabs(["➕ طلبية جديدة", "📋 الطلبيات المسجلة"])

        with tab1:
            with st.form("new_order_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)
                factory_label = c1.selectbox("المعمل *", list(factory_map.keys()))
                pi_no = c2.text_input("PI No. / رقم الطلبية *")
                order_status = c3.selectbox(
                    "حالة الطلبية",
                    ["Draft", "Confirmed", "In Production", "Ready", "Partially Shipped", "Completed", "Cancelled"]
                )

                c4, c5, c6 = st.columns(3)
                order_date = c4.date_input("تاريخ الطلبية", value=date.today())
                production_due_date = c5.date_input("موعد الإنتاج / الجاهزية", value=date.today())
                currency = c6.selectbox("العملة", ["USD", "CNY", "EUR"])

                c7, c8, c9 = st.columns(3)
                pi_amount = c7.number_input("PI Amount", min_value=0.0, step=100.0, format="%.2f")
                expected_containers = c8.number_input("عدد الحاويات المطلوبة", min_value=0.0, step=0.5)
                ordered_containers = c9.number_input("عدد الحاويات المؤكدة", min_value=0.0, step=0.5)

                c10, c11, c12 = st.columns(3)
                shipping_estimate = c10.number_input("تكلفة الشحن التقديرية", min_value=0.0, step=100.0)
                destination = c11.text_input("الوجهة")
                total_estimated_amount = c12.number_input("الإجمالي التقديري", min_value=0.0, step=100.0)

                n1, n2 = st.columns(2)
                private_notes = n1.text_area("🔒 ملاحظات خاصة")
                public_notes = n2.text_area("📝 ملاحظات عامة")

                submit_order = st.form_submit_button("💾 حفظ الطلبية", type="primary")

                if submit_order:
                    if not pi_no.strip():
                        st.error("يجب كتابة PI No.")
                    else:
                        remaining = max(float(expected_containers) - float(ordered_containers), 0)
                        order_id = execute("""
                            INSERT INTO orders (
                                factory_id, pi_no, order_date, production_due_date,
                                order_status, currency, pi_amount,
                                expected_containers, ordered_containers,
                                remaining_containers, shipping_estimate,
                                total_estimated_amount, destination,
                                private_notes, public_notes, created_at, updated_at
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """, (
                            factory_map[factory_label], pi_no.strip(), str(order_date),
                            str(production_due_date), order_status, currency, float(pi_amount),
                            float(expected_containers), float(ordered_containers), remaining,
                            float(shipping_estimate), float(total_estimated_amount),
                            destination.strip(), private_notes.strip(), public_notes.strip(),
                        ))
                        st.success(f"تم حفظ الطلبية رقم {order_id}")

        with tab2:
            orders = fetchall("""
                SELECT o.*, f.factory_name, f.brand_name
                FROM orders o
                LEFT JOIN factories f ON f.id = o.factory_id
                ORDER BY o.id DESC
            """)

            if not orders:
                st.info("لا توجد طلبيات.")
            else:
                for order in orders:
                    header = f"#{order['id']} — {order['pi_no'] or 'بدون PI'} — {order['factory_name'] or 'بدون معمل'}"

                    with st.expander(header):
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("PI Amount", f"{float(order['pi_amount'] or 0):,.2f} {order['currency'] or 'USD'}")
                        m2.metric("الحاويات المطلوبة", f"{float(order['expected_containers'] or 0):g}")
                        m3.metric("المؤكد", f"{float(order['ordered_containers'] or 0):g}")
                        m4.metric("المتبقي", f"{float(order['remaining_containers'] or 0):g}")

                        st.markdown("### 📦 تفاصيل الطلبية")

                        with st.form(f"item_form_{order['id']}", clear_on_submit=True):
                            i1, i2, i3 = st.columns(3)
                            size = i1.text_input("القياس / Size")
                            pattern = i2.text_input("النقشة / Pattern")
                            description = i3.text_input("الوصف")

                            i4, i5, i6 = st.columns(3)
                            quantity = i4.number_input("العدد / Qty", min_value=0, step=1)
                            unit_price = i5.number_input("سعر الوحدة", min_value=0.0, step=0.01)
                            item_notes = i6.text_input("ملاحظة")

                            if st.form_submit_button("➕ إضافة السطر"):
                                amount = int(quantity) * float(unit_price)
                                execute("""
                                    INSERT INTO order_items (
                                        order_id, size, pattern, description,
                                        quantity, unit_price, amount, notes, created_at
                                    )
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                                """, (
                                    order["id"], size.strip(), pattern.strip(),
                                    description.strip(), int(quantity), float(unit_price),
                                    amount, item_notes.strip(),
                                ))
                                st.success("تمت إضافة السطر.")
                                st.rerun()

                        items = fetchall("SELECT * FROM order_items WHERE order_id=? ORDER BY id", (order["id"],))
                        if items:
                            st.dataframe(
                                [
                                    {
                                        "Size": x["size"],
                                        "Pattern": x["pattern"],
                                        "Description": x["description"],
                                        "Qty": x["quantity"],
                                        "Unit Price": x["unit_price"],
                                        "Amount": x["amount"],
                                        "Notes": x["notes"],
                                    }
                                    for x in items
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )

                        st.markdown("### 📁 ملفات الطلبية")
                        doc_type = st.selectbox(
                            "نوع الملف",
                            ["PI / Proforma Invoice", "Order Excel", "Order PDF", "Price List", "Specification", "Confirmation", "Other"],
                            key=f"order_doc_type_{order['id']}",
                        )
                        files = st.file_uploader(
                            "ارفع ملف أو أكثر",
                            accept_multiple_files=True,
                            type=["pdf", "xlsx", "xls", "csv", "docx", "doc", "png", "jpg", "jpeg", "webp"],
                            key=f"order_upload_{order['id']}",
                        )

                        if files and st.button("⬆️ حفظ ملفات الطلبية", key=f"save_order_{order['id']}"):
                            saved = save_uploaded_files(
                                files,
                                ORDER_UPLOAD_ROOT / str(order["id"]),
                                "order_documents",
                                "order_id",
                                order["id"],
                                doc_type,
                            )
                            st.success(f"تم حفظ {saved} ملف/ملفات.")

# =========================================================
# PLACEHOLDERS
# =========================================================
elif page == "المشحونات":
    st.title("🚢 المشحونات")
    st.info("الخطوة القادمة: المشحونات وربطها بالطلبيات + B/L + ETD + ETA + الحاويات + المستندات.")

elif page == "الدفعات":
    st.title("💰 الدفعات")
    st.info("سيتم ربط الدفعات بالطلبيات والمشحونات.")

elif page == "المستندات":
    st.title("📁 المستندات")
    st.info("Invoice / Packing List / CO / COC / B/L / QR Code")

elif page == "التقارير":
    st.title("📄 التقارير")
    st.info("سيتم بناء تقارير PDF وExcel للمشحونات.")
