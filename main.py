import logging
import tweepy
import json
import asyncio
import os
from datetime import datetime, timedelta
import pytz
import subprocess
from openai import OpenAI
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, InputMediaVideo
from telegram.ext import (
    ApplicationBuilder,
    MessageHandler,
    filters,
    CallbackQueryHandler,
    ContextTypes,
    CommandHandler
)
from tweepy import OAuth1UserHandler, API

# ==== ЗАГРУЗКА КОНФИГА ====
with open('config.json', 'r') as f:
    config = json.load(f)

TELEGRAM_BOT_TOKEN = config['TELEGRAM_BOT_TOKEN']
OPENAI_API_KEY = config['OPENAI_API_KEY']
TWITTER_API_KEY = config['TWITTER_API_KEY']
TWITTER_API_SECRET = config['TWITTER_API_SECRET']
ACCESS_TOKEN = config['ACCESS_TOKEN']
ACCESS_TOKEN_SECRET = config['ACCESS_TOKEN_SECRET']
BEARER_TOKEN = config['BEARER_TOKEN']
ALLOWED_USER_ID = int(config['ALLOWED_USER_ID'])

# ==== НАСТРОЙКА ====
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

client = OpenAI(api_key=OPENAI_API_KEY)
twitter_client = tweepy.Client(
    consumer_key=TWITTER_API_KEY,
    consumer_secret=TWITTER_API_SECRET,
    access_token=ACCESS_TOKEN,
    access_token_secret=ACCESS_TOKEN_SECRET,
    bearer_token=BEARER_TOKEN
)
# Tweepy v1.1 API for media upload
auth_v1 = OAuth1UserHandler(
    TWITTER_API_KEY,
    TWITTER_API_SECRET,
    ACCESS_TOKEN,
    ACCESS_TOKEN_SECRET
)
twitter_api = API(auth_v1)

MSK = pytz.timezone('Europe/Moscow')
PROMPT = (
    "Ты — мой личный новостной комментатор в формате коротких твитов.\n"
    "Пиши ёмкие, живые и эмоциональные комментарии на новости так, как будто это я — простой, умный и неравнодушный гражданин России.\n\n"
    "Каждый комментарий должен быть:\n"
    "- 1–2 предложения, без воды и формализма\n"
    "- с иронией, сарказмом или, если нужно, жёсткой прямотой\n"
    "- искренним, вызывающим реакцию — улыбку, согласие или удивление\n"
    "- длиной не более 280 символов (лимит твита)\n"
    "- с уместными хештегами по теме\n\n"
    "Ты смотришь на новости глазами человека, который живёт в России, с его страхами, надеждами и эмоциями.\n"
    "Не используй штампы и клише вроде “мы все должны” или “общество обязано”.\n"
    "Твоя задача — настоящая реакция, живой и честный голос."
)

# ==== ХРАНЕНИЕ СОСТОЯНИЙ ====
pending_tweets = {}
original_messages = {}
user_states = {}
scheduled_queue = {}
pending_media = {}


# ==== ЗАГРУЗКА МЕДИА ИЗ TELEGRAM ====
async def download_telegram_file(file_obj, context, user_id):
    # Получим MIME из самого объекта, а не из telegram.File
    mime = ""
    if hasattr(file_obj, 'mime_type'):
        mime = file_obj.mime_type or ""

    if mime.startswith("video"):
        file_path = f"media_{user_id}_{file_obj.file_unique_id}.mp4"
    elif mime.startswith("image"):
        file_path = f"media_{user_id}_{file_obj.file_unique_id}.jpg"
    else:
        file_path = f"media_{user_id}_{file_obj.file_unique_id}"

    full_path = f"/tmp/{file_path}"
    await file_obj.download_to_drive(full_path)
    pending_media[user_id] = full_path
    return full_path

def convert_to_mp4(input_path):
    output_path = input_path.rsplit('.', 1)[0] + "_converted.mp4"
    try:
        subprocess.run([
            "ffmpeg", "-y", "-i", input_path,
            "-c:v", "libx264", "-preset", "fast",
            "-c:a", "aac", output_path
        ], check=True)
        return output_path
    except subprocess.CalledProcessError:
        return None

# ==== ОБРАБОТЧИК СООБЩЕНИЙ (обновлён с логами и обработкой пересланного) ====
async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    message = update.message
    if user_id != ALLOWED_USER_ID or not message:
        return

    logger.info(f"📩 Получено сообщение от {user_id}: forwarded={message.forward_origin is not None}, caption={message.caption}, text={message.text}")

    text = message.caption if message.caption else message.text
    media_saved = False
    state = user_states.get(user_id)

    # === Обработка редактирования через GPT ===
    if isinstance(state, dict) and state.get('mode') == 'editing':
        comment = text
        draft = state.get('draft')
        try:
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {"role": "system", "content": PROMPT},
                    {"role": "user", "content": f"Вот твит: {draft}\n\nПожалуйста, доработай его по замечанию: {comment}"}
                ]
            )
            updated = response.choices[0].message.content.strip()
            pending_tweets[user_id] = updated
            user_states[user_id] = None
            keyboard = [[
                InlineKeyboardButton("✅ Публиковать", callback_data="publish"),
                InlineKeyboardButton("❌ Отменить", callback_data="cancel")
            ]]
            await message.reply_text(f"Обновлённый твит:\n\n{updated}", reply_markup=InlineKeyboardMarkup(keyboard))
        except Exception as e:
            logger.error(f"GPT ошибка при редактировании: {e}")
            await message.reply_text("⚠️ Ошибка при доработке твита.")
        return

    # === Обработка медиа: фото, видео, документ ===
    if message.photo:
        await download_telegram_file(await message.photo[-1].get_file(), context, user_id)
        media_saved = True
    elif message.video:
        await download_telegram_file(await message.video.get_file(), context, user_id)
        media_saved = True
    elif message.document and message.document.mime_type.startswith("video"):
        await download_telegram_file(await message.document.get_file(), context, user_id)
        media_saved = True

    # === Если пришла медиа без текста ===
    if media_saved and not text:
        user_states[user_id] = "awaiting_caption"
        await message.reply_text("📎 Получил медиа. Пришли подпись (текст), чтобы сделать твит.")
        return

    # === Получили подпись после медиа ===
    if state == "awaiting_caption":
        user_states[user_id] = None

    # === Если нет текста — игнор ===
    if not text:
        await message.reply_text("❗️ Не вижу текста для твита. Отправь текст.")
        return

    # === Генерация твита ===
    try:
        response = client.chat.completions.create(
            model="gpt-4",
            messages=[
                {"role": "system", "content": PROMPT},
                {"role": "user", "content": text}
            ]
        )
        tweet_text = response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"GPT ошибка: {e}")
        await message.reply_text("⚠️ Ошибка при генерации твита.")
        return

    pending_tweets[user_id] = tweet_text
    original_messages[user_id] = text
    keyboard = [[
        InlineKeyboardButton("✅ Публиковать", callback_data="publish"),
        InlineKeyboardButton("✏️ Редактировать", callback_data="edit"),
        InlineKeyboardButton("❌ Отменить", callback_data="cancel")
    ]]
    await message.reply_text(f"Вот адаптированный твит:\n\n{tweet_text}", reply_markup=InlineKeyboardMarkup(keyboard))

# ==== КНОПКИ ====
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id
    await query.answer()

    if user_id != ALLOWED_USER_ID:
        return

    if query.data == "publish":
        keyboard = [[
            InlineKeyboardButton("Сейчас", callback_data="post_now"),
            InlineKeyboardButton("15 минут", callback_data="delay_15")
        ], [
            InlineKeyboardButton("30 минут", callback_data="delay_30"),
            InlineKeyboardButton("1 час", callback_data="delay_60")
        ]]
        await query.edit_message_text("⏱ Когда опубликовать твит?", reply_markup=InlineKeyboardMarkup(keyboard))

    elif query.data == "post_now":
        await post_now(user_id, context)
        await query.edit_message_text("✅ Твит опубликован немедленно!")

    elif query.data.startswith("delay_"):
        minutes = int(query.data.split("_")[1])
        post_time_utc = datetime.utcnow().replace(tzinfo=pytz.utc) + timedelta(minutes=minutes)
        await schedule_tweet(user_id, post_time_utc, context)
        msk_time = post_time_utc.astimezone(MSK).strftime('%H:%M:%S')
        await query.edit_message_text(f"✅ Твит будет опубликован в {msk_time} МСК")

    elif query.data == "custom_time":
        user_states[user_id] = 'awaiting_time'
        await query.edit_message_text("🕒 Введите время в формате HH:MM (МСК)")

    elif query.data == "edit":
        tweet = pending_tweets.get(user_id, '')
        user_states[user_id] = {"mode": "editing", "draft": tweet}
        await query.edit_message_text(f"✏️ Напиши, что изменить. Текущий твит:\n\n{tweet}")

    elif query.data == "cancel":
        pending_tweets.pop(user_id, None)
        original_messages.pop(user_id, None)
        user_states.pop(user_id, None)
        await query.edit_message_text("❌ Отменено.")

    elif query.data.startswith("cancel_post_"):
        idx = int(query.data.split("_")[-1])
        if user_id in scheduled_queue and idx < len(scheduled_queue[user_id]):
            scheduled_queue[user_id].pop(idx)
            await query.edit_message_text("❌ Твит удалён из очереди.")
        else:
            await query.edit_message_text("⚠️ Не удалось найти твит.")

# ==== ПУБЛИКАЦИЯ СРАЗУ ====
async def post_now(user_id, context):
    tweet = pending_tweets.get(user_id)
    media_ids = []
    media_path = pending_media.get(user_id)

    # Загрузка медиа через tweepy.API
    if media_path and os.path.exists(media_path):
        try:
            if not media_path.endswith(".mp4"):
                converted = convert_to_mp4(media_path)
                if not converted or not os.path.exists(converted):
                    await context.bot.send_message(chat_id=user_id, text="❌ Ошибка при конвертации видео в .mp4")
                    return
                media_path = converted  # заменяем путь на сконвертированный

            media = twitter_api.media_upload(filename=media_path, media_category='tweet_video')
            media_ids = [media.media_id]
        except Exception as e:
            await context.bot.send_message(chat_id=user_id, text=f"⚠️ Не удалось прикрепить медиа: {e}")

    try:
        twitter_client.create_tweet(text=tweet, media_ids=media_ids if media_ids else None)
        await context.bot.send_message(chat_id=user_id, text="✅ Твит опубликован!")
    except Exception as e:
        await context.bot.send_message(chat_id=user_id, text=f"⚠️ Ошибка при публикации: {e}")

    # Очистка
    pending_tweets.pop(user_id, None)
    original_messages.pop(user_id, None)
    user_states.pop(user_id, None)
    if media_path and os.path.exists(media_path):
        os.remove(media_path)
    pending_media.pop(user_id, None)

# ==== ОТЛОЖЕННАЯ ПУБЛИКАЦИЯ ====
async def schedule_tweet(user_id, time_dt, context):
    tweet = pending_tweets.get(user_id)
    if not tweet:
        return

    scheduled_queue.setdefault(user_id, []).append((tweet, time_dt))
    pending_tweets.pop(user_id, None)
    original_messages.pop(user_id, None)
    user_states.pop(user_id, None)

    async def delayed_post():
        now = datetime.utcnow().replace(tzinfo=pytz.utc)
        media_path = pending_media.get(user_id)
        delay = (time_dt - now).total_seconds()
        await asyncio.sleep(max(0, delay))

        media_ids = []
        if media_path and os.path.exists(media_path):
            try:
                if not media_path.endswith(".mp4"):
                    converted = convert_to_mp4(media_path)
                    if not converted or not os.path.exists(converted):
                        await context.bot.send_message(chat_id=user_id, text="❌ Ошибка при конвертации видео в .mp4")
                        return
                    media_path = converted  # заменяем путь на сконвертированный

                media = twitter_api.media_upload(filename=media_path, media_category='tweet_video')
                media_ids = [media.media_id]
            except Exception as e:
                await context.bot.send_message(chat_id=user_id, text=f"⚠️ Не удалось прикрепить медиа: {e}")
        try:
            twitter_client.create_tweet(text=tweet, media_ids=media_ids if media_ids else None)
            await context.bot.send_message(chat_id=user_id, text="✅ Отложенный твит опубликован!")
        except Exception as e:
            await context.bot.send_message(chat_id=user_id, text=f"⚠️ Ошибка при публикации: {e}")
        if user_id in scheduled_queue:
            scheduled_queue[user_id] = [x for x in scheduled_queue[user_id] if x[0] != tweet or x[1] != time_dt]

    asyncio.create_task(delayed_post())

# ==== КОМАНДА /see ====
async def see_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != ALLOWED_USER_ID:
        return

    posts = scheduled_queue.get(user_id, [])
    if not posts:
        await update.message.reply_text("📭 У тебя нет отложенных твитов.")
        return

    for idx, (text, dt) in enumerate(posts):
        msk_time = dt.astimezone(MSK).strftime('%Y-%m-%d %H:%M:%S')
        preview = text[:100].strip() + ('...' if len(text) > 100 else '')
        keyboard = [[InlineKeyboardButton("❌ Удалить", callback_data=f"cancel_post_{idx}")]]
        await update.message.reply_text(f"🕒 {msk_time} МСК\n{preview}", reply_markup=InlineKeyboardMarkup(keyboard))

# ==== ЗАПУСК ====
if __name__ == '__main__':
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.FORWARDED, text_handler))
    app.add_handler(CommandHandler("see", see_handler))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    print("Бот запущен...")
    app.run_polling()