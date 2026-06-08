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


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
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


# ──────────────────────────────────────────────
# Apify REST API scraper (no apify-client needed)
# ──────────────────────────────────────────────
def _apify_headers():
    return {"Authorization": f"Bearer {APIFY_API_TOKEN}", "Content-Type": "application/json"}


def _scrape_sync(username: str) -> list[str]:
    """
    Call Apify REST API directly. Runs in a thread.
    Returns a list of photo URLs.
    """
    headers = _apify_headers()

    # 1. Start the actor run
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

    # 2. Poll until the run finishes
    for _ in range(120):  # max ~10 minutes
        time.sleep(5)
        resp = requests.get(
            f"{APIFY_BASE}/actor-runs/{run_id}",
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
        status = resp.json()["data"]["status"]
        if status in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break

    if status != "SUCCEEDED":
        raise RuntimeError(f"Apify run ended with status: {status}")

    dataset_id = resp.json()["data"]["defaultDatasetId"]
    logger.info("Run finished — dataset %s", dataset_id)

    # 3. Fetch dataset items
    items_resp = requests.get(
        f"{APIFY_BASE}/datasets/{dataset_id}/items",
        headers=headers,
        params={"format": "json"},
        timeout=30,
    )
    items_resp.raise_for_status()
    items = items_resp.json()

    # 4. Extract photo URLs only
    photos: list[str] = []

    for item in items:
        post_type = (item.get("type") or "").lower()

        # Skip videos / reels
        if post_type == "video" or item.get("videoUrl"):
            continue

        # Carousel / Sidecar
        if post_type == "sidecar":
            children = (
                item.get("childPosts")
                or item.get("sidecarImages")
                or item.get("images")
                or []
            )
            for child in children:
                if child.get("videoUrl"):
                    continue
                url = child.get("displayUrl") or child.get("url") or child.get("src")
                if url:
                    photos.append(url)
            if not children:
                url = item.get("displayUrl")
                if url:
                    photos.append(url)
        else:
            url = item.get("displayUrl")
            if url:
                photos.append(url)

    logger.info("@%s → %d photos extracted", username, len(photos))
    return photos


async def scrape_instagram_photos(username: str) -> list[str]:
    return await asyncio.to_thread(_scrape_sync, username)


# ──────────────────────────────────────────────
# Photo sender with retry
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
                try:
                    await bot.send_photo(
                        chat_id=GROUP_CHAT_ID,
                        photo=url,
                        message_thread_id=topic_id,
                    )
                    sent += 1
                except Exception as exc2:
                    logger.error("Single photo failed: %s", exc2)
                await asyncio.sleep(0.5)

        # Progress update every 3 batches
        if (i // MEDIA_GROUP_SIZE) % 3 == 0 and i > 0:
            try:
                await status_msg.edit_text(
                    f"⏳ Envoi en cours… {sent}/{total} photos"
                )
            except Exception:
                pass

        await asyncio.sleep(SEND_DELAY)

    return sent


# ──────────────────────────────────────────────
# Commands
# ──────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Instagram → Telegram Photo Bot*\n\n"
        "Envoie-moi un lien de profil Instagram et je :\n"
        "1. Récupère toutes les photos du profil\n"
        "2. Crée un topic dédié dans le groupe\n"
        "3. Envoie chaque photo dans ce topic\n\n"
        "Exemple : `https://instagram.com/username`",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔧 *Commandes*\n\n"
        "/start — Message de bienvenue\n"
        "/help  — Ce message\n"
        "/info  — Config actuelle\n\n"
        "📸 Envoie simplement un lien instagram.com/username",
        parse_mode="Markdown",
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ℹ️ *Info*\n\n"
        f"• Groupe cible : `{GROUP_CHAT_ID}`\n"
        f"• Limite de posts : `{MAX_POSTS}`\n"
        f"• Whitelist : {'Oui' if ALLOWED_USER_IDS else 'Non'}",
        parse_mode="Markdown",
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

    logger.info("User %s requested @%s", update.effective_user.username or user_id, username)

    status = await update.message.reply_text(
        f"🔍 Analyse du profil **@{username}** en cours…\n"
        f"⏳ Le scraping peut prendre quelques minutes.",
        parse_mode="Markdown",
    )

    # Step 1: Scrape
    try:
        photos = await scrape_instagram_photos(username)
    except Exception as exc:
        logger.error("Scrape failed: %s", exc)
        await status.edit_text(
            f"❌ Erreur lors du scraping de @{username}.\nDétail : `{exc}`",
            parse_mode="Markdown",
        )
        return

    if not photos:
        await status.edit_text(
            f"😕 Aucune photo trouvée pour **@{username}**.\n"
            f"Le profil est peut-être privé ou vide.",
            parse_mode="Markdown",
        )
        return

    await status.edit_text(
        f"📸 **{len(photos)} photos** trouvées !\n🗂 Création du topic…",
        parse_mode="Markdown",
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
            f"Détail : `{exc}`",
            parse_mode="Markdown",
        )
        return

    # Step 3: Info message in topic
    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=topic_id,
            text=(
                f"📸 **Profil : @{username}**\n"
                f"🔢 Photos : {len(photos)}\n"
                f"📅 {datetime.now().strftime('%d/%m/%Y à %H:%M')}\n"
                f"🔗 https://instagram.com/{username}"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await status.edit_text(f"⏳ Envoi de {len(photos)} photos…")

    # Step 4: Send photos
    sent = await send_photos_to_topic(context.bot, photos, topic_id, status)

    # Step 5: Done
    if sent == len(photos):
        await status.edit_text(
            f"✅ **Terminé !** {sent} photos dans le topic @{username}.",
            parse_mode="Markdown",
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
