import os
import sqlite3
import logging
import uuid
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder, ContextTypes, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters
)

import pirate
import photo_handler

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_ID = os.getenv("GROUP_ID")

# --- DATABASE SETUP ---
conn = sqlite3.connect('filestore_v2.db', check_same_thread=False)
conn.row_factory = sqlite3.Row
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, topic_id INTEGER)
''')
# Added bundle_id for torrents and clustered photos
cursor.execute('''
    CREATE TABLE IF NOT EXISTS files (
        uid TEXT PRIMARY KEY, file_id TEXT, file_unique_id TEXT, 
        file_type TEXT, file_name TEXT, owner_id INTEGER, bundle_id TEXT
    )
''')
conn.commit()

async def get_or_create_topic(context: ContextTypes.DEFAULT_TYPE, user_id: int, user_name: str) -> int:
    cursor.execute('SELECT topic_id FROM users WHERE user_id = ?', (user_id,))
    row = cursor.fetchone()
    if row: return row['topic_id']
    try:
        topic = await context.bot.create_forum_topic(chat_id=GROUP_ID, name=f"{user_name} ({user_id})")
        topic_id = topic.message_thread_id
        cursor.execute('INSERT INTO users (user_id, topic_id) VALUES (?, ?)', (user_id, topic_id))
        conn.commit()
        return topic_id
    except Exception as e:
        logging.error(f"Failed to create topic: {e}")
        return None

# --- START MENU ---
async def start_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Handle bundle/file retrieval via deep linking
    if context.args:
        uid_or_bundle = context.args[0]
        cursor.execute('SELECT * FROM files WHERE uid = ? OR bundle_id = ?', (uid_or_bundle, uid_or_bundle))
        files_data = cursor.fetchall()
        
        if not files_data:
            await update.message.reply_text("❌ File or bundle not found.")
            return
            
        await update.message.reply_text("📦 Retrieving your requested items...")
        for file_data in files_data:
            if file_data['file_type'] == "document":
                await update.message.reply_document(file_data['file_id'])
            elif file_data['file_type'] == "photo":
                await update.message.reply_photo(file_data['file_id'])
            elif file_data['file_type'] == "video":
                await update.message.reply_video(file_data['file_id'])
        return

    keyboard = [
        [InlineKeyboardButton("📁 File Upload", callback_data="menu_upload")],
        [InlineKeyboardButton("🏴‍☠️ Torrent Search", callback_data="menu_torrent")],
        [InlineKeyboardButton("🖼️ Photos (Auto-Group)", callback_data="menu_photos")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text("👋 Welcome to your Cloud! Select an option below:", reply_markup=reply_markup)

async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    if query.data == "menu_upload":
        await query.edit_message_text("📁 **File Upload**\n\nJust send me any file directly, and I'll securely store it in your cloud.", parse_mode=ParseMode.MARKDOWN)
    elif query.data == "menu_torrent":
        await query.edit_message_text("🏴‍☠️ **Torrent Mode**\n\nSend me your search query below:")
        context.user_data['state'] = 'WAITING_FOR_TORRENT'
    elif query.data == "menu_photos":
        await query.edit_message_text("🖼️ **Auto-Group Photos**\n\nSend me a `.zip` file containing your images. I'll analyze, cluster them visually, and send you a categorized collage.")
        context.user_data['state'] = 'WAITING_FOR_ZIP'

async def universal_message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state')
    
    if state == 'WAITING_FOR_TORRENT' and update.message.text:
        context.user_data['state'] = None
        await torrent_handler.perform_search(update, context)
        return
        
    if update.message.document:
        if state == 'WAITING_FOR_ZIP' and update.message.document.file_name.endswith('.zip'):
            context.user_data['state'] = None
            await photo_handler.handle_zip_upload(update, context, cursor, conn, GROUP_ID, get_or_create_topic)
            return
            
        # Standard File Upload Flow
        user = update.effective_user
        file_id = update.message.document.file_id
        file_unique_id = update.message.document.file_unique_id
        raw_name = update.message.document.file_name

        await update.message.reply_chat_action("upload_document")
        topic_id = await get_or_create_topic(context, user.id, user.first_name)
        
        await context.bot.forward_message(
            chat_id=GROUP_ID, from_chat_id=update.message.chat_id,
            message_id=update.message.message_id, message_thread_id=topic_id
        )

        uid = str(uuid.uuid4())[:8]
        cursor.execute('''INSERT INTO files (uid, file_id, file_unique_id, file_type, file_name, owner_id) 
                          VALUES (?, ?, ?, ?, ?, ?)''', (uid, file_id, file_unique_id, "document", raw_name, user.id))
        conn.commit()

        link = f"https://t.me/{context.bot.username}?start={uid}"
        await update.message.reply_text(f"✅ Saved: {raw_name}\n🔗 Link: {link}")

if __name__ == '__main__':
    logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Pass db dependencies to the external handlers
    torrent_handler.setup_db(cursor, conn, GROUP_ID, get_or_create_topic)
    
    app.add_handler(CommandHandler("start", start_menu))
    app.add_handler(CallbackQueryHandler(menu_callback, pattern="^menu_"))
    
    # Torrent callbacks
    app.add_handler(CallbackQueryHandler(torrent_handler.hacker_callback_handler, pattern="^tor_"))
    
    # Photo callbacks
    app.add_handler(CallbackQueryHandler(photo_handler.photo_cluster_callback, pattern="^cluster_"))

    # Universal handler catches text (for torrent searches) and files (for zips/standard uploads)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, universal_message_handler))

    print("Bot is running...")
    app.run_polling()
