#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""خزنة — Telegram media saver bot (public).

Sends back videos and photos from a link you send. Uses yt-dlp for video/audio
and gallery-dl for image galleries. No cookies, no private accounts, no mass
scraping. Each request is limited and files above Telegram's bot limit are
rejected with a clear message.

Run: BOT_TOKEN=... python khazna_bot.py
"""
import asyncio
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yt_dlp
from telegram import Update
from telegram.constants import ChatAction, ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

# ---- limits ----
MAX_UPLOAD_BYTES = 45 * 1024 * 1024   # Telegram bot API hard limit is 50 MB
MAX_IMAGES = 10
RATE_WINDOW = 60
RATE_MAX = 5
URL_RE = re.compile(r"https?://[^\s<>\"']+", re.I)
VIDEO_EXT = {".mp4", ".webm", ".mkv", ".mov", ".m4v"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_rates = defaultdict(deque)
_log_lock = threading.Lock()


async def health_server():
    """Tiny HTTP endpoint so free hosts see an inbound port and stay 'up'."""
    port = int(os.environ.get("PORT", "7860"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write("خزنة ok".encode("utf-8"))

        def log_message(self, *a):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"health endpoint on :{port}", flush=True)


def allowed(chat_id):
    now = time.time()
    q = _rates[chat_id]
    while q and q[0] < now - RATE_WINDOW:
        q.popleft()
    if len(q) >= RATE_MAX:
        return False
    q.append(now)
    return True


def clean_url(u):
    return u.rstrip(".,;:!؟?)]}>\u200b")


def host_of(u):
    m = re.match(r"https?://([^/]+)", u, re.I)
    return (m.group(1).lower() if m else "").replace("www.", "")


def size_text(n):
    return f"{n / 1024 / 1024:.1f} MB"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "هلا 🤍 أنا خزنة. أرسل لي رابط فيديو أو صور من يوتيوب، إنستغرام، تيك توك "
        "أو أي موقع مدعوم، وأرسلها لك هنا.\n\n"
        "أوامر: /video رابط • /photo رابط • /help\n"
        "الحد: 45 MB للملف، 10 صور، 5 طلبات بالدقيقة."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "فقط أرسل الرابط وأنا أختار 🤍\n"
        "/video رابط — فيديو\n"
        "/photo رابط — صور من ألبوم أو منشور\n"
        "ما أفتح حسابات خاصة ولا أطلب كوكيز."
    )


async def download_video(update, url):
    out = Path(tempfile.mkdtemp(prefix="khazna-"))
    try:
        opts = {
            "outtmpl": str(out / "%(id)s.%(ext)s"),
            "format": "best[filesize<=45M]/best[filesize_approx<=45M]/best[ext=mp4][height<=720]/best[height<=720]/best",
            "quiet": True,
            "noplaylist": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "retries": 2,
        }
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: _ytdlp_download(opts, url))
        files = [p for p in out.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
        if not files:
            await update.message.reply_text("ما لقيت فيديو في الرابط 🤍 جرّب /photo.")
            return
        f = max(files, key=lambda p: p.stat().st_size)
        if f.stat().st_size > MAX_UPLOAD_BYTES:
            await update.message.chat.send_action(ChatAction.TYPING)
            f = await loop.run_in_executor(None, lambda: compress_video(f, MAX_UPLOAD_BYTES))
            if f.stat().st_size > MAX_UPLOAD_BYTES:
                await update.message.reply_text(
                    f"الملف كبير حتى بعد الضغط ({size_text(f.stat().st_size)}) 🤍 حد تليجرام 45 MB."
                )
                return
            cap_note = " (ضغطناه عشان يدخل)"

        cap = (info.get("title") or "خزنة")[:100] + cap_note if "cap_note" in locals() else (info.get("title") or "خزنة")[:100]
        with f.open("rb") as fh:
            await update.message.reply_video(fh, caption=cap)
    except Exception as e:
        await update.message.reply_text(f"ما قدرت أحمّله 🤍 ({str(e)[:120]})")
    finally:
        for p in out.iterdir():
            p.unlink(missing_ok=True)
        out.rmdir()


def compress_video(src: Path, limit: int) -> Path:
    """Shrink with ffmpeg until under Telegram's limit. Returns a new file."""
    tries = [(720, "28"), (540, "30"), (480, "32"), (360, "34")]
    for height, crf in tries:
        dst = src.with_name(src.stem + f"-{height}.mp4")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-i", str(src),
            "-vf", f"scale=-2:{height}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            str(dst),
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=180)
            if dst.exists() and dst.stat().st_size <= limit:
                src.unlink(missing_ok=True)
                return dst
        except subprocess.TimeoutExpired:
            pass
        dst.unlink(missing_ok=True)
    return src


def _ytdlp_download(opts, url):
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=True)


async def download_photos(update, url):
    out = Path(tempfile.mkdtemp(prefix="khazna-img-"))
    try:
        cmd = ["gallery-dl", "--range", f"1-{MAX_IMAGES}", "-d", str(out), url]
        loop = asyncio.get_running_loop()
        proc = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, capture_output=True, text=True, timeout=90),
        )
        files = [p for p in out.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT]
        if not files:
            await update.message.reply_text(
                "ما لقيت صور 🤍 ربما المنشور خاص أو الموقع يحتاج تسجيل دخول."
            )
            return
        files = sorted(files, key=lambda p: p.stat().st_size)[:MAX_IMAGES]
        for f in files:
            with f.open("rb") as fh:
                await update.message.reply_photo(fh)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("الرابط بطيء جداً 🤍 جرّب مرة ثانية.")
    except Exception as e:
        await update.message.reply_text(f"ما قدرت أحمّل الصور 🤍 ({str(e)[:120]})")
    finally:
        for p in out.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
        for p in sorted(out.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if p.is_dir():
                p.rmdir()
        out.rmdir()


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE, mode="auto"):
    cid = update.message.chat_id
    if not allowed(cid):
        await update.message.reply_text("هدّي سرعتك شوي 🤍 ٥ طلبات بالدقيقة، رجّع بعد شوي.")
        return
    url = clean_url(update.message.text or "")
    m = URL_RE.search(url)
    if not m:
        await update.message.reply_text("أرسل رابط يبدأ بـ https 🤍")
        return
    u = m.group(0)
    await update.message.chat.send_action(ChatAction.TYPING)
    if mode == "photo":
        await download_photos(update, u)
    elif mode == "video":
        await download_video(update, u)
    elif host_of(u) in ("instagram.com", "pixabay.com", "unsplash.com"):
        await download_photos(update, u)
    else:
        await download_video(update, u)


async def on_text(update, context):
    await handle(update, context, "auto")


async def video_cmd(update, context):
    await handle(update, context, "video")


async def photo_cmd(update, context):
    await handle(update, context, "photo")


def main():
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is required")
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("video", video_cmd))
    app.add_handler(CommandHandler("photo", photo_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    health_thread = threading.Thread(target=lambda: asyncio.run(health_server()), daemon=True)
    health_thread.start()
    print("خزنة bot started", flush=True)
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
