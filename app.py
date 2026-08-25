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
# DATABASE
# =========================================================
def get_conn():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def column_exists(table_name, column_name):
    conn = get_conn()
    cols = conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    conn.close()
    return any(c["name"] == column_name for c in cols)

def add_column_if_missing(table_name, definition):
    column_name = definition.split()[0]
    if not column_exists(table_name, column_name):
        conn = get_conn()
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
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(factory_id) REFERENCES factories(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_id INTEGER NOT NULL,
            pi_no TEXT,
            order_date TEXT,
            production_due_date TEXT,
            order_status TEXT DEFAULT 'Draft',
            currency TEXT DEFAULT 'USD',
            pi_amount REAL DEFAULT 0,
            expected_containers REAL DEFAULT 0,
            ordered_containers REAL DEFAULT 0,
            remaining_containers REAL DEFAULT 0,
            shipping_estimate REAL DEFAULT 0,
            total_estimated_amount REAL DEFAULT 0,
            destination TEXT,
            private_notes TEXT,
            public_notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(factory_id) REFERENCES factories(id)
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
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id INTEGER NOT NULL,
            document_type TEXT,
            file_name TEXT NOT NULL,
            file_path TEXT NOT NULL,
            uploaded_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)

    conn.commit()
    conn.close()

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

def safe_file_name(name):
    return Path(name).name

def save_uploaded_files(uploaded_files, target_dir, table_name, entity_field, entity_id, document_type):
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = 0

    for uploaded in uploaded_files:
        filename = safe_file_name(uploaded.name)
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
    [
        "الرئيسية",
        "المعامل",
        "الطلبيات",
        "المشحونات",
        "الدفعات",
        "المستندات",
        "التقارير",
    ],
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

    st.success("الخطوة 3: المعامل + الطلبيات + تفاصيل الطلبية جاهزة.")

    st.subheader("آخر الطلبيات")
    recent_orders = fetchall("""
        SELECT o.id, o.pi_no, f.factory_name, o.order_status,
               o.pi_amount, o.currency, o.expected_containers,
               o.production_due_date
        FROM orders o
        JOIN factories f ON f.id = o.factory_id
        ORDER BY o.id DESC
        LIMIT 10
    """)
    if recent_orders:
        st.dataframe(
            [
                {
                    "ID": r["id"],
                    "PI No.": r["pi_no"] or "-",
                    "المعمل": r["factory_name"],
                    "الحالة": r["order_status"],
                    "PI Amount": r["pi_amount"],
                    "Currency": r["currency"],
                    "الحاويات المطلوبة": r["expected_containers"],
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
        st.subheader("إضافة معمل جديد")

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

            c4, c5 = st.columns(2)
            private_notes = c4.text_area("🔒 ملاحظات خاصة")
            public_notes = c5.text_area("📝 ملاحظات عامة")

            submitted = st.form_submit_button("💾 حفظ المعمل", type="primary")

            if submitted:
                if not factory_name.strip():
                    st.error("يجب كتابة اسم المعمل.")
                else:
                    factory_id = execute("""
                        INSERT INTO factories (
                            factory_name, chinese_name, brand_name,
                            contact_person, phone, wechat, email,
                            address, website, bank_info, payment_terms,
                            monthly_order_deadline, private_notes, public_notes
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        factory_name.strip(),
                        chinese_name.strip(),
                        brand_name.strip(),
                        contact_person.strip(),
                        phone.strip(),
                        wechat.strip(),
                        email.strip(),
                        address.strip(),
                        website.strip(),
                        bank_info.strip(),
                        payment_terms.strip(),
                        int(monthly_order_deadline),
                        private_notes.strip(),
                        public_notes.strip(),
                    ))
                    st.success(f"تم حفظ المعمل بنجاح. رقم المعمل: {factory_id}")

    with tab2:
        factories = fetchall("""
            SELECT *
            FROM factories
            ORDER BY active DESC, factory_name COLLATE NOCASE
        """)

        if not factories:
            st.info("لا توجد معامل مسجلة بعد.")
        else:
            search_text = st.text_input("🔎 بحث عن معمل")

            filtered = []
            for r in factories:
                haystack = " ".join([
                    str(r["factory_name"] or ""),
                    str(r["brand_name"] or ""),
                    str(r["contact_person"] or ""),
                    str(r["email"] or ""),
                    str(r["wechat"] or ""),
                ]).lower()

                if not search_text.strip() or search_text.lower().strip() in haystack:
                    filtered.append(r)

            for factory in filtered:
                status = "🟢" if factory["active"] else "⚪"
                title = f"{status} {factory['factory_name']}"
                if factory["brand_name"]:
                    title += f" — {factory['brand_name']}"

                with st.expander(title):
                    c1, c2, c3 = st.columns(3)
                    c1.write(f"**الشخص المسؤول:** {factory['contact_person'] or '-'}")
                    c1.write(f"**الهاتف:** {factory['phone'] or '-'}")
                    c1.write(f"**WeChat:** {factory['wechat'] or '-'}")

                    c2.write(f"**Email:** {factory['email'] or '-'}")
                    c2.write(f"**Website:** {factory['website'] or '-'}")
                    c2.write(f"**موعد الطلب:** قبل يوم {factory['monthly_order_deadline']}")

                    c3.write(f"**الاسم بالصيني:** {factory['chinese_name'] or '-'}")
                    c3.write(f"**رقم المعمل:** {factory['id']}")
                    c3.write(f"**الحالة:** {'فعال' if factory['active'] else 'متوقف'}")

                    if factory["address"]:
                        st.write(f"**العنوان:** {factory['address']}")

                    n1, n2 = st.columns(2)
                    with n1:
                        st.markdown("##### 🔒 الملاحظات الخاصة")
                        st.info(factory["private_notes"] or "لا توجد")
                    with n2:
                        st.markdown("##### 📝 الملاحظات العامة")
                        st.info(factory["public_notes"] or "لا توجد")

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

                    if uploads and st.button(
                        "⬆️ حفظ الملفات",
                        key=f"save_factory_files_{factory['id']}",
                    ):
                        saved = save_uploaded_files(
                            uploads,
                            FACTORY_UPLOAD_ROOT / str(factory["id"]),
                            "factory_documents",
                            "factory_id",
                            factory["id"],
                            doc_type,
                        )
                        st.success(f"تم حفظ {saved} ملف/ملفات.")
                        st.rerun()

                    documents = fetchall("""
                        SELECT document_type, file_name, uploaded_at
                        FROM factory_documents
                        WHERE factory_id=?
                        ORDER BY id DESC
                    """, (factory["id"],))

                    if documents:
                        st.dataframe(
                            [
                                {
                                    "نوع الملف": d["document_type"],
                                    "اسم الملف": d["file_name"],
                                    "تاريخ الرفع": d["uploaded_at"],
                                }
                                for d in documents
                            ],
                            use_container_width=True,
                            hide_index=True,
                        )

# =========================================================
# ORDERS
# =========================================================
elif page == "الطلبيات":
    st.title("📋 الطلبيات")

    factories = fetchall("""
        SELECT id, factory_name, brand_name
        FROM factories
        WHERE active=1
        ORDER BY factory_name
    """)

    if not factories:
        st.warning("يجب إضافة معمل واحد على الأقل قبل إنشاء طلبية.")
    else:
        factory_map = {
            f"{r['factory_name']} — {r['brand_name'] or 'بدون براند'}": r["id"]
            for r in factories
        }

        tab1, tab2 = st.tabs(["➕ طلبية جديدة", "📋 الطلبيات المسجلة"])

        # -------------------------------------------------
        # ADD ORDER
        # -------------------------------------------------
        with tab1:
            st.subheader("إنشاء طلبية جديدة")

            with st.form("new_order_form", clear_on_submit=True):
                c1, c2, c3 = st.columns(3)

                factory_label = c1.selectbox("المعمل *", list(factory_map.keys()))
                pi_no = c2.text_input("PI No. / رقم الطلبية *")
                order_status = c3.selectbox(
                    "حالة الطلبية",
                    [
                        "Draft",
                        "Confirmed",
                        "In Production",
                        "Ready",
                        "Partially Shipped",
                        "Completed",
                        "Cancelled",
                    ],
                )

                c4, c5, c6 = st.columns(3)
                order_date = c4.date_input("تاريخ الطلبية", value=date.today())
                production_due_date = c5.date_input("موعد الإنتاج / الجاهزية", value=date.today())
                currency = c6.selectbox("العملة", ["USD", "CNY", "EUR"])

                c7, c8, c9 = st.columns(3)
                pi_amount = c7.number_input("PI Amount", min_value=0.0, step=100.0, format="%.2f")
                expected_containers = c8.number_input(
                    "عدد الحاويات المطلوبة",
                    min_value=0.0,
                    step=0.5,
                    format="%.2f",
                )
                ordered_containers = c9.number_input(
                    "عدد الحاويات المؤكدة حالياً",
                    min_value=0.0,
                    step=0.5,
                    format="%.2f",
                )

                c10, c11, c12 = st.columns(3)
                shipping_estimate = c10.number_input(
                    "تكلفة الشحن التقديرية",
                    min_value=0.0,
                    step=100.0,
                    format="%.2f",
                )
                destination = c11.text_input("الوجهة", placeholder="UMM QASR / AQABA / MERSIN")
                total_estimated_amount = c12.number_input(
                    "الإجمالي التقديري",
                    min_value=0.0,
                    step=100.0,
                    format="%.2f",
                    help="يمكنك وضع PI Amount + Shipping أو أي إجمالي تريد متابعته.",
                )

                n1, n2 = st.columns(2)
                private_notes = n1.text_area("🔒 ملاحظات خاصة")
                public_notes = n2.text_area("📝 ملاحظات عامة")

                submit_order = st.form_submit_button("💾 حفظ الطلبية", type="primary")

                if submit_order:
                    if not pi_no.strip():
                        st.error("يجب كتابة PI No. أو رقم الطلبية.")
                    else:
                        remaining = max(float(expected_containers) - float(ordered_containers), 0)

                        order_id = execute("""
                            INSERT INTO orders (
                                factory_id, pi_no, order_date, production_due_date,
                                order_status, currency, pi_amount,
                                expected_containers, ordered_containers,
                                remaining_containers, shipping_estimate,
                                total_estimated_amount, destination,
                                private_notes, public_notes
                            )
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            factory_map[factory_label],
                            pi_no.strip(),
                            str(order_date),
                            str(production_due_date),
                            order_status,
                            currency,
                            float(pi_amount),
                            float(expected_containers),
                            float(ordered_containers),
                            remaining,
                            float(shipping_estimate),
                            float(total_estimated_amount),
                            destination.strip(),
                            private_notes.strip(),
                            public_notes.strip(),
                        ))

                        st.success(f"تم إنشاء الطلبية بنجاح. رقمها داخل MIDO: {order_id}")

            st.caption("بعد حفظ الطلبية، افتحها من تبويب «الطلبيات المسجلة» لإضافة القياسات والنقشات والملفات.")

        # -------------------------------------------------
        # ORDER LIST + DETAILS
        # -------------------------------------------------
        with tab2:
            orders = fetchall("""
                SELECT o.*, f.factory_name, f.brand_name
                FROM orders o
                JOIN factories f ON f.id = o.factory_id
                ORDER BY o.id DESC
            """)

            if not orders:
                st.info("لا توجد طلبيات حتى الآن.")
            else:
                search = st.text_input(
                    "🔎 بحث",
                    placeholder="PI No. أو اسم المعمل أو البراند",
                    key="order_search",
                )

                filtered_orders = []
                for o in orders:
                    haystack = " ".join([
                        str(o["pi_no"] or ""),
                        str(o["factory_name"] or ""),
                        str(o["brand_name"] or ""),
                        str(o["order_status"] or ""),
                    ]).lower()
                    if not search.strip() or search.lower().strip() in haystack:
                        filtered_orders.append(o)

                st.caption(f"عدد الطلبيات: {len(filtered_orders)}")

                for order in filtered_orders:
                    header = (
                        f"#{order['id']} — {order['pi_no']} — "
                        f"{order['factory_name']} — {order['order_status']}"
                    )

                    with st.expander(header):
                        m1, m2, m3, m4 = st.columns(4)
                        m1.metric("PI Amount", f"{order['pi_amount']:,.2f} {order['currency']}")
                        m2.metric("الحاويات المطلوبة", f"{order['expected_containers']:g}")
                        m3.metric("المؤكد", f"{order['ordered_containers']:g}")
                        m4.metric("المتبقي", f"{order['remaining_containers']:g}")

                        c1, c2, c3 = st.columns(3)
                        c1.write(f"**المعمل:** {order['factory_name']}")
                        c1.write(f"**البراند:** {order['brand_name'] or '-'}")
                        c1.write(f"**تاريخ الطلبية:** {order['order_date'] or '-'}")

                        c2.write(f"**موعد الإنتاج:** {order['production_due_date'] or '-'}")
                        c2.write(f"**الوجهة:** {order['destination'] or '-'}")
                        c2.write(f"**حالة الطلبية:** {order['order_status']}")

                        c3.write(f"**Shipping Estimate:** {order['shipping_estimate']:,.2f}")
                        c3.write(f"**Total Estimate:** {order['total_estimated_amount']:,.2f}")
                        c3.write(f"**العملة:** {order['currency']}")

                        n1, n2 = st.columns(2)
                        with n1:
                            st.markdown("##### 🔒 الملاحظات الخاصة")
                            st.info(order["private_notes"] or "لا توجد")
                        with n2:
                            st.markdown("##### 📝 الملاحظات العامة")
                            st.info(order["public_notes"] or "لا توجد")

                        st.divider()
                        st.markdown("### 📦 تفاصيل الطلبية: القياس / النقشة / العدد / السعر")

                        with st.form(f"item_form_{order['id']}", clear_on_submit=True):
                            i1, i2, i3 = st.columns(3)
                            size = i1.text_input("القياس / Size")
                            pattern = i2.text_input("النقشة / Pattern")
                            description = i3.text_input("الوصف")

                            i4, i5, i6 = st.columns(3)
                            quantity = i4.number_input(
                                "العدد / Qty",
                                min_value=0,
                                step=1,
                                key=f"qty_{order['id']}",
                            )
                            unit_price = i5.number_input(
                                "سعر الوحدة",
                                min_value=0.0,
                                step=0.01,
                                format="%.2f",
                                key=f"unit_price_{order['id']}",
                            )
                            item_notes = i6.text_input("ملاحظة")

                            save_item = st.form_submit_button("➕ إضافة للسطر")

                            if save_item:
                                if not size.strip() and not description.strip():
                                    st.error("اكتب القياس أو الوصف على الأقل.")
                                else:
                                    amount = float(quantity) * float(unit_price)
                                    execute("""
                                        INSERT INTO order_items (
                                            order_id, size, pattern, description,
                                            quantity, unit_price, amount, notes
                                        )
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (
                                        order["id"],
                                        size.strip(),
                                        pattern.strip(),
                                        description.strip(),
                                        int(quantity),
                                        float(unit_price),
                                        amount,
                                        item_notes.strip(),
                                    ))
                                    st.success("تمت إضافة السطر.")
                                    st.rerun()

                        items = fetchall("""
                            SELECT *
                            FROM order_items
                            WHERE order_id=?
                            ORDER BY id
                        """, (order["id"],))

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

                            total_qty = sum(x["quantity"] or 0 for x in items)
                            total_items_amount = sum(x["amount"] or 0 for x in items)

                            q1, q2 = st.columns(2)
                            q1.metric("إجمالي القطع", f"{total_qty:,}")
                            q2.metric("إجمالي تفاصيل الطلبية", f"{total_items_amount:,.2f} {order['currency']}")
                        else:
                            st.caption("لا توجد تفاصيل قياسات مضافة بعد.")

                        st.divider()
                        st.markdown("### 📁 ملفات الطلبية")

                        order_doc_type = st.selectbox(
                            "نوع الملف",
                            [
                                "PI / Proforma Invoice",
                                "Order Excel",
                                "Order PDF",
                                "Price List",
                                "Specification",
                                "Confirmation",
                                "Other",
                            ],
                            key=f"order_doc_type_{order['id']}",
                        )

                        order_files = st.file_uploader(
                            "ارفع ملف الطلبية أو أكثر",
                            accept_multiple_files=True,
                            type=["pdf", "xlsx", "xls", "csv", "docx", "doc", "png", "jpg", "jpeg", "webp"],
                            key=f"order_upload_{order['id']}",
                        )

                        if order_files and st.button(
                            "⬆️ حفظ ملفات الطلبية",
                            key=f"save_order_files_{order['id']}",
                            type="primary",
                        ):
                            saved = save_uploaded_files(
                                order_files,
                                ORDER_UPLOAD_ROOT / str(order["id"]),
                                "order_documents",
                                "order_id",
                                order["id"],
                                order_doc_type,
                            )
                            st.success(f"تم حفظ {saved} ملف/ملفات.")
                            st.rerun()

                        order_docs = fetchall("""
                            SELECT document_type, file_name, uploaded_at
                            FROM order_documents
                            WHERE order_id=?
                            ORDER BY id DESC
                        """, (order["id"],))

                        if order_docs:
                            st.dataframe(
                                [
                                    {
                                        "نوع الملف": d["document_type"],
                                        "اسم الملف": d["file_name"],
                                        "تاريخ الرفع": d["uploaded_at"],
                                    }
                                    for d in order_docs
                                ],
                                use_container_width=True,
                                hide_index=True,
                            )
                        else:
                            st.caption("لا توجد ملفات لهذه الطلبية بعد.")

                        st.divider()
                        st.markdown("### ✏️ تحديث الطلبية")

                        with st.form(f"edit_order_{order['id']}"):
                            e1, e2, e3 = st.columns(3)

                            edit_status = e1.selectbox(
                                "الحالة",
                                [
                                    "Draft",
                                    "Confirmed",
                                    "In Production",
                                    "Ready",
                                    "Partially Shipped",
                                    "Completed",
                                    "Cancelled",
                                ],
                                index=[
                                    "Draft",
                                    "Confirmed",
                                    "In Production",
                                    "Ready",
                                    "Partially Shipped",
                                    "Completed",
                                    "Cancelled",
                                ].index(order["order_status"]) if order["order_status"] in [
                                    "Draft",
                                    "Confirmed",
                                    "In Production",
                                    "Ready",
                                    "Partially Shipped",
                                    "Completed",
                                    "Cancelled",
                                ] else 0,
                                key=f"edit_status_{order['id']}",
                            )

                            edit_expected = e2.number_input(
                                "الحاويات المطلوبة",
                                min_value=0.0,
                                step=0.5,
                                value=float(order["expected_containers"] or 0),
                                key=f"edit_expected_{order['id']}",
                            )

                            edit_ordered = e3.number_input(
                                "الحاويات المؤكدة",
                                min_value=0.0,
                                step=0.5,
                                value=float(order["ordered_containers"] or 0),
                                key=f"edit_ordered_{order['id']}",
                            )

                            edit_private = st.text_area(
                                "🔒 ملاحظات خاصة",
                                value=order["private_notes"] or "",
                                key=f"edit_order_private_{order['id']}",
                            )

                            edit_public = st.text_area(
                                "📝 ملاحظات عامة",
                                value=order["public_notes"] or "",
                                key=f"edit_order_public_{order['id']}",
                            )

                            update_order = st.form_submit_button("💾 حفظ التحديث")

                            if update_order:
                                remaining = max(float(edit_expected) - float(edit_ordered), 0)

                                execute("""
                                    UPDATE orders
                                    SET order_status=?,
                                        expected_containers=?,
                                        ordered_containers=?,
                                        remaining_containers=?,
                                        private_notes=?,
                                        public_notes=?,
                                        updated_at=CURRENT_TIMESTAMP
                                    WHERE id=?
                                """, (
                                    edit_status,
                                    float(edit_expected),
                                    float(edit_ordered),
                                    remaining,
                                    edit_private.strip(),
                                    edit_public.strip(),
                                    order["id"],
                                ))

                                st.success("تم تحديث الطلبية.")
                                st.rerun()

# =========================================================
# PLACEHOLDERS
# =========================================================
elif page == "المشحونات":
    st.title("🚢 المشحونات")
    st.info("الخطوة القادمة: إنشاء المشحونات وربطها بالطلبيات + B/L + ETD + ETA + الحاويات + المستندات.")

elif page == "الدفعات":
    st.title("💰 الدفعات")
    st.info("سيتم ربط الدفعات بالطلبيات والمشحونات.")

elif page == "المستندات":
    st.title("📁 المستندات")
    st.info("Invoice / Packing List / CO / COC / B/L / QR Code")

elif page == "التقارير":
    st.title("📄 التقارير")
    st.info("سيتم بناء تقارير PDF وExcel للمشحونات.")
