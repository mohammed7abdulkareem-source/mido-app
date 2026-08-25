
import streamlit as st
import sqlite3
from datetime import date
from pathlib import Path

APP_TITLE = "MIDO Business System"
DB_PATH = Path("mido.db")

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="📦",
    layout="wide",
)

# -----------------------------
# Database
# -----------------------------
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS factories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            contact_person TEXT,
            phone TEXT,
            wechat TEXT,
            email TEXT,
            address TEXT,
            bank_info TEXT,
            payment_terms TEXT,
            brands TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_id INTEGER NOT NULL,
            order_no TEXT,
            order_date TEXT,
            due_date TEXT,
            expected_containers REAL DEFAULT 0,
            total_amount REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            status TEXT DEFAULT 'Draft',
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(factory_id) REFERENCES factories(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS shipments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            factory_id INTEGER NOT NULL,
            order_id INTEGER,
            shipment_no TEXT,
            bl_no TEXT,
            shipping_line TEXT,
            loading_port TEXT,
            destination_port TEXT,
            etd TEXT,
            eta TEXT,
            containers_count REAL DEFAULT 0,
            shipment_value REAL DEFAULT 0,
            currency TEXT DEFAULT 'USD',
            payment_status TEXT DEFAULT 'Not Paid',
            shipment_status TEXT DEFAULT 'Production',
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(factory_id) REFERENCES factories(id),
            FOREIGN KEY(order_id) REFERENCES orders(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            entity_id INTEGER NOT NULL,
            document_type TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_path TEXT,
            notes TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()
    conn.close()

init_db()

# -----------------------------
# Helpers
# -----------------------------
def fetchall(query, params=()):
    conn = get_conn()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows

def execute(query, params=()):
    conn = get_conn()
    conn.execute(query, params)
    conn.commit()
    conn.close()

def factory_options():
    rows = fetchall("SELECT id, name FROM factories ORDER BY name")
    return {f"{r['name']} (#{r['id']})": r["id"] for r in rows}

# -----------------------------
# Header
# -----------------------------
st.title("📦 MIDO Business System")
st.caption("Factories • Orders • Shipments • Documents • Payments • AI")

tabs = st.tabs([
    "🏠 Dashboard",
    "🏭 Factories",
    "🧾 Orders",
    "🚢 Shipments",
    "📁 Documents",
])

# -----------------------------
# Dashboard
# -----------------------------
with tabs[0]:
    factories_count = fetchall("SELECT COUNT(*) AS n FROM factories")[0]["n"]
    orders_count = fetchall("SELECT COUNT(*) AS n FROM orders")[0]["n"]
    shipments_count = fetchall("SELECT COUNT(*) AS n FROM shipments")[0]["n"]
    in_transit = fetchall("""
        SELECT COALESCE(SUM(containers_count), 0) AS n
        FROM shipments
        WHERE shipment_status IN ('Booked','Shipped','In Transit')
    """)[0]["n"]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Factories", factories_count)
    c2.metric("Orders", orders_count)
    c3.metric("Shipments", shipments_count)
    c4.metric("Containers on the way", f"{in_transit:g}")

    st.subheader("Recent shipments")
    recent = fetchall("""
        SELECT s.shipment_no, f.name AS factory, s.containers_count,
               s.etd, s.eta, s.payment_status, s.shipment_status
        FROM shipments s
        JOIN factories f ON f.id = s.factory_id
        ORDER BY s.id DESC
        LIMIT 20
    """)
    if recent:
        st.dataframe([dict(r) for r in recent], use_container_width=True, hide_index=True)
    else:
        st.info("No shipments yet.")

# -----------------------------
# Factories
# -----------------------------
with tabs[1]:
    st.subheader("Add Chinese Factory")

    with st.form("factory_form", clear_on_submit=True):
        c1, c2 = st.columns(2)
        name = c1.text_input("Factory name *")
        contact_person = c2.text_input("Contact person")
        phone = c1.text_input("Phone")
        wechat = c2.text_input("WeChat")
        email = c1.text_input("Email")
        address = c2.text_input("Address")
        bank_info = st.text_area("Bank information")
        payment_terms = st.text_input("Payment terms")
        brands = st.text_input("Brands")
        notes = st.text_area("Notes")
        submitted = st.form_submit_button("Save factory", type="primary")

        if submitted:
            if not name.strip():
                st.error("Factory name is required.")
            else:
                execute("""
                    INSERT INTO factories
                    (name, contact_person, phone, wechat, email, address,
                     bank_info, payment_terms, brands, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    name.strip(), contact_person, phone, wechat, email,
                    address, bank_info, payment_terms, brands, notes
                ))
                st.success("Factory saved.")

    st.divider()
    st.subheader("Factories")
    rows = fetchall("SELECT * FROM factories ORDER BY id DESC")
    if rows:
        st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)
    else:
        st.info("No factories yet.")

# -----------------------------
# Orders
# -----------------------------
with tabs[2]:
    st.subheader("Add Order")
    factories = factory_options()

    if not factories:
        st.warning("Add at least one factory first.")
    else:
        with st.form("order_form", clear_on_submit=True):
            factory_label = st.selectbox("Factory", list(factories.keys()))
            c1, c2, c3 = st.columns(3)
            order_no = c1.text_input("Order number")
            order_date = c2.date_input("Order date", value=date.today())
            due_date = c3.date_input("Required / production date", value=date.today())
            c4, c5, c6 = st.columns(3)
            expected_containers = c4.number_input("Expected containers", min_value=0.0, step=1.0)
            total_amount = c5.number_input("Total amount", min_value=0.0, step=100.0)
            currency = c6.selectbox("Currency", ["USD", "CNY", "EUR"])
            status = st.selectbox(
                "Order status",
                ["Draft", "Confirmed", "Production", "Ready", "Completed", "Cancelled"]
            )
            notes = st.text_area("Notes")
            submit_order = st.form_submit_button("Save order", type="primary")

            if submit_order:
                execute("""
                    INSERT INTO orders
                    (factory_id, order_no, order_date, due_date,
                     expected_containers, total_amount, currency, status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    factories[factory_label], order_no,
                    str(order_date), str(due_date),
                    expected_containers, total_amount,
                    currency, status, notes
                ))
                st.success("Order saved.")

    st.divider()
    st.subheader("Orders")
    rows = fetchall("""
        SELECT o.id, f.name AS factory, o.order_no, o.order_date,
               o.due_date, o.expected_containers, o.total_amount,
               o.currency, o.status
        FROM orders o
        JOIN factories f ON f.id = o.factory_id
        ORDER BY o.id DESC
    """)
    if rows:
        st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)
    else:
        st.info("No orders yet.")

# -----------------------------
# Shipments
# -----------------------------
with tabs[3]:
    st.subheader("Add Shipment")
    factories = factory_options()

    if not factories:
        st.warning("Add at least one factory first.")
    else:
        factory_label = st.selectbox(
            "Factory for shipment",
            list(factories.keys()),
            key="shipment_factory"
        )
        factory_id = factories[factory_label]

        order_rows = fetchall(
            "SELECT id, order_no FROM orders WHERE factory_id=? ORDER BY id DESC",
            (factory_id,)
        )
        order_map = {"No linked order": None}
        for r in order_rows:
            order_map[f"{r['order_no'] or 'Order'} (#{r['id']})"] = r["id"]

        with st.form("shipment_form", clear_on_submit=True):
            order_label = st.selectbox("Linked order", list(order_map.keys()))
            c1, c2 = st.columns(2)
            shipment_no = c1.text_input("Shipment number")
            bl_no = c2.text_input("B/L number")
            shipping_line = c1.text_input("Shipping line")
            loading_port = c2.text_input("Loading port")
            destination_port = c1.text_input("Destination port")

            c3, c4 = st.columns(2)
            etd = c3.date_input("ETD", value=date.today())
            eta = c4.date_input("ETA", value=date.today())

            c5, c6, c7 = st.columns(3)
            containers_count = c5.number_input("Containers", min_value=0.0, step=1.0)
            shipment_value = c6.number_input("Shipment value", min_value=0.0, step=100.0)
            currency = c7.selectbox("Currency", ["USD", "CNY", "EUR"], key="ship_currency")

            c8, c9 = st.columns(2)
            payment_status = c8.selectbox(
                "Payment status",
                ["Not Paid", "Deposit Paid", "Partially Paid", "Paid", "Supplier Confirmed"]
            )
            shipment_status = c9.selectbox(
                "Shipment status",
                ["Production", "Ready", "Booked", "Shipped", "In Transit",
                 "Arrived", "Customs", "Delivered"]
            )
            notes = st.text_area("Notes")
            submit_shipment = st.form_submit_button("Save shipment", type="primary")

            if submit_shipment:
                execute("""
                    INSERT INTO shipments
                    (factory_id, order_id, shipment_no, bl_no, shipping_line,
                     loading_port, destination_port, etd, eta,
                     containers_count, shipment_value, currency,
                     payment_status, shipment_status, notes)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    factory_id, order_map[order_label],
                    shipment_no, bl_no, shipping_line,
                    loading_port, destination_port,
                    str(etd), str(eta),
                    containers_count, shipment_value, currency,
                    payment_status, shipment_status, notes
                ))
                st.success("Shipment saved.")

    st.divider()
    st.subheader("Shipments")
    rows = fetchall("""
        SELECT s.id, f.name AS factory, s.shipment_no, s.bl_no,
               s.shipping_line, s.containers_count, s.etd, s.eta,
               s.payment_status, s.shipment_status
        FROM shipments s
        JOIN factories f ON f.id = s.factory_id
        ORDER BY s.id DESC
    """)
    if rows:
        st.dataframe([dict(r) for r in rows], use_container_width=True, hide_index=True)
    else:
        st.info("No shipments yet.")

# -----------------------------
# Documents
# -----------------------------
with tabs[4]:
    st.subheader("Shipment Documents")

    shipment_rows = fetchall("""
        SELECT s.id, s.shipment_no, f.name AS factory
        FROM shipments s
        JOIN factories f ON f.id = s.factory_id
        ORDER BY s.id DESC
    """)

    if not shipment_rows:
        st.warning("Add a shipment first.")
    else:
        shipment_map = {
            f"{r['factory']} — {r['shipment_no'] or 'Shipment'} (#{r['id']})": r["id"]
            for r in shipment_rows
        }
        selected = st.selectbox("Shipment", list(shipment_map.keys()))
        shipment_id = shipment_map[selected]

        doc_types = [
            "Commercial Invoice",
            "Packing List",
            "Certificate of Origin (CO)",
            "COC",
            "Bill of Lading",
            "QR Code",
            "Other",
        ]

        st.caption("This first version saves uploaded files locally. Dropbox will be connected in the next step.")

        for doc_type in doc_types:
            with st.expander(doc_type, expanded=False):
                files = st.file_uploader(
                    f"Upload {doc_type}",
                    accept_multiple_files=True,
                    key=f"{shipment_id}_{doc_type}"
                )
                if files:
                    save_dir = Path("uploads") / "shipments" / str(shipment_id) / doc_type.replace("/", "-")
                    save_dir.mkdir(parents=True, exist_ok=True)

                    for uploaded in files:
                        target = save_dir / uploaded.name
                        target.write_bytes(uploaded.getbuffer())

                        existing = fetchall("""
                            SELECT id FROM documents
                            WHERE entity_type='shipment'
                              AND entity_id=?
                              AND document_type=?
                              AND file_name=?
                        """, (shipment_id, doc_type, uploaded.name))

                        if not existing:
                            execute("""
                                INSERT INTO documents
                                (entity_type, entity_id, document_type, file_name, file_path)
                                VALUES ('shipment', ?, ?, ?, ?)
                            """, (shipment_id, doc_type, uploaded.name, str(target)))

                    st.success(f"{len(files)} file(s) saved.")

        st.divider()
        docs = fetchall("""
            SELECT document_type, file_name, created_at
            FROM documents
            WHERE entity_type='shipment' AND entity_id=?
            ORDER BY id DESC
        """, (shipment_id,))

        if docs:
            st.dataframe([dict(r) for r in docs], use_container_width=True, hide_index=True)
        else:
            st.info("No documents uploaded for this shipment yet.")

st.sidebar.success("MIDO V1 — New clean build")
st.sidebar.caption("Next: Dropbox → order item details → payments → MIDO AI")
