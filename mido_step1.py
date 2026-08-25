import streamlit as st
import sqlite3
from pathlib import Path

DB_FILE = Path("mido.db")

st.set_page_config(
    page_title="MIDO",
    page_icon="📦",
    layout="wide",
)

def init_database():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute(
        '''
        CREATE TABLE IF NOT EXISTS app_settings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            setting_key TEXT UNIQUE NOT NULL,
            setting_value TEXT
        )
        '''
    )
    conn.commit()
    conn.close()

init_database()

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

if page == "الرئيسية":
    st.title("📦 MIDO")
    st.subheader("نظام إدارة المعامل والطلبيات والمشحونات")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("المعامل", 0)
    c2.metric("الطلبيات", 0)
    c3.metric("الحاويات بالطريق", 0)
    c4.metric("الدفعات المعلقة", 0)

    st.info("المرحلة الأولى اكتملت: البرنامج يعمل وقاعدة البيانات جاهزة.")

elif page == "المعامل":
    st.title("🏭 المعامل")
    st.info("سنضيف هذا القسم في الخطوة القادمة.")

elif page == "الطلبيات":
    st.title("📋 الطلبيات")
    st.info("سنضيف هذا القسم بعد المعامل.")

elif page == "المشحونات":
    st.title("🚢 المشحونات")
    st.info("سيكون هنا جدول المشحونات الرئيسي وتصدير PDF.")

elif page == "الدفعات":
    st.title("💰 الدفعات")
    st.info("سيتم ربط الدفعات بالطلبيات والمشحونات.")

elif page == "المستندات":
    st.title("📁 المستندات")
    st.info("Invoice / Packing List / CO / COC / B/L / QR Code")

elif page == "التقارير":
    st.title("📄 التقارير")
    st.info("سيتم إضافة تقارير PDF وExcel لاحقاً.")
