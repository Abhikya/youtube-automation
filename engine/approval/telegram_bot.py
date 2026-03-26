import os
from dotenv import load_dotenv

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, Bot
from telegram.ext import ApplicationBuilder, CallbackQueryHandler, ContextTypes

from engine.uploader.youtube_uploader import upload_video

load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

bot = Bot(token=BOT_TOKEN)


# 🚀 SEND VIDEO
async def send_video_for_approval(video_data):

    video_path = video_data["video_path"]

    # 🔥 short unique key (last 40 chars only)
    short_path = video_path[-40:]

    # 🔥 store mapping (TEMP FILE)
    import json
    TEMP_FILE = "temp_map.json"

    if os.path.exists(TEMP_FILE):
        with open(TEMP_FILE, "r") as f:
            data = json.load(f)
    else:
        data = {}

    data[short_path] = video_path

    with open(TEMP_FILE, "w") as f:
        json.dump(data, f)

    title = video_data["metadata"]["Title"]
    description = video_data["metadata"]["Description"]
    hashtags = video_data["metadata"]["Hashtags"]

    caption = f"""{title}

{description}

{' '.join(hashtags)}

Approve?"""

    keyboard = [
        [
            InlineKeyboardButton("✅ Approve", callback_data=f"approve|{short_path}"),
            InlineKeyboardButton("❌ Reject", callback_data=f"reject|{short_path}"),
        ]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    with open(video_path, "rb") as video:
        await bot.send_video(
            chat_id=CHAT_ID,
            video=video,
            caption=caption,
            reply_markup=reply_markup,
        )


# 🚀 HANDLE BUTTON
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    query = update.callback_query
    await query.answer()

    action, short_path = query.data.split("|")

    import json
    TEMP_FILE = "temp_map.json"

    if not os.path.exists(TEMP_FILE):
        await query.edit_message_caption("❌ Storage missing")
        return

    with open(TEMP_FILE, "r") as f:
        data = json.load(f)

    if short_path not in data:
        await query.edit_message_caption("❌ Video not found")
        return

    video_path = data[short_path]

    if not os.path.exists(video_path):
        await query.edit_message_caption("❌ File missing")
        return

    if action == "approve":

        await query.edit_message_caption("Uploading... 🚀")

        upload_video(
            video_path,
            os.path.basename(video_path),
            "Auto generated",
            ["AI", "shorts"]
        )

        await query.edit_message_caption("✅ Uploaded!")

    elif action == "reject":

        await query.edit_message_caption("❌ Rejected")


# 🚀 START BOT
def start_bot():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CallbackQueryHandler(button_handler))

    print("Bot running...")

    app.run_polling()