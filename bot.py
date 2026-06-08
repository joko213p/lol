"""
Telegram Bot — Instagram Profile Photo Downloader
Scrapes photos from Instagram profiles via Apify REST API
and sends them into dedicated Telegram forum topics.
"""

import os
import re
import time
import asyncio
import logging
import json
from datetime import datetime

import requests
from telegram import Update, Bot, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
APIFY_API_TOKEN = os.environ["APIFY_API_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])

ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = (
    {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}
    if ALLOWED_USERS_RAW
    else None
)

APIFY_ACTOR_ID = "apify~instagram-profile-scraper"
APIFY_BASE = "https://api.apify.com/v2"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "500"))

MEDIA_GROUP_SIZE = 10
SEND_DELAY = 1.5

logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("insta-bot")

INSTAGRAM_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9_.]{1,30})/?(?:\?.*)?$"
)

NON_PROFILE_SLUGS = {
    "p", "reel", "reels", "stories", "explore", "tv",
    "accounts", "about", "legal", "developer", "directory",
}

# Image URL patterns — must match actual CDN image URLs, not profile pages
IMAGE_URL_PATTERNS = (
    "cdninstagram.com",
    "fbcdn.net",
    "scontent",
    ".jpg",
    ".jpeg",
    ".png",
    ".webp",
)


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def is_allowed(user_id: int) -> bool:
    if ALLOWED_USER_IDS is None:
        return True
    return user_id in ALLOWED_USER_IDS


def extract_username(text: str) -> str | None:
    for word in text.split():
        match = INSTAGRAM_REGEX.match(word.strip())
        if match:
            username = match.group(1)
            if username.lower() not in NON_PROFILE_SLUGS:
                return username
    return None


def is_image_url(url: str) -> bool:
    """Check if a URL is an actual image CDN URL, not a profile/page link."""
    if not url or not isinstance(url, str):
        return False
    lower = url.lower()
    # Reject obvious non-image URLs
    if "instagram.com/" in lower and "cdninstagram" not in lower:
        return False
    return any(pattern in lower for pattern in IMAGE_URL_PATTERNS)


# ──────────────────────────────────────────────
# Extract photo URLs from a single Apify item
# ──────────────────────────────────────────────
def extract_photos_from_item(item: dict) -> list[str]:
    photos = []

    # Skip error items from Apify
    if item.get("error") or item.get("requestErrorMessages"):
        logger.info("Skipping error item: %s", item.get("error") or item.get("errorDescription"))
        return []

    post_type = str(item.get("type", "")).lower()
    has_video = bool(
        item.get("videoUrl")
        or item.get("video_url")
        or item.get("videoVersions")
        or item.get("video_versions")
        or post_type in ("video", "reel", "clips")
    )

    media_type = item.get("media_type") or item.get("mediaType")
    if media_type == 2:
        return []

    if has_video and post_type in ("video", "reel", "clips"):
        return []

    is_carousel = (
        post_type in ("sidecar", "carousel")
        or media_type == 8
        or item.get("carousel_media")
        or item.get("carouselMedia")
        or item.get("childPosts")
        or item.get("sidecarImages")
        or item.get("edge_sidecar_to_children")
    )

    if is_carousel:
        children = (
            item.get("carousel_media")
            or item.get("carouselMedia")
            or item.get("childPosts")
            or item.get("sidecarImages")
            or item.get("images")
            or []
        )

        edges = item.get("edge_sidecar_to_children", {})
        if isinstance(edges, dict) and edges.get("edges"):
            children = [edge.get("node", {}) for edge in edges["edges"]]

        for child in children:
            child_type = child.get("media_type") or child.get("mediaType")
            if child_type == 2 or child.get("videoUrl") or child.get("video_url"):
                continue
            url = _best_image_url(child)
            if url:
                photos.append(url)

        if photos:
            return photos

    if not has_video:
        url = _best_image_url(item)
        if url:
            photos.append(url)

    return photos


def _best_image_url(item: dict) -> str | None:
    """Try multiple known field names to find a real image CDN URL."""

    for key in ("displayUrl", "display_url", "imageUrl", "image_url"):
        val = item.get(key)
        if val and is_image_url(val):
            return val

    # image_versions2 → candidates (Instagram private API format)
    versions = item.get("image_versions2") or item.get("imageVersions2")
    if isinstance(versions, dict):
        candidates = versions.get("candidates", [])
        if candidates:
            best = max(candidates, key=lambda c: c.get("width", 0) * c.get("height", 0))
            url = best.get("url")
            if url and is_image_url(url):
                return url

    # display_resources / thumbnail_resources (GraphQL format)
    for key in ("display_resources", "thumbnail_resources"):
        resources = item.get(key)
        if isinstance(resources, list) and resources:
            best = max(resources, key=lambda r: r.get("config_width", 0) * r.get("config_height", 0))
            url = best.get("src")
            if url and is_image_url(url):
                return url

    # thumbnail_src fallback
    val = item.get("thumbnail_src")
    if val and is_image_url(val):
        return val

    # Last resort: "url" or "src" but ONLY if it looks like an image
    for key in ("url", "src"):
        val = item.get(key)
        if val and is_image_url(val):
            return val

    return None


# ──────────────────────────────────────────────
# Apify REST API scraper
# ──────────────────────────────────────────────
def _apify_headers():
    return {"Authorization": f"Bearer {APIFY_API_TOKEN}", "Content-Type": "application/json"}


def _scrape_sync(username: str) -> tuple[list[str], str | None]:
    """
    Returns (photos, error_message).
    If error_message is set, the profile could not be scraped.
    """
    headers = _apify_headers()

    run_input = {
        "usernames": [username],
        "resultsLimit": MAX_POSTS,
        "resultsType": "posts",
    }

    logger.info("Starting Apify run for @%s…", username)
    resp = requests.post(
        f"{APIFY_BASE}/acts/{APIFY_ACTOR_ID}/runs",
        headers=headers,
        json=run_input,
        timeout=30,
    )
    resp.raise_for_status()
    run_data = resp.json()["data"]
    run_id = run_data["id"]
    logger.info("Apify run started: %s", run_id)

    for _ in range(120):
        time.sleep(5)
        resp = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        status = resp.json()["data"]["status"]
        logger.info("Apify run status: %s", status)
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        return [], f"Apify run ended with status: {status}"

    dataset_id = resp.json()["data"]["defaultDatasetId"]
    logger.info("Run finished — dataset %s", dataset_id)

    items_resp = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        headers=headers,
        params={"format": "json"},
        timeout=60,
    )
    items_resp.raise_for_status()
    items = items_resp.json()

    logger.info("Dataset contains %d items", len(items))

    # Check if Apify returned an error (private/empty profile)
    if items:
        first = items[0]
        logger.info("FIRST ITEM KEYS: %s", list(first.keys()))

        if first.get("error"):
            err = first.get("errorDescription") or first.get("error")
            logger.warning("Apify error for @%s: %s", username, err)
            return [], err

        debug_item = {k: (str(v)[:100] if isinstance(v, str) else type(v).__name__)
                      for k, v in first.items()}
        logger.info("FIRST ITEM PREVIEW: %s", json.dumps(debug_item, indent=2))

    photos: list[str] = []
    skipped_videos = 0

    for item in items:
        item_photos = extract_photos_from_item(item)
        if item_photos:
            photos.extend(item_photos)
        elif item.get("videoUrl") or item.get("video_url") or item.get("media_type") == 2:
            skipped_videos += 1

    logger.info("@%s → %d photos extracted, %d videos skipped", username, len(photos), skipped_videos)
    return photos, None


async def scrape_instagram_photos(username: str) -> tuple[list[str], str | None]:
    return await asyncio.to_thread(_scrape_sync, username)


# ──────────────────────────────────────────────
# Photo sender with retry and flood control
# ──────────────────────────────────────────────
async def send_photos_to_topic(
    bot: Bot,
    photos: list[str],
    topic_id: int,
    status_msg,
) -> int:
    sent = 0
    total = len(photos)

    for i in range(0, total, MEDIA_GROUP_SIZE):
        batch = photos[i : i + MEDIA_GROUP_SIZE]
        media_group = [InputMediaPhoto(media=url) for url in batch]

        try:
            await bot.send_media_group(
                chat_id=GROUP_CHAT_ID,
                media=media_group,
                message_thread_id=topic_id,
            )
            sent += len(batch)
        except Exception as exc:
            logger.warning("Batch failed (%s), sending one-by-one", exc)
            for url in batch:
                for attempt in range(3):
                    try:
                        await bot.send_photo(
                            chat_id=GROUP_CHAT_ID,
                            photo=url,
                            message_thread_id=topic_id,
                        )
                        sent += 1
                        break
                    except Exception as exc2:
                        err_str = str(exc2).lower()
                        if "flood" in err_str or "retry" in err_str:
                            wait = 3 * (attempt + 1)
                            logger.warning("Flood control, waiting %ds…", wait)
                            await asyncio.sleep(wait)
                        else:
                            logger.error("Photo failed: %s", exc2)
                            break
                await asyncio.sleep(1)

        # Progress update every 3 batches
        if (i // MEDIA_GROUP_SIZE) % 3 == 0 and i > 0:
            try:
                await status_msg.edit_text(f"⏳ Envoi en cours… {sent}/{total} photos")
            except Exception:
                pass

        await asyncio.sleep(SEND_DELAY)

    return sent


# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 <b>Instagram → Telegram Photo Bot</b>\n\n"
        "Envoie-moi un lien de profil Instagram et je :\n"
        "1. Récupère toutes les photos du profil\n"
        "2. Crée un topic dédié dans le groupe\n"
        "3. Envoie chaque photo dans ce topic\n\n"
        "⚠️ Le profil doit être <b>public</b>.\n\n"
        "Exemple : <code>https://instagram.com/username</code>",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 <b>Commandes</b>\n\n"
        "/start — Message de bienvenue\n"
        "/help  — Ce message\n"
        "/info  — Config actuelle\n\n"
        "📸 Envoie simplement un lien instagram.com/username",
        parse_mode="HTML",
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ℹ️ <b>Info</b>\n\n"
        f"• Groupe cible : <code>{GROUP_CHAT_ID}</code>\n"
        f"• Limite de posts : <code>{MAX_POSTS}</code>\n"
        f"• Whitelist : {'Oui' if ALLOWED_USER_IDS else 'Non'}",
        parse_mode="HTML",
    )


# ──────────────────────────────────────────────
# Main message handler
# ──────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    if not is_allowed(user_id):
        await update.message.reply_text("🚫 Tu n'es pas autorisé à utiliser ce bot.")
        return

    username = extract_username(update.message.text)
    if not username:
        return

    safe = esc(username)

    logger.info("User %s requested @%s", update.effective_user.username or user_id, username)

    status = await update.message.reply_text(
        f"🔍 Analyse du profil <b>@{safe}</b> en cours…\n"
        f"⏳ Le scraping peut prendre quelques minutes.",
        parse_mode="HTML",
    )

    # Step 1: Scrape
    try:
        photos, apify_error = await scrape_instagram_photos(username)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        await status.edit_text(
            f"❌ Erreur lors du scraping de @{safe}.\nDétail : <code>{esc(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    if apify_error:
        await status.edit_text(
            f"❌ Impossible de scraper <b>@{safe}</b>.\n\n"
            f"Raison : <code>{esc(apify_error)}</code>\n\n"
            f"💡 Le profil est probablement <b>privé</b>. "
            f"Seuls les profils publics peuvent être scrapés.",
            parse_mode="HTML",
        )
        return

    if not photos:
        await status.edit_text(
            f"😕 Aucune photo trouvée pour <b>@{safe}</b>.\n"
            f"Le profil n'a peut-être que des vidéos/reels.",
            parse_mode="HTML",
        )
        return

    await status.edit_text(
        f"📸 <b>{len(photos)} photos</b> trouvées !\n🗂 Création du topic…",
        parse_mode="HTML",
    )

    # Step 2: Create forum topic
    try:
        topic_name = f"📸 @{username} — {len(photos)} photos"
        if len(topic_name) > 128:
            topic_name = topic_name[:125] + "…"

        forum_topic = await context.bot.create_forum_topic(
            chat_id=GROUP_CHAT_ID,
            name=topic_name,
        )
        topic_id = forum_topic.message_thread_id
    except Exception as exc:
        logger.error("Topic creation failed: %s", exc)
        await status.edit_text(
            f"❌ Impossible de créer le topic.\n"
            f"Vérifie que le bot est admin avec 'Gérer les topics'.\n"
            f"Détail : <code>{esc(str(exc))}</code>",
            parse_mode="HTML",
        )
        return

    # Step 3: Info message in topic
    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=topic_id,
            text=(
                f"📸 <b>Profil : @{safe}</b>\n"
                f"🔢 Photos : {len(photos)}\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y à %H:%M')}\n"
                f"🔗 https://instagram.com/{username}"
            ),
            parse_mode="HTML",
        )
    except Exception:
        pass

    await status.edit_text(f"⏳ Envoi de {len(photos)} photos…")

    # Step 4: Send photos
    sent = await send_photos_to_topic(context.bot, photos, topic_id, status)

    # Step 5: Done
    if sent == len(photos):
        await status.edit_text(
            f"✅ <b>Terminé !</b> {sent} photos dans le topic @{safe}.",
            parse_mode="HTML",
        )
    elif sent > 0:
        await status.edit_text(f"⚠️ {sent}/{len(photos)} photos envoyées.")
    else:
        await status.edit_text("❌ Échec de l'envoi des photos.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled: %s", context.error, exc_info=context.error)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("info", cmd_info))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_error_handler(error_handler)
    logger.info("🤖 Bot started — polling…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
