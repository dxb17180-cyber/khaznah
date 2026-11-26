# خزنة 🤍 — بوت حفظ الفيديو والصور

بوت تليجرام عام: ترسل له رابط، يرسل لك الفيديو أو الصور.

## اللي يعمله
- يوتيوب، تيك توك، إنستغرام، إكس، ريديت وآلاف المواقع عبر yt-dlp
- صور الألبومات والمنشورات عبر gallery-dl
- حد 45 MB للملف، 10 صور، 5 طلبات بالدقيقة لكل شخص
- ما يفتح حسابات خاصة، ما يطلب كوكيز، ما يسحب حساب كامل
- يفضّل نسخة المصدر النظيف متى ما وفرتها المنصة؛ بعض المقاطع فيها العلامة مدمجة من المصدر وما ينفع يشيلها شي

## الرفع المجاني: Render (لا بطاقة مطلوبة)
1. ارفع الملفات إلى GitHub repo جديد (public ok):
   `khazna_bot.py`, `requirements.txt`, `Dockerfile`, `render.yaml`
2. افتح https://dashboard.render.com → سجّل الدخول → **New** → **Web Service**
3. اربط الـ repo → الاسم `khazna` → Runtime: **Docker** → Plan: **Free**
4. في Environment أضف المتغير: `BOT_TOKEN` = التوكن من @BotFather
5. `render.yaml` موجود يضبط Health Check تلقائياً على `/`
6. بعد النشر، جرّب البوت في تليجرام.

### important: keeping it online 24/7
- 750 free hours/month ≈ 31 × 24 = 744, so one service can run the entire month.
- Render free spins down after 15 idle minutes. The bot only polls outward, so nothing wakes it → set up a free external pinger (cron-job.org, UptimeRobot, or Render's own health check) to hit `https://<service>.onrender.com/` every 10 minutes. That keeps it awake continuously.
- With the pinger running, the service is effectively 24/7 and stays inside the 750-hour budget.
- Code uses `drop_pending_updates=False`, so messages that arrive while it is waking are queued by Telegram and answered after startup instead of being lost.

## لماذا ما استخدمنا Hugging Face؟
الصفحة تقول Gradio و Docker Spaces يحتاجون خطة مدفوعة، وStatic ما يشغّل بايثون. فما يصلح للبوت.

## لماذا ما استخدمنا السيرفر؟
لأنك طلبت استضافة مجانية منفصلة عن VPS.

## ملاحظات
- تليجرام يمنع رفع أكثر من 50 MB عبر البوت API → الحد 45 MB.
- البوت عام = أي أحد يقدر يستخدمه. أضفنا حدود بس ما نضمن عدم الإساءة.
- احترام حقوق المحتوى مسؤولية المستخدم.
