MIDO ERP v6 AI Suite
نسخة مطورة من MIDO مبنية على Streamlit + SQLite + Dropbox + AI API.
أهم المزايا
شركات، طلبيات، فواتير، دفعات، حسابات بنكية، أسعار، وكالات، مهام.
حالات الشحنات مع أرشفة الشحنات المستلمة وعدم ظهورها ضمن المهم.
رفع عدة ملفات لكل شحنة دفعة واحدة: Invoice, Packing List, Certificate of Origin, BL, QR Code وغيرها.
دعم PDF والصور وExcel (XLSX/XLS) وCSV وTXT.
حفظ النسخ الأصلية في Dropbox داخل مجلد كل شركة/شحنة.
إنشاء Shipment Package ZIP واحد يحتوي جميع الملفات الأصلية للشحنة مع manifest.
MIDO AI لتحليل الملفات وتصنيف كل ملف واستخراج معلومات الشركة/الطلبية/الفاتورة/الشحنة/الدفعة/البنك/الأسعار.
محادثة AI مع بيانات ERP.
Voice AI: تسجيل الصوت وتحويله إلى نص ثم رد ميدو، مع TTS اختياري.
Backup تلقائي لقاعدة البيانات إلى Dropbox مع snapshots دورية.
AI Developer Inbox لتحويل طلبات تطوير MIDO إلى خطة تنفيذ محفوظة، من دون استبدال كود الإنتاج تلقائياً.
Streamlit Secrets
احتفظ بالمفاتيح في Streamlit Secrets فقط، وليس GitHub.
[dropbox]
access_token = "YOUR_DROPBOX_ACCESS_TOKEN"
# root_folder = "/MIDO"

[ai]
api_key = "YOUR_AI_API_KEY"
model = "YOUR_TEXT_AND_VISION_MODEL"
# base_url = "OPTIONAL_PROVIDER_URL"

# لتفعيل الصوت، ضع أسماء النماذج المتاحة لدى مزود AI الخاص بك:
transcription_model = "YOUR_TRANSCRIPTION_MODEL"
tts_model = "YOUR_TTS_MODEL"
tts_voice = "YOUR_VOICE_NAME"
ملاحظات مهمة
Wake word دائم مثل Alexa يحتاج تطبيق Android Native وخدمة Microphone تعمل بالخلفية. نسخة Streamlit تستخدم زر تسجيل الصوت.
MIDO لا يغيّر app.py تلقائياً في الإنتاج. تبويب مطور ميدو يحفظ خطة التطوير حتى لا يؤدي طلب خاطئ إلى كسر النظام أو فقد البيانات.
يفضّل لاحقاً نقل SQLite إلى PostgreSQL عندما يصبح عدد المستخدمين كبيراً أو عند الحاجة للتزامن المتعدد.
