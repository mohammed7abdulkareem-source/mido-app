import sqlite3
import os
import streamlit as st
import pandas as pd
from datetime import datetime

# إعدادات واجهة الموبايل العريضة والتصميم
st.set_page_config(page_title="برنامج ميدو - Mido ERP", layout="wide", initial_sidebar_state="expanded")

# 1. إعداد قاعدة البيانات ومجلد الملفات
DB_NAME = "mido_database.db"
UPLOAD_DIR = "mido_dropbox_files"

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT,
            bank_account TEXT,
            order_details TEXT,
            payment_date TEXT,
            shipment_status TEXT,
            unit_price REAL,
            total_amount REAL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            file_name TEXT,
            file_path TEXT,
            upload_date TEXT,
            FOREIGN KEY(company_id) REFERENCES suppliers(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# --- القائمة الجانبية للتنقل ---
st.sidebar.title("📱 برنامج ميدو الذكي")
st.sidebar.markdown("---")
menu = ["📊 لوحة التحكم الرئيسية", "🏢 الشركات الصينية والدفعات", "📁 دروب بوكس المستندات (PDF)", "📈 مقارنة أسعار المعامل", "📞 مركز الاتصالات الذكي (AI)"]
choice = st.sidebar.radio("اختر القسم:", menu)

# --- القسم الأول: لوحة التحكم (Dashboard) ---
if choice == "📊 لوحة التحكم الرئيسية":
    st.title("📊 لوحة الإحصائيات والتنبهات")
    
    conn = sqlite3.connect(DB_NAME)
    df_suppliers = pd.read_sql_query("SELECT * FROM suppliers", conn)
    df_docs = pd.read_sql_query("SELECT * FROM documents", conn)
    conn.close()
    
    col1, col2, col3 = st.columns(3)
    with col1:
        st.metric("إجمالي الشركات المسجلة", len(df_suppliers))
    with col2:
        total_val = df_suppliers["total_amount"].sum() if not df_suppliers.empty and "total_amount" in df_suppliers.columns else 0.0
        st.metric("إجمالي المبالغ والالتزامات", f"${total_val:,.2f}")
    with col3:
        st.metric("عدد الملفات بأرشيف دروب بوكس", len(df_docs))
        
    st.markdown("---")
    st.subheader("🚛 حالة الشحنات الحالية بالطريق")
    if not df_suppliers.empty:
        st.dataframe(df_suppliers[["company_name", "shipment_status", "payment_date", "unit_price"]], use_container_width=True)
    else:
        st.info("لا توجد شركات أو شحنات مسجلة حالياً.")

# --- القسم الثاني: إدخال الشركات والحسابات ---
elif choice == "🏢 الشركات الصينية والدفعات":
    st.title("🏢 إدارة الشركات الصينية والحسابات البنكية")
    
    with st.expander("➕ إضافة شركة صينية / شحنة جديدة", expanded=True):
        with st.form("add_supplier_form"):
            col1, col2 = st.columns(2)
            with col1:
                name = st.text_input("اسم الشركة / المصنع الصيني")
                bank = st.text_input("تفاصيل الحساب البنكي (IBAN / Bank Details)")
                price = st.number_input("سعر القطعة الواحدة ($)", min_value=0.0, step=0.1)
                total = st.number_input("إجمالي قيمة الفاتورة ($)", min_value=0.0, step=10.0)
            with col2:
                order = st.text_area("تفاصيل الطلبية والمنتجات")
                pay_date = st.date_input("موعد الدفعة القادمة")
                status = st.selectbox("حالة الشحنة", ["في المعمل (تحت التصنيع)", "تم الشحن بحري 🚢", "تم الشحن جوي ✈️", "وصلت للميناء/الجمارك 🛃", "تم الاستلام بالكامل ✅"])
            
            submit = st.form_submit_button("💾 حفظ البيانات بقاعدة البيانات")
            if submit:
                if name:
                    conn = sqlite3.connect(DB_NAME)
                    c = conn.cursor()
                    c.execute("INSERT INTO suppliers (company_name, bank_account, order_details, payment_date, shipment_status, unit_price, total_amount) VALUES (?,?,?,?,?,?,?)",
                              (name, bank, order, str(pay_date), status, price, total))
                    conn.commit()
                    conn.close()
                    st.success(f"تم حفظ بيانات شركة ({name}) بنجاح!")
                    st.rerun()
                else:
                    st.error("يرجى إدخال اسم الشركة على الأقل.")

    st.markdown("---")
    st.subheader("📋 قائمة الحسابات والشركات الصينية")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT id, company_name, bank_account, order_details, payment_date, shipment_status, unit_price, total_amount FROM suppliers", conn)
    conn.close()
    
    if not df.empty:
        st.dataframe(df, use_container_width=True)
    else:
        st.info("السجل فارغ حتى الآن.")

# --- القسم الثالث: دروب بوكس المستندات ---
elif choice == "📁 دروب بوكس المستندات (PDF)":
    st.title("📁 دروب بوكس ميدو - أرشيف المستندات والملفات الأصلية")
    
    conn = sqlite3.connect(DB_NAME)
    suppliers_df = pd.read_sql_query("SELECT id, company_name FROM suppliers", conn)
    conn.close()
    
    if not suppliers_df.empty:
        col_up, col_list = st.columns([1, 1])
        with col_up:
            st.subheader("رفع مستند جديد")
            selected_company = st.selectbox("اختر الشركة الصينية", suppliers_df["company_name"].tolist())
            uploaded_file = st.file_uploader("اختر ملف الفاتورة أو البوليصة (PDF / PNG / JPG)", type=["pdf", "png", "jpg", "jpeg"])
            
            if uploaded_file is not None:
                file_path = os.path.join(UPLOAD_DIR, uploaded_file.name)
                with open(file_path, "wb") as f:
                    f.write(uploaded_file.getbuffer())
                
                comp_id = suppliers_df[suppliers_df["company_name"] == selected_company]["id"].values[0]
                conn = sqlite3.connect(DB_NAME)
                c = conn.cursor()
                c.execute("INSERT INTO documents (company_id, file_name, file_path, upload_date) VALUES (?,?,?,?)", 
                          (int(comp_id), uploaded_file.name, file_path, str(datetime.now().strftime("%Y-%m-%d %H:%M"))))
                conn.commit()
                conn.close()
                st.success(f"تم حفظ الملف ({uploaded_file.name}) في السحابة بنجاح!")
        
        with col_list:
            st.subheader("📚 الملفات المخزنة بالدروب بوكس")
            conn = sqlite3.connect(DB_NAME)
            docs_df = pd.read_sql_query("""
                SELECT d.id, s.company_name, d.file_name, d.upload_date 
                FROM documents d 
                JOIN suppliers s ON d.company_id = s.id
            """, conn)
            conn.close()
            
            if not docs_df.empty:
                st.dataframe(docs_df, use_container_width=True)
            else:
                st.info("لا توجد ملفات مرفوعة بعد.")
    else:
        st.warning("يرجى إضافة شركات صينية في القسم السابق لتتمكن من رفع المستندات لها.")

# --- القسم الرابع: مقارنة الأسعار ---
elif choice == "📈 مقارنة أسعار المعامل":
    st.title("📈 مقارنة أسعار المعامل والوكالات")
    
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT company_name, unit_price, order_details FROM suppliers WHERE unit_price > 0", conn)
    conn.close()
    
    if not df.empty:
        st.subheader("مقارنة سعر القطعة بين المعامل ($)")
        st.bar_chart(data=df.set_index("company_name")["unit_price"])
        st.markdown("---")
        st.subheader("ترتيب المصانع من الأقل سعراً للقطعة")
        st.table(df.sort_values(by="unit_price"))
    else:
        st.info("أدخل أسعار قطع المنتجات في قسم الشركات لعرض رسم بياني بمقارنة الأسعار هنا.")

# --- القسم الخامس: المساعد الصوتي والاتصالات ---
elif choice == "📞 مركز الاتصالات الذكي (AI)":
    st.title("📞 المساعد الصوتي الآلي والاتصالات")
    st.info("💡 هذا القسم مخصص لإعطاء أمر للذكاء الاصطناعي ليقوم بالاتصال الهاتفي بالمصانع والعملاء ومتابعة الحاويات نيابة عنك.")
    
    col1, col2 = st.columns(2)
    with col1:
        phone = st.text_input("رقم الهاتف الدولي المراد الاتصال به", value="+86")
        language = st.selectbox("لغة المكالمة", ["الصينية (Mandarin)", "الإنكليزية (English)", "العربية"])
    with col2:
        task = st.text_area("تعليمات المكالمة للمساعد الذكي", "اتصل بالمصنع واستفسر عن سبب تأخير شحن الحاوية وموعد إرسال أوراق الشحن الأصلية.")
    
    if st.button("🚀 بدء الاتصال الهاتفي الذكي الآن"):
        st.success(f"جاري تحضير الاتصال برقم {phone} باللغة ({language})...")
        st.caption("ملاحظة: تفعيل الاتصال الهاتفي الفعلي يرتبط بشرائح الاتصال السحابية مثل (Bland AI / Twilio API).")
