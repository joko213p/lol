"""
Telegram Bot — Instagram Profile Photo Downloader
Scrapes photos from Instagram profiles via Apify and sends them
into dedicated Telegram forum topics.
"""

import os
import re
import asyncio
import logging
from datetime import datetime

from telegram import Update, Bot, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from apify_client import ApifyClient

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
APIFY_API_TOKEN = os.environ["APIFY_API_TOKEN"]
GROUP_CHAT_ID = int(os.environ["GROUP_CHAT_ID"])

# Optional: restrict usage to specific user IDs (comma-separated)
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USER_IDS", "")
ALLOWED_USER_IDS = (
    {int(uid.strip()) for uid in ALLOWED_USERS_RAW.split(",") if uid.strip()}
    if ALLOWED_USERS_RAW
    else None
)

# Apify actor
APIFY_ACTOR = "apify/instagram-profile-scraper"
MAX_POSTS = int(os.environ.get("MAX_POSTS", "500"))

# Telegram limits
MEDIA_GROUP_SIZE = 10  # max photos per media group
SEND_DELAY = 1.5       # seconds between batches (rate-limit safety)

# Logging
logging.basicConfig(
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("insta-bot")

# Regex to capture Instagram usernames from various URL formats
INSTAGRAM_REGEX = re.compile(
    r"(?:https?://)?(?:www\.)?instagram\.com/([a-zA-Z0-9_.]{1,30})/?(?:\?.*)?$"
)

# Pages that are not user profiles
NON_PROFILE_SLUGS = {
    "p", "reel", "reels", "stories", "explore", "tv",
    "accounts", "about", "legal", "developer", "directory",
}


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    """Check if a user is allowed (if whitelist is set)."""
    if ALLOWED_USER_IDS is None:
        return True
    return user_id in ALLOWED_USER_IDS


def extract_username(text: str) -> str | None:
    """Extract Instagram username from a message."""
    for word in text.split():
        match = INSTAGRAM_REGEX.match(word.strip())
        if match:
            username = match.group(1)
            if username.lower() not in NON_PROFILE_SLUGS:
                return username
    return None


# ──────────────────────────────────────────────
# Apify scraper
# ──────────────────────────────────────────────
def _scrape_sync(username: str) -> list[str]:
    """
    Synchronous Apify call — runs in a thread via asyncio.to_thread().
    Returns a list of image URLs (photos only, no videos/reels).
    """
    client = ApifyClient(APIFY_API_TOKEN)

    run_input = {
        "usernames": [username],
        "resultsLimit": MAX_POSTS,
        "resultsType": "posts",
    }

    logger.info("Starting Apify run for @%s (limit=%d)…", username, MAX_POSTS)
    run = client.actor(APIFY_ACTOR).call(run_input=run_input)
    logger.info("Apify run finished – dataset %s", run["defaultDatasetId"])

    photos: list[str] = []

    for item in client.dataset(run["defaultDatasetId"]).iterate_items():
        post_type = (item.get("type") or "").lower()

        # ── Skip anything that is a video / reel ──
        if post_type == "video" or item.get("videoUrl"):
            continue

        # ── Carousel / Sidecar → extract each child image ──
        if post_type == "sidecar":
            children = (
                item.get("childPosts")
                or item.get("sidecarImages")
                or item.get("images")
                or []
            )
            for child in children:
                if child.get("videoUrl"):
                    continue  # skip video slides
                url = child.get("displayUrl") or child.get("url") or child.get("src")
                if url:
                    photos.append(url)
            # If no children extracted, fall back to main displayUrl
            if not children:
                url = item.get("displayUrl")
                if url:
                    photos.append(url)
        else:
            # ── Single image post ──
            url = item.get("displayUrl")
            if url:
                photos.append(url)

    logger.info("@%s → %d photos extracted", username, len(photos))
    return photos


async def scrape_instagram_photos(username: str) -> list[str]:
    """Async wrapper around the blocking Apify call."""
    return await asyncio.to_thread(_scrape_sync, username)


# ──────────────────────────────────────────────
# Photo sender with retry logic
# ──────────────────────────────────────────────
async def send_photos_to_topic(
    bot: Bot,
    photos: list[str],
    topic_id: int,
    status_msg,
) -> int:
    """
    Send photos to a forum topic in batches.
    Returns the number of successfully sent photos.
    """
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
            logger.warning("Batch send failed (%s), falling back to one-by-one", exc)
            for url in batch:
                try:
                    await bot.send_photo(
                        chat_id=GROUP_CHAT_ID,
                        photo=url,
                        message_thread_id=topic_id,
                    )
                    sent += 1
                except Exception as exc2:
                    logger.error("Single photo send failed: %s", exc2)
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
# Command handlers
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
        "/info  — Voir la config actuelle\n\n"
        "📸 Envoie simplement un lien instagram.com/username",
        parse_mode="Markdown",
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"ℹ️ *Info*\n\n"
        f"• Groupe cible : `{GROUP_CHAT_ID}`\n"
        f"• Limite de posts : `{MAX_POSTS}`\n"
        f"• Whitelist active : {'Oui' if ALLOWED_USER_IDS else 'Non'}",
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
        return  # Ignore messages that are not Instagram links

    logger.info(
        "User %s requested scrape of @%s",
        update.effective_user.username or user_id,
        username,
    )

    status = await update.message.reply_text(
        f"🔍 Analyse du profil **@{username}** en cours…\n"
        f"⏳ Le scraping peut prendre quelques minutes.",
        parse_mode="Markdown",
    )

    # ── Step 1: Scrape ──
    try:
        photos = await scrape_instagram_photos(username)
    except Exception as exc:
        logger.error("Apify scrape failed: %s", exc)
        await status.edit_text(
            f"❌ Erreur lors du scraping de @{username}.\n"
            f"Détail : `{exc}`",
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
        f"📸 **{len(photos)} photos** trouvées pour @{username} !\n"
        f"🗂 Création du topic…",
        parse_mode="Markdown",
    )

    # ── Step 2: Create forum topic ──
    try:
        topic_name = f"📸 @{username} — {len(photos)} photos"
        # Telegram topic name limit is 128 chars
        if len(topic_name) > 128:
            topic_name = topic_name[:125] + "…"

        forum_topic = await context.bot.create_forum_topic(
            chat_id=GROUP_CHAT_ID,
            name=topic_name,
        )
        topic_id = forum_topic.message_thread_id
        logger.info("Created topic '%s' (id=%d)", topic_name, topic_id)
    except Exception as exc:
        logger.error("Topic creation failed: %s", exc)
        await status.edit_text(
            f"❌ Impossible de créer le topic.\n"
            f"Vérifie que le bot est admin avec 'Gérer les topics'.\n"
            f"Détail : `{exc}`",
            parse_mode="Markdown",
        )
        return

    # ── Step 3: Send first message in topic ──
    try:
        await context.bot.send_message(
            chat_id=GROUP_CHAT_ID,
            message_thread_id=topic_id,
            text=(
                f"📸 **Profil Instagram : @{username}**\n"
                f"🔢 Nombre de photos : {len(photos)}\n"
                f"📅 Scrapé le : {datetime.now().strftime('%d/%m/%Y à %H:%M')}\n"
                f"🔗 https://instagram.com/{username}"
            ),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await status.edit_text(f"⏳ Envoi de {len(photos)} photos dans le topic…")

    # ── Step 4: Send all photos ──
    sent = await send_photos_to_topic(context.bot, photos, topic_id, status)

    # ── Step 5: Final status ──
    if sent == len(photos):
        await status.edit_text(
            f"✅ **Terminé !** {sent} photos envoyées dans le topic @{username}.",
            parse_mode="Markdown",
        )
    elif sent > 0:
        await status.edit_text(
            f"⚠️ {sent}/{len(photos)} photos envoyées (certaines ont échoué).",
            parse_mode="Markdown",
        )
    else:
        await status.edit_text("❌ Échec de l'envoi des photos.")

    logger.info("Done: @%s → %d/%d photos sent", username, sent, len(photos))


# ──────────────────────────────────────────────
# Error handler
# ──────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Unhandled exception: %s", context.error, exc_info=context.error)


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
