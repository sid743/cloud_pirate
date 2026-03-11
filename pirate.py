import os
import time
import re
import uuid
import requests
import asyncio
import shutil
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# DB references set from app.py
_cursor = None
_conn = None
_GROUP_ID = None
_get_or_create_topic = None

def setup_db(cursor, conn, group_id, topic_func):
    global _cursor, _conn, _GROUP_ID, _get_or_create_topic
    _cursor = cursor
    _conn = conn
    _GROUP_ID = group_id
    _get_or_create_topic = topic_func

async def perform_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    await update.message.reply_text(f"🔍 Searching APIBay for: {text}...")
    
    try:
        resp = requests.get(f"https://apibay.org/q.php?q={text}")
        valid_data = [d for d in resp.json() if d.get('id') != '0']
        valid_data.sort(key=lambda x: (int(x.get('seeders', 0)), int(x.get('size', 0))), reverse=True)
        
        keyboard = []
        for item in valid_data[:10]:
            name = item.get('name', 'Unknown')[:35] 
            size_mb = int(item.get('size', 0)) / (1024 * 1024)
            info_hash = item.get('info_hash')
            keyboard.append([InlineKeyboardButton(f"🏴‍☠️ {name} | {size_mb:.1f}MB", callback_data=f"tor_{info_hash}")])
            
        await update.message.reply_text("Select a torrent to download:", reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Search error: {e}")

async def hacker_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    info_hash = query.data.split('_')[1]
    
    await query.edit_message_text(f"⏳ Torrent locked. Initiating background download...")
    asyncio.create_task(download_torrent_task(info_hash, update.effective_user, update.effective_chat.id, context))

async def download_torrent_task(info_hash: str, user, chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    magnet_link = f"magnet:?xt=urn:btih:{info_hash}&tr=udp://tracker.opentrackr.org:1337/announce"
    download_dir = f'./downloads/{info_hash}'
    os.makedirs(download_dir, exist_ok=True)
    
    progress_msg = await context.bot.send_message(chat_id, "⏬ Starting aria2c engine...")
    
    try:
        process = await asyncio.create_subprocess_exec(
            'aria2c', '--dir', download_dir, '--seed-time=0', '--bt-stop-timeout=300', '--summary-interval=2', magnet_link,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        
        last_update = time.time()
        while True:
            line = await process.stdout.readline()
            if not line: break
            line_str = line.decode('utf-8').strip()
            
            if "%" in line_str and "ETA" in line_str and (time.time() - last_update > 4):
                try:
                    await progress_msg.edit_text(f"⏬ Downloading Payload...\n\n`{line_str}`", parse_mode=ParseMode.MARKDOWN)
                    last_update = time.time()
                except: pass
                
        await process.wait()
        await progress_msg.edit_text("✅ Download complete. Packaging bundle to Supergroup...")
        
        topic_id = await _get_or_create_topic(context, user.id, user.first_name)
        bundle_id = str(uuid.uuid4())[:8]
        
        files_uploaded = 0
        for root, _, files in os.walk(download_dir):
            for file in files:
                if file.endswith('.aria2'): continue
                file_path = os.path.join(root, file)
                
                with open(file_path, 'rb') as f:
                    msg = await context.bot.send_document(chat_id=_GROUP_ID, message_thread_id=topic_id, document=f)
                    doc = msg.document
                    
                    uid = str(uuid.uuid4())[:8]
                    _cursor.execute('''INSERT INTO files (uid, file_id, file_unique_id, file_type, file_name, owner_id, bundle_id) 
                                      VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                      (uid, doc.file_id, doc.file_unique_id, "document", doc.file_name, user.id, bundle_id))
                    _conn.commit()
                    files_uploaded += 1

        # Cleanup local machine
        shutil.rmtree(download_dir, ignore_errors=True)
        
        if files_uploaded > 0:
            link = f"https://t.me/{context.bot.username}?start={bundle_id}"
            await context.bot.send_message(chat_id, f"🎉 **Torrent Bundled!**\nUploaded {files_uploaded} files.\n🔗 Retrieve entire bundle: {link}", parse_mode=ParseMode.MARKDOWN)
        
    except Exception as e:
        await context.bot.send_message(chat_id, f"❌ System Exception: {e}")
