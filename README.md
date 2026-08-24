MIDO ERP
نسخة مطورة من برنامج ميدو لإدارة الشركات الصينية والطلبيات والفواتير والشحنات والدفعات والحسابات البنكية والمستندات والأسعار والوكالات.
التشغيل
pip install -r requirements.txt
streamlit run app.py
المزايا
ملف كامل لكل شركة صينية
طلبيات مستقلة وربطها بالشركة
فواتير PI/CI ومواعيد استحقاق
شحنات وحاويات وBL وETD/ETA
دفعات وربطها بالطلبية والفاتورة والحساب البنكي
حسابات بنكية للمصانع
رفع PDF والصور الأصلية وربطها بالشركة/الطلبية/الشحنة/الفاتورة
مقارنة أسعار حسب المنتج أو القياس
الوكالات والبراندات
مهام ومواعيد متابعة
MIDO AI للبحث داخل البيانات، مع بنية جاهزة لإضافة AI حقيقي لاحقاً
تنبيه أمني
SQLite والتخزين المحلي مناسبان للنسخة الداخلية الأولية. عند النشر على الإنترنت يجب إضافة تسجيل دخول وصلاحيات، تشفير، نسخ احتياطي، وتخزين ملفات آمن قبل وضع بيانات بنكية أو مستندات حساسة.
MIDO v3 — Dropbox
كل PDF أو صورة أصلية تُرفع مباشرة إلى Dropbox داخل /MIDO/Companies/<Company>/Documents/<Type>/.
قاعدة بيانات SQLite تُنسخ احتياطياً إلى /MIDO/System/mido_database.db بعد كل تعديل.
على تشغيل جديد يحاول MIDO استرجاع قاعدة البيانات من Dropbox تلقائياً.
لا تضع مفاتيح Dropbox في app.py أو GitHub. ضعها في Streamlit Cloud > App settings > Secrets.
راجع .streamlit/secrets.example.toml لمعرفة أسماء المفاتيح المطلوبة.
MIDO v4 additions
Automatic database migrations: old SQLite tables are upgraded without deleting data.
One-time import from the earliest suppliers schema when present.
Shipment received/archive workflow.
Received shipments are excluded from Dashboard important/active shipments and MIDO AI active-shipment results.
received_at date stored automatically when marking a shipment as received.
Original Dropbox document architecture from v3 retained.
