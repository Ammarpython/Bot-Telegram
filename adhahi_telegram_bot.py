"""
╔══════════════════════════════════════════════════════════════════╗
║     بوت تيليجرام — مراقبة ولايات أضاحي.dz                     ║
║     adhahi_telegram_bot.py                                       ║
║                                                                  ║
║  ✦ إشعارات فورية عند فتح/إغلاق الولايات                       ║
║  ✦ اشتراك بولاية واحدة أو أكثر برقمها                         ║
║  ✦ مراقبة كل 50ms — HTTP مباشر                                 ║
╚══════════════════════════════════════════════════════════════════╝

pip install python-telegram-bot requests
python adhahi_telegram_bot.py
"""

import asyncio
import sqlite3
import time
import logging
import sys
import os
from datetime import datetime

import requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    ContextTypes, MessageHandler, filters
)
from telegram.constants import ParseMode

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  ⚙️  الإعدادات
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

import os
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
API_URL         = "https://adhahi.dz/api/v1/public/wilaya-quotas"
POLL_INTERVAL   = 0.5           # 500ms على السيرفر (Railway أبطأ من المحلي)
HTTP_TIMEOUT    = 10            # رفعنا من 2 إلى 10 ثواني
DB_FILE         = "adhahi_subs.db"

API_HEADERS = {
    "accept":             "application/json",
    "referer":            "https://adhahi.dz/register",
    "user-agent":         (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "sec-ch-ua":          '"Chromium";v="125", "Not.A/Brand";v="24"',
    "sec-ch-ua-mobile":   "?0",
    "sec-ch-ua-platform": '"Windows"',
}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  📋  السجل
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s │ %(levelname)-8s │ %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("adhahi_bot.log", encoding="utf-8"),
    ]
)
log = logging.getLogger("AdhahiBot")
# تقليل ضجيج تيليجرام
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🗃️  قاعدة البيانات
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def init_db():
    con = sqlite3.connect(DB_FILE)
    cur = con.cursor()
    # جدول المستخدمين
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            chat_id   INTEGER PRIMARY KEY,
            username  TEXT,
            joined_at TEXT
        )
    """)
    # جدول الاشتراكات (chat_id + wilaya_code)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            chat_id      INTEGER,
            wilaya_code  TEXT,
            wilaya_name  TEXT,
            PRIMARY KEY (chat_id, wilaya_code)
        )
    """)
    con.commit()
    con.close()

def db_add_user(chat_id: int, username: str):
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "INSERT OR IGNORE INTO users VALUES (?,?,?)",
        (chat_id, username, datetime.now().isoformat())
    )
    con.commit()
    con.close()

def db_subscribe(chat_id: int, wilaya_code: str, wilaya_name: str):
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "INSERT OR IGNORE INTO subscriptions VALUES (?,?,?)",
        (chat_id, wilaya_code, wilaya_name)
    )
    con.commit()
    con.close()

def db_unsubscribe(chat_id: int, wilaya_code: str):
    con = sqlite3.connect(DB_FILE)
    con.execute(
        "DELETE FROM subscriptions WHERE chat_id=? AND wilaya_code=?",
        (chat_id, wilaya_code)
    )
    con.commit()
    con.close()

def db_unsubscribe_all(chat_id: int):
    con = sqlite3.connect(DB_FILE)
    con.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
    con.commit()
    con.close()

def db_get_subs(chat_id: int) -> list[dict]:
    con = sqlite3.connect(DB_FILE)
    rows = con.execute(
        "SELECT wilaya_code, wilaya_name FROM subscriptions WHERE chat_id=?",
        (chat_id,)
    ).fetchall()
    con.close()
    return [{"code": r[0], "name": r[1]} for r in rows]

def db_get_subscribers_for(wilaya_code: str) -> list[int]:
    con = sqlite3.connect(DB_FILE)
    rows = con.execute(
        "SELECT chat_id FROM subscriptions WHERE wilaya_code=?",
        (wilaya_code,)
    ).fetchall()
    con.close()
    return [r[0] for r in rows]

def db_get_all_subscribers() -> list[int]:
    """المشتركون في أي شيء."""
    con = sqlite3.connect(DB_FILE)
    rows = con.execute("SELECT DISTINCT chat_id FROM subscriptions").fetchall()
    con.close()
    return [r[0] for r in rows]

def db_stats() -> dict:
    con = sqlite3.connect(DB_FILE)
    users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    subs  = con.execute("SELECT COUNT(DISTINCT chat_id) FROM subscriptions").fetchone()[0]
    con.close()
    return {"users": users, "active": subs}

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  📡  مراقب الولايات
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

# حالة الولايات المحفوظة: {wilaya_code: bool}
_wilaya_state: dict[str, bool] = {}
# معلومات الولايات: {wilaya_code: {"ar": ..., "fr": ...}}
_wilaya_info:  dict[str, dict] = {}

# عداد الفحوصات
_check_count   = 0
_last_log_time = 0.0


def fetch_wilayas_sync() -> list | None:
    """جلب قائمة الولايات — sync."""
    try:
        r = requests.get(API_URL, headers=API_HEADERS, timeout=HTTP_TIMEOUT)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


async def monitor_loop(app: "Application"):
    """حلقة المراقبة — تعمل بشكل مستمر في الخلفية."""
    global _check_count, _last_log_time

    log.info("━" * 55)
    log.info("  📡 بدء مراقبة adhahi.dz — كل 50ms")
    log.info("━" * 55)

    session = requests.Session()
    session.headers.update(API_HEADERS)
    errors  = 0

    while True:
        t0 = time.time()
        _check_count += 1

        try:
            r = await asyncio.get_event_loop().run_in_executor(
                None,
                lambda: session.get(API_URL, timeout=HTTP_TIMEOUT)
            )

            if r.status_code != 200:
                errors += 1
                await asyncio.sleep(1)
                continue

            data   = r.json()
            errors = 0

            # ── تحليل التغييرات
            for w in data:
                code      = str(w.get("wilayaCode", ""))
                name_ar   = w.get("wilayaNameAr", "")
                name_fr   = w.get("wilayaNameFr", "")
                available = bool(w.get("available", False))

                # حفظ المعلومات
                _wilaya_info[code] = {"ar": name_ar, "fr": name_fr}

                prev = _wilaya_state.get(code)

                if prev is None:
                    # أول مرة — فقط تسجيل
                    _wilaya_state[code] = available

                elif prev != available:
                    # تغيّرت الحالة!
                    _wilaya_state[code] = available
                    ts = datetime.now().strftime("%H:%M:%S")

                    if available:
                        log.info(f"🟢 OUVERTE: {name_fr} ({name_ar}) — {ts}")
                        await notify_change(app, code, name_ar, name_fr, True, ts)
                    else:
                        log.info(f"🔴 FERMÉE: {name_fr} ({name_ar}) — {ts}")
                        await notify_change(app, code, name_ar, name_fr, False, ts)

            # ── log كل 10 ثوانٍ
            now = time.time()
            if now - _last_log_time >= 10:
                open_count = sum(1 for v in _wilaya_state.values() if v)
                log.info(
                    f"  📊 فحص #{_check_count:,} "
                    f"· مفتوحة: {open_count}/{len(_wilaya_state)}"
                )
                _last_log_time = now

        except Exception as e:
            errors += 1
            if errors <= 5:
                log.warning(f"⚠️ خطأ مراقبة #{errors}: {e}")
            await asyncio.sleep(2)
            continue

        # ── انتظار ذكي
        elapsed = time.time() - t0
        wait    = max(0.0, POLL_INTERVAL - elapsed)
        await asyncio.sleep(wait)


async def notify_change(
    app: "Application",
    code: str, name_ar: str, name_fr: str,
    opened: bool, ts: str
):
    """إرسال إشعار لكل من اشترك في هذه الولاية."""
    if opened:
        msg = (
            f"🟢 *ولاية مفتوحة\\!*\n\n"
            f"🏙️ *{escape(name_ar)}* — {escape(name_fr)}\n"
            f"🕐 الوقت: `{escape(ts)}`\n"
            f"🔗 [سجّل الآن](https://adhahi.dz/register)"
        )
    else:
        msg = (
            f"🔴 *ولاية أُغلقت*\n\n"
            f"🏙️ *{escape(name_ar)}* — {escape(name_fr)}\n"
            f"🕐 الوقت: `{escape(ts)}`"
        )

    # المشتركون في هذه الولاية بالذات
    subscribers = await asyncio.get_event_loop().run_in_executor(
        None, lambda: db_get_subscribers_for(code)
    )

    for chat_id in subscribers:
        try:
            await app.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode=ParseMode.MARKDOWN_V2,
                disable_web_page_preview=True,
            )
        except Exception as e:
            log.warning(f"⚠️ إرسال فشل لـ {chat_id}: {e}")

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🛠️  مساعدات
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

def escape(text: str) -> str:
    """Escape للـ MarkdownV2 — كل الرموز المحجوزة."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def safe(text: str) -> str:
    """نفس escape — اسم أقصر للاستخدام الداخلي."""
    return escape(str(text))


def get_current_status() -> list[dict]:
    """قائمة كل الولايات بحالتها الحالية."""
    result = []
    for code, available in sorted(_wilaya_state.items()):
        info = _wilaya_info.get(code, {})
        result.append({
            "code": code,
            "ar":   info.get("ar", ""),
            "fr":   info.get("fr", ""),
            "open": available,
        })
    return result

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🤖  أوامر البوت
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.effective_chat.id
    username = update.effective_user.username or update.effective_user.first_name or ""
    db_add_user(chat_id, username)

    text = (
        "🐑 *بوت أضاحي — مراقبة الولايات*\n\n"
        "أراقب موقع adhahi\\.dz كل 50ms وأُخطرك فور فتح أي ولاية\\.\n\n"
        "*الأوامر:*\n"
        "📋 /status — حالة كل الولايات الآن\n"
        "🔔 /subscribe — اشترك في ولاية\n"
        "🔕 /unsubscribe — إلغاء اشتراك\n"
        "📌 /mysubs — اشتراكاتي الحالية\n"
        "ℹ️ /help — المساعدة"
    )
    await update.message.reply_text(
        text, parse_mode=ParseMode.MARKDOWN_V2,
        disable_web_page_preview=True
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *دليل الاستخدام*\n\n"
        "1️⃣ اكتب /subscribe\n"
        "2️⃣ اختر الولاية أو الولايات التي تريد\n"
        "3️⃣ ستصلك رسالة فور فتح الولاية\n\n"
        "*مثال:*\n"
        "إذا اخترت *جيجل* \\(18\\)، ستصلك رسالة فور فتح التسجيل في جيجل\\.\n\n"
        "📋 /status — لمشاهدة كل الولايات الآن\n"
        "📌 /mysubs — لمعرفة اشتراكاتك الحالية"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """عرض حالة كل الولايات."""
    wilayas = get_current_status()

    if not wilayas:
        await update.message.reply_text(
            "⏳ جارٍ جلب البيانات\\.\\.\\. حاول مرة أخرى بعد ثانية\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    open_w   = [w for w in wilayas if w["open"]]
    closed_w = [w for w in wilayas if not w["open"]]
    ts       = datetime.now().strftime("%H:%M:%S")

    lines = [f"📊 *حالة الولايات — {escape(ts)}*\n"]

    if open_w:
        lines.append("*🟢 مفتوحة الآن:*")
        for w in open_w:
            lines.append(
                f"  • {escape(w['ar'])} \\({escape(w['fr'])}\\) "
                f"— رقم `{safe(w['code'])}`"
            )
    else:
        lines.append("*🔴 لا توجد ولايات مفتوحة حالياً*")

    lines.append(
        f"\n📉 مغلقة: {len(closed_w)}  ·  "
        f"📈 مفتوحة: {len(open_w)}  ·  "
        f"🔍 فحص \\#{_check_count:,}"
    )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2
    )


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """عرض قائمة الولايات للاشتراك."""
    wilayas = get_current_status()

    if not wilayas:
        await update.message.reply_text(
            "⏳ جارٍ تحميل قائمة الولايات\\.\\.\\. حاول بعد ثانية\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    chat_id     = update.effective_chat.id
    current_subs = {s["code"] for s in db_get_subs(chat_id)}

    # بناء الأزرار — 2 في كل صف
    keyboard = []
    row = []
    for w in sorted(wilayas, key=lambda x: int(x["code"]) if x["code"].isdigit() else 999):
        code   = w["code"]
        icon   = "🟢" if w["open"] else "⚫"
        check  = "✅ " if code in current_subs else ""
        # لا نستخدم MarkdownV2 في نص الأزرار
        label  = f"{icon} {check}{w['ar']} ({code})"
        row.append(InlineKeyboardButton(label, callback_data=f"sub_{code}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    # زر إلغاء الكل
    keyboard.append([
        InlineKeyboardButton("🗑️ إلغاء كل اشتراكاتي", callback_data="unsub_all")
    ])

    await update.message.reply_text(
        "🔔 *اختر الولاية \\(أو الولايات\\) التي تريد متابعتها:*\n"
        "🟢 مفتوحة الآن  ·  ⚫ مغلقة  ·  ✅ مشترك بالفعل",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """إلغاء اشتراك محدد."""
    chat_id = update.effective_chat.id
    subs    = db_get_subs(chat_id)

    if not subs:
        await update.message.reply_text(
            "ℹ️ ليس لديك أي اشتراكات حالياً\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    keyboard = []
    for s in subs:
        keyboard.append([
            InlineKeyboardButton(
                f"🗑️ {s['name']}",
                callback_data=f"unsub_{s['code']}"
            )
        ])
    keyboard.append([
        InlineKeyboardButton("🗑️ إلغاء الكل", callback_data="unsub_all")
    ])

    await update.message.reply_text(
        "🔕 *اختر الاشتراك الذي تريد إلغاءه:*",
        parse_mode=ParseMode.MARKDOWN_V2,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )


async def cmd_mysubs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """عرض اشتراكات المستخدم."""
    chat_id = update.effective_chat.id
    subs    = db_get_subs(chat_id)

    if not subs:
        await update.message.reply_text(
            "📭 لا توجد اشتراكات\\.\n\nاستخدم /subscribe للاشتراك\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    lines = ["📌 *اشتراكاتك الحالية:*\n"]
    for s in subs:
        state = _wilaya_state.get(s["code"])
        icon  = "🟢" if state else "🔴" if state is not None else "⚪"
        lines.append(
            f"  {icon} {escape(s['name'])} "
            f"\\(رقم {safe(s['code'])}\\)"
        )

    lines.append(f"\n_مجموع: {len(subs)} ولاية_")
    lines.append("اكتب /unsubscribe لإلغاء أي اشتراك\\.")

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN_V2
    )


# ── Callbacks للأزرار

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    chat_id = query.from_user.id
    data    = query.data

    await query.answer()

    if data == "unsub_all":
        db_unsubscribe_all(chat_id)
        await query.edit_message_text(
            "✅ تم إلغاء جميع اشتراكاتك\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    if data.startswith("sub_"):
        code = data[4:]
        info = _wilaya_info.get(code, {})
        name = f"{info.get('ar', code)} ({info.get('fr', '')})"
        db_subscribe(chat_id, code, name)

        subs  = db_get_subs(chat_id)
        names = ", ".join(escape(s["name"]) for s in subs)
        await query.edit_message_text(
            f"✅ تم الاشتراك في: *{escape(name)}*\n\n"
            f"📌 اشتراكاتك: {names}\n\n"
            f"سيصلك إشعار فور فتح أي من هذه الولايات\\.",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

    if data.startswith("unsub_"):
        code = data[6:]
        db_unsubscribe(chat_id, code)
        info = _wilaya_info.get(code, {})
        name = info.get("ar", code)
        await query.edit_message_text(
            f"✅ تم إلغاء الاشتراك في: *{escape(name)}*",
            parse_mode=ParseMode.MARKDOWN_V2
        )
        return

# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
#  🚀  التشغيل الرئيسي
# ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

async def prefetch_wilayas():
    """جلب أولي للولايات قبل قبول الأوامر."""
    global _wilaya_state, _wilaya_info
    log.info("⏳ جلب أولي لقائمة الولايات...")
    for attempt in range(10):
        data = await asyncio.get_event_loop().run_in_executor(
            None, fetch_wilayas_sync
        )
        if data:
            for w in data:
                code      = str(w.get("wilayaCode", ""))
                name_ar   = w.get("wilayaNameAr", "")
                name_fr   = w.get("wilayaNameFr", "")
                available = bool(w.get("available", False))
                _wilaya_info[code]  = {"ar": name_ar, "fr": name_fr}
                _wilaya_state[code] = available
            open_c = sum(1 for v in _wilaya_state.values() if v)
            log.info(f"✅ جُلبت {len(_wilaya_state)} ولاية — مفتوحة: {open_c}")
            return
        log.warning(f"⚠️ جلب أولي فشل ({attempt+1}/10)")
        await asyncio.sleep(1)
    log.error("❌ فشل الجلب الأولي")


async def post_init(app: "Application"):
    """جلب البيانات الأولية ثم بدء حلقة المراقبة."""
    await prefetch_wilayas()
    asyncio.create_task(monitor_loop(app))
    log.info("✅ حلقة المراقبة بدأت")


async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """معالج الأخطاء العامة — يمنع توقف البوت."""
    from telegram.error import Conflict, NetworkError, TimedOut
    err = ctx.error
    if isinstance(err, Conflict):
        log.error("❌ Conflict: أوقف أي نسخة أخرى من البوت!")
    elif isinstance(err, (NetworkError, TimedOut)):
        log.warning(f"⚠️ Network error (سيعيد المحاولة): {err}")
    else:
        log.error(f"❌ خطأ غير متوقع: {err}")


def main():
    init_db()

    log.info("═" * 55)
    log.info("  🚀 بوت أضاحي تيليجرام — جارٍ التشغيل")
    log.info("═" * 55)

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # ── تسجيل الأوامر
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("subscribe",   cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("mysubs",      cmd_mysubs))
    app.add_handler(CallbackQueryHandler(on_callback))

    # ── معالج الأخطاء — يمنع توقف البوت عند أي خطأ
    app.add_error_handler(error_handler)

    # ── تشغيل البوت
    log.info("📡 البوت يعمل — اضغط Ctrl+C للإيقاف")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,    # تجاهل الرسائل القديمة عند إعادة التشغيل
        close_loop=False,
    )


if __name__ == "__main__":
    main()
