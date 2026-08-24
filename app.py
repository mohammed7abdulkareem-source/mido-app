import sqlite3
import os
import streamlit as st
import pandas as pd

# 1. إعداد قاعدة البيانات المحلية
DB_NAME = "mido_database.db"
UPLOAD_DIR = "mido_dropbox_files"

if not os.path.exists(UPLOAD_DIR):
    os.makedirs(UPLOAD_DIR)

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # جدول الشركات الصينية والحسابات
    c.execute('''
        CREATE TABLE IF NOT EXISTS suppliers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_name TEXT,
            bank_account TEXT,
            order_details TEXT,
            payment_date TEXT,
            shipment_status TEXT,
            unit_price REAL
        )
    ''')
    # جدول أرشيف المستندات (دروب بوكس)
    c.execute('''
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id INTEGER,
            file_name TEXT,
            file_path TEXT,
            FOREIGN KEY(company_id) REFERENCES suppliers(id)
        )
    ''')
    conn.commit()
    conn.close()

init_db()

# 2. الواجهة الرئيسية للبرنامج
st.set_page_config(page_title="برنامج ميدو - Mido ERP", layout="wide")
st.title("📦 برنامج ميدو لإدارة التجارة والحسابات والاتصالات")

menu = ["الشركات الصينية والحسابات", "دروب بوكس ميدو (PDF)", "مقارنة الأسعار", "المساعد الصوتي والاتصالات (AI Caller)"]
choice = st.sidebar.selectbox("القائمة الرئيسية", menu)

# --- القسم الأول: إدارة الشركات والحسابات ---
if choice == "الشركات الصينية والحسابات":
    st.subheader("إضافة شركة صينية جديدة")
    with st.form("add_supplier_form"):
        col1, col2 = st.columns(2)
        with col1:
            name = st.text_input("اسم الشركة / المعمل الصيني")
            bank = st.text_input("الحساب البنكي (Bank Details)")
            price = st.number_input("سعر القطعة ($)", min_value=0.0)
        with col2:
            order = st.text_area("تفاصيل الطلبية والفاتورة")
            pay_date = st.date_input("موعد الدفعة القادمة")
            status = st.selectbox("حالة الشحنة بالطريق", ["في المعمل", "تم الشحن (بحر)", "تم الشحن (جو)", "وصلت للميناء", "مكتملة"])
        
        submit = st.form_submit_button("حفظ البيانات")
        if submit:
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("INSERT INTO suppliers (company_name, bank_account, order_details, payment_date, shipment_status, unit_price) VALUES (?,?,?,?,?,?)",
                      (name, bank, order, str(pay_date), status, price))
            conn.commit()
            conn.close()
            st.success(f"تم حفظ بيانات شركة {name} بنجاح!")

    st.markdown("---")
    st.subheader("سجل الشركات والشحنات والحسابات")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM suppliers", conn)
    conn.close()
    st.dataframe(df, use_container_width=True)

# --- القسم الثاني: دروب بوكس ميدو للملفات ---
elif choice == "دروب بوكس ميدو (PDF)":
    st.subheader("📁 دروب بوكس ميدو - أرشيف المستندات والملفات الأصلية")
    
    conn = sqlite3.connect(DB_NAME)
    suppliers_df = pd.read_sql_query("SELECT id, company_name FROM suppliers", conn)
    conn.close()
    
    if not suppliers_df.empty:
        selected_company = st.selectbox("اختر الشركة المرفق لها الملف", suppliers_df["company_name"].tolist())
        uploaded_file = st.file_uploader("رفع الفاتورة أو أوراق الشحن الأصلية (PDF)", type=["pdf", "png", "jpg"])
        
        if uploaded_file is not None:
            file_path = os.path.join(UPLOAD_DIR, uploaded_file.name)
            with open(file_path, "wb") as f:
                f.write(uploaded_file.getbuffer())
            
            comp_id = suppliers_df[suppliers_df["company_name"] == selected_company]["id"].values[0]
            conn = sqlite3.connect(DB_NAME)
            c = conn.cursor()
            c.execute("INSERT INTO documents (company_id, file_name, file_path) VALUES (?,?,?)", (int(comp_id), uploaded_file.name, file_path))
            conn.commit()
            conn.close()
            st.success(f"تم حفظ الملف {uploaded_file.name} في دروب بوكس ميدو بنجاح!")
    else:
        st.info("يرجى إضافة شركات صينية أولاً لتتمكن من رفع الملفات لها.")

# --- القسم الثالث: مقارنة الأسعار ---
elif choice == "مقارنة الأسعار":
    st.subheader("📊 مقارنة أسعار المعامل والوكالات")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT company_name, order_details, unit_price FROM suppliers", conn)
    conn.close()
    
    if not df.empty:
        st.bar_chart(data=df, x="company_name", y="unit_price")
        st.table(df.sort_values(by="unit_price"))
    else:
        st.info("لا توجد بيانات كافية لمقارنة الأسعار.")

# --- القسم الرابع: المساعد الصوتي والاتصالات ---
elif choice == "المساعد الصوتي والاتصالات (AI Caller)":
    st.subheader("📞 مركز الاتصالات الصوتي الذكي")
    st.write("يقوم المساعد الذكي بإجراء اتصالات هاتفية نيابة عنك للشركات والموردين أو العملاء.")
    
    phone_number = st.text_input("رقم الهاتف المطلوب الاتصال به (مع الرمز الدولي)", "+86...")
    call_script = st.text_area("الخدمة أو الرسالة المطلوبة من الذكاء الاصطناعي قوها/متابعتها خلال المكالمة", 
                               "مرحباً، أنا أتصل نيابة عن شركة ميدو لمتابعة الفاتورة رقم 102 وموعد شحن الحاوية.")
    
    if st.button("إجراء الاتصال الآن"):
        st.warning("⚡ للاتصال الحقيقي: يتم ربط هذا القسم بخدمة Twilio + Bland AI لتمكين النظام من إجراء المكالمات الصوتية المباشرة برقم خاص.")
        st.info(f"جاري تحضير الاتصال إلى {phone_number} مع الرسالة: '{call_script}'")
