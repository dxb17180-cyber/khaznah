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
import html
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import yt_dlp
from fb_scraper import FacebookDownloader
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
LAST_EVENT = {"text": "", "stage": "", "error": ""}

# YouTube refuses known datacenter IPs (Render, VPS, CI). A residential proxy
# supplied through PROXY_URL is the only reliable fix for a public bot.
YOUTUBE_BLOCK_MARKERS = (
    "sign in to confirm you're not a bot",
    "sign in to confirm you are not a bot",
    "failed to extract any player response",
    "captcha",
)


def friendly_error(text):
    """Turn a raw yt-dlp error into a message a user can act on."""
    t = str(text).lower()
    if any(marker in t for marker in YOUTUBE_BLOCK_MARKERS):
        return "يوتيوب عطى رابط تحميل فاشل. جرّب بعد دقيقة، أو أرسل رابط ثاني."
    return f"ما قدرت أحمّله ({str(text)[:120]})"


async def health_server():
    """Tiny HTTP endpoint so free hosts see an inbound port and stay 'up'."""
    port = int(os.environ.get("PORT", "7860"))

    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            import json as _j
            if self.path.startswith("/debug"):
                body = _j.dumps(LAST_EVENT, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif self.path.startswith("/warp"):
                # Verifies the free Cloudflare WARP egress used for YouTube.
                proxy = os.environ.get("PROXY_URL", "socks5://127.0.0.1:1080").strip()
                try:
                    ip = requests.get(
                        "https://api.ipify.org", timeout=12,
                        proxies={"http": proxy, "https": proxy},
                    ).text.strip()
                    body = _j.dumps({"ok": True, "ip": ip}, ensure_ascii=False).encode("utf-8")
                except Exception as e:
                    body = _j.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif self.path.startswith("/snap"):
                # Self-check: one fixed public Snapchat Spotlight clip.
                try:
                    title, media = snap_media_info(
                        "https://www.snapchat.com/spotlight/W7_EDlXWTBiXAEEniNoMPwAAYdnR4eHJ6YXFjAZ9jqOLlAZ9jqIFXAAAAAQ")
                    body = _j.dumps({"ok": bool(media), "title": title[:80]},
                                    ensure_ascii=False).encode("utf-8")
                except Exception as e:
                    body = _j.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif self.path.startswith("/fb"):
                # Self-check: one fixed public Facebook reel, no login.
                try:
                    info = FacebookDownloader().get_fb_video_info(
                        "https://www.facebook.com/reel/1195289147628387")
                    body = _j.dumps({
                        "ok": bool(info.get("hd") or info.get("sd")),
                        "title": (info.get("title") or "")[:80],
                    }, ensure_ascii=False).encode("utf-8")
                except Exception as e:
                    body = _j.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            elif self.path.startswith("/yt"):
                # Self-check: one fixed public video through the live egress.
                proxy = os.environ.get("PROXY_URL", "socks5://127.0.0.1:1080").strip()
                check_opts = {
                    "quiet": True, "no_warnings": True, "noplaylist": True,
                    "extractor_args": {"youtube": {"player_client": ["android", "tv", "web_safari"]}},
                }
                if proxy:
                    check_opts["proxy"] = proxy
                try:
                    info = _ytdlp_download(
                        check_opts, "https://www.youtube.com/shorts/Kg4uxRWyN04")
                    body = _j.dumps({
                        "ok": True,
                        "title": (info.get("title") or "")[:80],
                        "formats": len(info.get("formats") or []),
                    }, ensure_ascii=False).encode("utf-8")
                except Exception as e:
                    body = _j.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
            else:
                body = "خزن ok v15".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
        "مرحباً، أنا خزن. أحفظ لك الفيديوهات والصور من الروابط العامة.\n\n"
        "المنصات المدعومة:\n"
        "يوتيوب، إنستغرام، تيك توك، فيسبوك، سناب شات، إكس، ريديت، "
        "ثريدز، بينترست، تويتش، فيميو، ديلي موشن، لينكد إن، تيليجرام.\n\n"
        "الأوامر:\n"
        "/video رابط — تنزيل فيديو\n"
        "/photo رابط — تنزيل صور\n"
        "/help — عرض المساعدة\n\n"
        "الحدود: 45 ميجابايت للملف، 10 صور، 5 طلبات في الدقيقة.\n"
        "المحتوى الخاص أو الذي يتطلب تسجيل دخول غير مدعوم."
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "أرسل الرابط وسأختار نوع المحتوى تلقائياً.\n\n"
        "/video رابط — فيديو\n"
        "/photo رابط — صور من ألبوم أو منشور\n\n"
        "المنصات المدعومة: يوتيوب، إنستغرام، تيك توك، فيسبوك، سناب شات، إكس، "
        "ريديت، ثريدز، بينترست، تويتش، فيميو، ديلي موشن، لينكد إن، تيليجرام.\n\n"
        "لا أفتح الحسابات الخاصة ولا أطلب بيانات دخول."
    )


async def download_video(update, url):
    out = Path(tempfile.mkdtemp(prefix="khazna-"))
    try:
        opts = {
            "outtmpl": str(out / "%(id)s.%(ext)s"),
            "format": "best[filesize<=45M]/best[filesize_approx<=45M]/best[ext=mp4][height<=480]/best[height<=480]/worst[ext=mp4]/worst",
            "quiet": True,
            "noplaylist": True,
            "no_warnings": True,
            "socket_timeout": 25,
            "retries": 2,
            "extractor_args": {"youtube": {"player_client": ["android", "tv", "web_safari"]}},
        }
        proxy = os.environ.get("PROXY_URL", "socks5://127.0.0.1:1080").strip()
        if proxy:
            opts["proxy"] = proxy
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(None, lambda: _ytdlp_download(opts, url))
        files = [p for p in out.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXT]
        if not files:
            await update.message.reply_text("ما لقيت فيديو في الرابط. جرّب /photo.")
            return
        f = max(files, key=lambda p: p.stat().st_size)
        if f.stat().st_size > MAX_UPLOAD_BYTES:
            await update.message.chat.send_action(ChatAction.TYPING)
            f = await loop.run_in_executor(None, lambda: compress_video(f, MAX_UPLOAD_BYTES))
            if f.stat().st_size > MAX_UPLOAD_BYTES:
                await update.message.reply_text(
                    f"الملف كبير حتى بعد الضغط ({size_text(f.stat().st_size)}) حد تليجرام 45 MB."
                )
                return
            cap_note = " (ضغطناه عشان يدخل)"

        cap = (info.get("title") or "خزنة")[:100] + cap_note if "cap_note" in locals() else (info.get("title") or "خزنة")[:100]
        with f.open("rb") as fh:
            await update.message.reply_video(fh, caption=cap)
        LAST_EVENT["stage"] = "done"
        LAST_EVENT["error"] = ""
    except Exception as e:
        LAST_EVENT["stage"] = "error"
        LAST_EVENT["error"] = str(e)[:300]
        await update.message.reply_text(friendly_error(e))
    finally:
        for p in out.iterdir():
            p.unlink(missing_ok=True)
        out.rmdir()


SNAP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


def snap_media_info(url):
    """Public Snapchat Spotlight: og:video / <video src> carries the MP4."""
    r = requests.get(url, timeout=25, headers={
        "User-Agent": SNAP_UA, "Accept-Language": "en-US,en;q=0.9"})
    r.raise_for_status()
    m = (re.search(r'<meta property="og:video" content="([^"]+)"', r.text)
         or re.search(r"<video[^>]+src=\"([^\"]+)\"", r.text))
    if not m:
        raise RuntimeError("Snapchat ما أعطى رابط فيديو. الرابط ممكن يكون خاص أو محذوف.")
    media = html.unescape(m.group(1))
    tm = re.search(r'<meta property="og:title" content="([^"]+)"', r.text)
    title = html.unescape(tm.group(1)) if tm else "خزن"
    return title, media


def strip_snap_watermark(path: Path) -> Path:
    """Snapchat bakes its logo + username into the top-left of the web video.

    Blur that corner with ffmpeg delogo; keep the original if ffmpeg fails.
    """
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=s=x:p=0", str(path)],
            capture_output=True, text=True, timeout=20)
        w, h = (int(v) for v in probe.stdout.strip().split("x")[:2])
        x, y = int(w * 0.015), int(h * 0.047)
        dw, dh = int(w * 0.34), int(h * 0.182)
        out = path.with_name("clean.mp4")
        r = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
             "-vf", f"delogo=x={x}:y={y}:w={dw}:h={dh}", "-c:a", "copy", str(out)],
            capture_output=True, timeout=90)
        if r.returncode == 0 and out.exists() and out.stat().st_size > 0:
            path.unlink(missing_ok=True)
            return out
    except Exception:
        pass
    return path


async def download_snap(update, url):
    out = Path(tempfile.mkdtemp(prefix="khazna-snap-"))
    try:
        loop = asyncio.get_running_loop()
        title, media = await loop.run_in_executor(None, lambda: snap_media_info(url))

        def fetch():
            r = requests.get(media, timeout=60, headers={"User-Agent": SNAP_UA})
            r.raise_for_status()
            return r.content

        data = await loop.run_in_executor(None, fetch)
        f = out / "video.mp4"
        f.write_bytes(data)
        f = await loop.run_in_executor(None, lambda: strip_snap_watermark(f))
        if f.stat().st_size > MAX_UPLOAD_BYTES:
            await update.message.reply_text(
                f"الملف كبير ({size_text(f.stat().st_size)}) حد تليجرام 45 MB.")
            return
        with f.open("rb") as fh:
            await update.message.reply_video(fh, caption=title[:100])
        LAST_EVENT["stage"] = "done"
        LAST_EVENT["error"] = ""
    except Exception as e:
        LAST_EVENT["stage"] = "error"
        LAST_EVENT["error"] = str(e)[:300]
        await update.message.reply_text(friendly_error(e))
    finally:
        for p in out.iterdir():
            p.unlink(missing_ok=True)
        out.rmdir()


async def download_facebook(update, url):
    """Public Facebook videos/reels: scrape the CDN link, no login needed."""
    out = Path(tempfile.mkdtemp(prefix="khazna-fb-"))
    try:
        loop = asyncio.get_running_loop()
        info = await loop.run_in_executor(
            None, lambda: FacebookDownloader().get_fb_video_info(url))
        media = info.get("hd") or info.get("sd")
        if not media:
            await update.message.reply_text(
                "ما لقيت فيديو. لو المنشور خاص أو الرابط يحتاج تسجيل دخول ما أقدر أفتحه.")
            return

        def fetch():
            r = requests.get(media, timeout=60, headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            return r.content

        data = await loop.run_in_executor(None, fetch)
        if len(data) > MAX_UPLOAD_BYTES:
            await update.message.reply_text(
                f"الملف كبير ({size_text(len(data))}) حد تليجرام 45 MB.")
            return
        f = out / "video.mp4"
        f.write_bytes(data)
        with f.open("rb") as fh:
            await update.message.reply_video(
                fh, caption=(info.get("title") or "خزنة")[:100])
        LAST_EVENT["stage"] = "done"
        LAST_EVENT["error"] = ""
    except Exception as e:
        LAST_EVENT["stage"] = "error"
        LAST_EVENT["error"] = str(e)[:300]
        await update.message.reply_text(friendly_error(e))
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
                "ما لقيت صور. ربما المنشور خاص أو الموقع يحتاج تسجيل دخول."
            )
            return
        files = sorted(files, key=lambda p: p.stat().st_size)[:MAX_IMAGES]
        for f in files:
            with f.open("rb") as fh:
                await update.message.reply_photo(fh)
    except subprocess.TimeoutExpired:
        await update.message.reply_text("الرابط بطيء جداً. جرّب مرة ثانية.")
    except Exception as e:
        await update.message.reply_text(f"ما قدرت أحمّل الصور ({str(e)[:120]})")
    finally:
        for p in out.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
        for p in sorted(out.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if p.is_dir():
                p.rmdir()
        out.rmdir()


async def twitter_photos(update, url):
    """Fetch tweet photos from X's public syndication endpoint (no login)."""
    m = re.search(r"/status/(\d+)", url)
    if not m:
        return False
    tid = m.group(1)
    loop = asyncio.get_running_loop()
    def fetch():
        j = requests.get(
            "https://cdn.syndication.twimg.com/tweet-result",
            params={"id": tid, "lang": "en"}, timeout=20,
            headers={"User-Agent": "Mozilla/5.0"},
        ).json()
        urls = []
        for x in j.get("mediaDetails", []):
            u = x.get("media_url_https") or x.get("video", {}).get("poster")
            if u:
                urls.append(u)
        return urls[:MAX_IMAGES]
    try:
        urls = await loop.run_in_executor(None, fetch)
    except Exception:
        return False
    if not urls:
        return False
    for u in urls:
        try:
            data = await loop.run_in_executor(
                None, lambda: requests.get(u, timeout=25).content)
            if data:
                await update.message.reply_photo(data)
        except Exception:
            continue
    return True


async def download_smart(update, url):
    """Try direct photos, then gallery, then video."""
    if host_of(url) in ("twitter.com", "x.com") and await twitter_photos(update, url):
        return
    out = Path(tempfile.mkdtemp(prefix="khazna-smart-"))
    try:
        loop = asyncio.get_running_loop()
        cmd = ["gallery-dl", "--range", f"1-{MAX_IMAGES}", "-d", str(out), url]
        proc = await loop.run_in_executor(None, lambda: subprocess.run(cmd, capture_output=True, timeout=60))
        files = [p for p in out.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT]
        if files:
            for f in sorted(files, key=lambda p: p.stat().st_size)[:MAX_IMAGES]:
                with f.open("rb") as fh:
                    await update.message.reply_photo(fh)
            return
    except Exception:
        pass
    finally:
        for p in out.rglob("*"):
            if p.is_file():
                p.unlink(missing_ok=True)
        for p in sorted(out.rglob("*"), key=lambda x: len(x.parts), reverse=True):
            if p.is_dir():
                p.rmdir()
        out.rmdir()
    await download_video(update, url)


async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE, mode="auto"):
    cid = update.message.chat_id
    LAST_EVENT["text"] = (update.message.text or "")[:200]
    LAST_EVENT["stage"] = "start"
    LAST_EVENT["error"] = ""
    if not allowed(cid):
        await update.message.reply_text("هدّي سرعتك شوي. ٥ طلبات بالدقيقة، رجّع بعد شوي.")
        return
    url = clean_url(update.message.text or "")
    m = URL_RE.search(url)
    if not m:
        await update.message.reply_text("أرسل رابط يبدأ بـ https.")
        return
    u = m.group(0)
    LAST_EVENT["text"] = u[:200]
    if "snapchat.com" in u:
        await download_snap(update, u)
        return
    if "facebook.com" in u or "fb.me" in u or "fb.watch" in u:
        await download_facebook(update, u)
        return
    LAST_EVENT["stage"] = "downloading"
    await update.message.chat.send_action(ChatAction.TYPING)
    if mode == "photo":
        await download_photos(update, u)
    elif mode == "video":
        await download_video(update, u)
    elif "/reel/" in u or "/reels/" in u or "/tv/" in u or any(x in u for x in (".mp4", "?video", "&video")):
        await download_video(update, u)
    elif host_of(u) in ("twitter.com", "x.com", "instagram.com", "pixabay.com", "unsplash.com"):
        await download_smart(update, u)
    else:
        await download_video(update, u)


async def on_text(update, context):
    try:
        await handle(update, context, "auto")
    except Exception as e:
        LAST_EVENT["stage"] = "error"
        LAST_EVENT["error"] = str(e)[:300]
        try:
            await update.message.reply_text(f"صار خطأ ({str(e)[:120]})")
        except Exception:
            pass


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
