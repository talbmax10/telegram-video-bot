#!/usr/bin/env python3
"""Telegram video downloader bot (TikTok / Instagram / YouTube).

Usage:
    TELEGRAM_BOT_TOKEN=... venv/bin/python bot.py

Notes:
- Uses yt-dlp for downloading. No AI, no paid API.
- Telegram Bot API limit: a bot can send files up to 50 MB.
  The bot therefore tries 720p first, then 480p / 360p if the file
  is too big, and tells the user clearly if it is still too big.
- Only download videos you own or that are public and allowed
  to be downloaded. Private / login-only videos will fail and
  the bot says so instead of crashing.
"""
import asyncio
import logging
import os
import re
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from telegram.request import HTTPXRequest

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
# httpx logs every request URL, which contains the bot token in the path.
# Keep those libraries quiet so the token never lands in a log file.
for _noisy in ("httpx", "httpcore", "telegram"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
log = logging.getLogger("video-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
MAX_BYTES = 48 * 1024 * 1024  # stay safely under Telegram's 50 MB bot limit

URL_RE = re.compile(r"https?://\S+")

START_TEXT = (
    "أهلاً 👋\n"
    "دز لي رابط فيديو من تيك توك أو إنستغرام أو يوتيوب، وأنا أحمّله وأدزّه لك هنا.\n\n"
    "مثال:\n"
    "https://www.tiktok.com/@user/video/123...\n"
    "https://www.instagram.com/reel/...\n"
    "https://www.youtube.com/watch?v=...\n\n"
    "ملاحظات مهمة:\n"
    "• الفيديو لازم يكون عام، مو خاص أو يحتاج تسجيل دخول.\n"
    "• تليجرام ما يقبل ملف أكبر من 50 ميغا، فالفيديوهات الطويلة بجودة عالية ممكن ما تنرسل.\n"
    "• حمّل فيديوهاتك الخاصة أو الفيديوهات المسموح تحميلها فقط."
)

HELP_TEXT = (
    "طريقة الاستخدام:\n"
    "1. انسخ رابط الفيديو من تيك توك / إنستا / يوتيوب.\n"
    "2. الصقه هنا بالمحادثة.\n"
    "3. انتظر شوي، البوت يحمّل ويدز الفيديو.\n\n"
    "إذا طلع خطأ «يحتاج تسجيل دخول» فهذا يعني الفيديو خاص أو المنصة طالبة حساب، "
    "والبوت ما يكدر يتخطى هذا الشي."
)


def platform_of(url: str) -> str | None:
    u = url.lower()
    if "tiktok.com" in u:
        return "تيك توك"
    if "instagram.com" in u:
        return "إنستغرام"
    if "youtube.com" in u or "youtu.be" in u:
        return "يوتيوب"
    return None


def download_video(url: str, workdir: Path) -> Path:
    """Download with yt-dlp, trying lower qualities if the file is too big.
    Returns the downloaded file path. Raises RuntimeError with a
    user-friendly Arabic message on failure.
    """
    import time

    import yt_dlp

    is_tiktok = "tiktok.com" in url.lower()
    is_insta = "instagram.com" in url.lower()
    if is_tiktok or is_insta:
        # TikTok/Instagram videos are single-file formats (e.g. TikTok's
        # bytevc1_720p). The bestvideo+bestaudio selectors never match
        # them, so they only wasted two extractions per video and made
        # TikTok throttle the shared IP faster. One selector is enough.
        formats = ["best[height<=720]/best"]
    else:
        formats = [
            # 720p mp4 first, then progressively lower
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
            "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
            "best[height<=360]/best",
        ]
    last_error = ""
    # TikTok throttles the shared egress IP after many downloads in a
    # row: "Unexpected response from webpage request". It is transient
    # (the same video downloaded fine minutes later, 2026-11-06).
    # Hammering it with three instant retries only makes it worse, so
    # wait 25 s and try once more before giving up.
    for attempt in range(2):
        for fmt in formats:
            ydl_opts = {
                "format": fmt,
                "outtmpl": str(workdir / "%(title).80s.%(ext)s"),
                "merge_output_format": "mp4",
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "max_filesize": MAX_BYTES,
                "retries": 2,
                "socket_timeout": 30,
                # Verified on this VM 2026-11-06:
                # - yt-dlp loads certifi only by default, so the VM egress
                #   proxy CA is rejected (SSL CERTIFICATE_VERIFY_FAILED).
                #   compat_opts no-certifi makes it use the system CA bundle.
                # - YouTube web client gets HTTP 429 + "Sign in to confirm
                #   you're not a bot" from the shared egress IP; the
                #   android player client works (tested: Big Buck Bunny
                #   360p, 27 MB, downloaded in ~25 s).
                "compat_opts": ["no-certifi"],
                "extractor_args": {
                    "youtube": {"player_client": ["android"]},
                    "tiktok": {"api_hostname": ["api16-normal-c-useast1a.tiktokv.com"]},
                },
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as e:  # noqa: BLE001 - surface as a friendly message
                last_error = str(e)
                log.warning("yt-dlp failed for format %s: %s", fmt, e)
                continue
            files = sorted(workdir.glob("*"), key=lambda p: p.stat().st_size, reverse=True)
            files = [f for f in files if f.is_file() and f.stat().st_size > 0]
            if not files:
                continue
            biggest = files[0]
            if biggest.stat().st_size <= MAX_BYTES:
                return biggest
            # Too big even at this quality: delete and try a lower one.
            log.info("file %s is %d bytes, too big, trying lower quality", biggest, biggest.stat().st_size)
            for f in files:
                f.unlink(missing_ok=True)
        transient = any(
            s in last_error
            for s in ("Unexpected response", "timed out", "429", "Too Many Requests")
        )
        if transient and attempt == 0:
            log.info("transient TikTok/platform error, waiting 25 s before one retry")
            time.sleep(25)
            continue
        break
    if "login" in last_error.lower() or "cookies" in last_error.lower() or "private" in last_error.lower():
        raise RuntimeError("هذا الفيديو خاص أو يحتاج تسجيل دخول، والبوت ما يكدر يحمّله.")
    if "Unexpected response" in last_error or "timed out" in last_error:
        raise RuntimeError(
            "تيك توك رفض الطلب مؤقتاً هسه (ضغط على السيرفر / حماية ضد البوتات). "
            "نفس الفيديو نزل قبل شوي بدون مشكلة، فجرّب نفس الرابط بعد دقيقة."
        )
    if "sign in" in last_error.lower() or "bot" in last_error.lower():
        raise RuntimeError("المنصة رفضت التحميل هسه (حماية ضد البوتات). جرّب رابط ثاني أو جرّب بعد شوي.")
    raise RuntimeError(f"ما كدرت أحمّل هذا الفيديو. السبب: {last_error[:200]}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(START_TEXT)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text or ""
    m = URL_RE.search(text)
    if not m:
        await update.message.reply_text("دز رابط فيديو صحيح من تيك توك أو إنستا أو يوتيوب.")
        return
    url = m.group(0).rstrip(").,")
    platform = platform_of(url)
    if not platform:
        await update.message.reply_text(
            "هذا الرابط مو من تيك توك أو إنستا أو يوتيوب.\n"
            "البوت يدعم هالثلاث منصات بس."
        )
        return

    status = await update.message.reply_text(f"جاري تحميل فيديو {platform}... ⏳")
    try:
        with tempfile.TemporaryDirectory(prefix="tg-video-") as tmp:
            workdir = Path(tmp)
            path = await asyncio.to_thread(download_video, url, workdir)
            await status.edit_text("تم التحميل، جاري الإرسال... 📤")
            with open(path, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=f"من {platform}",
                    supports_streaming=True,
                )
            await status.delete()
    except RuntimeError as e:
        await status.edit_text(f"❌ {e}")
    except Exception as e:  # noqa: BLE001
        log.exception("send failed")
        await status.edit_text(f"❌ صار خطأ أثناء الإرسال: {e}")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    log.exception("bot error while handling update: %s", context.error)


def start_health_server():
    """Render Web Services must bind $PORT. A Background Worker does not
    need this, but the endpoint is harmless there: it only starts when
    the PORT env var is set (Render sets it automatically)."""
    port = int(os.environ.get("PORT", "0") or 0)
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *args):  # noqa: D102
            pass

    threading.Thread(
        target=ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever,
        daemon=True,
    ).start()
    log.info("health server listening on port %d", port)


def main():
    if not TOKEN:
        raise SystemExit("TELEGRAM_BOT_TOKEN is not set. Export it first, never hard-code it.")
    # httpx breaks if NO_PROXY contains bracketed IPv6 entries such as
    # [::1] (seen on the Muse VM). Keep only plain localhost entries.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = "localhost,127.0.0.1"
    start_health_server()
    # Telegram is reached through the VM egress proxy, which is sometimes
    # slow: the default 5 s connect timeout caused TimedOut errors on
    # 2026-11-06 (a reply never arrived, so the download never started).
    # Give the proxy much more room instead of failing fast.
    request = HTTPXRequest(
        connection_pool_size=8,
        connect_timeout=30.0,
        read_timeout=120.0,
        write_timeout=60.0,
        pool_timeout=30.0,
    )
    app = Application.builder().token(TOKEN).request(request).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("video bot starting (polling)")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
