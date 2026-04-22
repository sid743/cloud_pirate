import os
import uuid
import zipfile
import shutil
import asyncio
import sqlite3
from PIL import Image, ImageDraw, ImageFont
import torch
import torchvision.models as models
import torchvision.transforms as transforms
from sklearn.cluster import AgglomerativeClustering
import numpy as np

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

# Init MobileNetV2 for feature extraction
model = models.mobilenet_v2(weights=models.MobileNet_V2_Weights.IMAGENET1K_V1)
model.eval()
preprocess = transforms.Compose([
    transforms.Resize(256),
    transforms.CenterCrop(224),
    transforms.ToTensor(),
    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
])

def extract_features(img_path):
    try:
        img = Image.open(img_path).convert('RGB')
        tensor = preprocess(img).unsqueeze(0)
        with torch.no_grad():
            features = model.features(tensor)
            features = torch.nn.functional.adaptive_avg_pool2d(features, (1, 1))
            return features.flatten().numpy()
    except:
        return np.zeros(1280)

async def handle_zip_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, cursor, conn, group_id, topic_func):
    msg = await update.message.reply_text("📥 Downloading ZIP file...")
    user = update.effective_user
    
    file_id = update.message.document.file_id
    new_file = await context.bot.get_file(file_id)
    
    work_dir = f"./ml_workspace/{uuid.uuid4()}"
    os.makedirs(work_dir, exist_ok=True)
    zip_path = os.path.join(work_dir, "upload.zip")
    extract_dir = os.path.join(work_dir, "extracted")
    
    await new_file.download_to_drive(zip_path)
    await msg.edit_text("🗜️ Unzipping and analyzing images using MobileNetV2...")
    
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)

    # Gather images and extract features
    image_paths = []
    features = []
    valid_exts = ('.png', '.jpg', '.jpeg')
    
    for root, _, files in os.walk(extract_dir):
        for file in files:
            if file.lower().endswith(valid_exts):
                path = os.path.join(root, file)
                image_paths.append(path)
                features.append(extract_features(path))
                
    # CRITICAL FIX: AgglomerativeClustering requires at least 2 images
    if len(image_paths) < 2:
        await msg.edit_text("❌ Please upload a ZIP containing at least 2 images to run clustering.")
        shutil.rmtree(work_dir, ignore_errors=True)
        return

    await msg.edit_text("🧠 Running visual clustering algorithm...")
    X = np.array(features)
    # Dynamic clustering based on visual distance threshold
    clusterer = AgglomerativeClustering(n_clusters=None, distance_threshold=25.0, linkage='ward')
    labels = clusterer.fit_predict(X)
    
    clusters = {}
    for path, label in zip(image_paths, labels):
        if label not in clusters: clusters[label] = []
        clusters[label].append(path)

    await msg.edit_text("🎨 Generating cluster collage...")
    
    # Collage Generation 
    thumb_size = 200
    cols = min(3, len(clusters))
    rows = (len(clusters) + cols - 1) // cols
    
    collage_w = cols * thumb_size
    collage_h = rows * (thumb_size + 40) # Extra space for labels
    collage = Image.new('RGB', (collage_w, collage_h), color='black') # Cyan on Black aesthetic
    draw = ImageDraw.Draw(collage)
    
    try:
        font = ImageFont.truetype("arial.ttf", 20)
    except:
        font = ImageFont.load_default()

    topic_id = await topic_func(context, user.id, user.first_name)
    bundle_id = str(uuid.uuid4())[:8] # Master ID for this ZIP run
    keyboard = []

    for idx, (label_id, paths) in enumerate(clusters.items()):
        class_name = f"Class {label_id}"
        rep_img = Image.open(paths[0]).convert('RGB')
        rep_img.thumbnail((thumb_size, thumb_size))
        
        x = (idx % cols) * thumb_size
        y = (idx // cols) * (thumb_size + 40)
        
        # Center the thumbnail in its grid slot
        offset_x = x + (thumb_size - rep_img.width) // 2
        offset_y = y + (thumb_size - rep_img.height) // 2
        collage.paste(rep_img, (offset_x, offset_y))
        
        # Draw label in cyan text against the black background for high contrast
        draw.text((x + 10, y + thumb_size + 10), class_name, fill="#00FFFF", font=font) 
        
        # Use hyphens instead of underscores to prevent Telegram formatting issues
        class_bundle_id = f"{bundle_id}-{label_id}"
        
        # Upload all images in this class to the supergroup in the background
        for img_path in paths:
            with open(img_path, 'rb') as f:
                # CRITICAL FIX: Increased timeouts for large file uploads
                sent = await context.bot.send_photo(
                    chat_id=group_id, 
                    message_thread_id=topic_id, 
                    photo=f,
                    read_timeout=60,
                    write_timeout=60,
                    connect_timeout=60
                )
                uid = str(uuid.uuid4())[:8]
                cursor.execute('''INSERT INTO files (uid, file_id, file_unique_id, file_type, file_name, owner_id, bundle_id) 
                                  VALUES (?, ?, ?, ?, ?, ?, ?)''', 
                                  (uid, sent.photo[-1].file_id, sent.photo[-1].file_unique_id, "photo", class_name, user.id, class_bundle_id))
                
                # Prevent Telegram API FloodWait errors
                await asyncio.sleep(0.3) 
                
        conn.commit()
        
        keyboard.append([InlineKeyboardButton(f"📥 Get {class_name} ({len(paths)} imgs)", callback_data=f"cluster_{class_bundle_id}")])

    collage_path = os.path.join(work_dir, "collage.jpg")
    collage.save(collage_path)
    
    await msg.delete()
    with open(collage_path, 'rb') as f:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id, 
            photo=f, 
            caption=f"✅ **Clustering Complete!**\nFound {len(clusters)} distinct visual groups.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )

    # Cleanup local machine
    shutil.rmtree(work_dir, ignore_errors=True)

async def photo_cluster_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    # Get the class_bundle_id from the button data
    class_bundle_id = query.data.split('cluster_')[1]
    
    # Temporarily connect to the DB locally just for this callback to retrieve the files
    conn = sqlite3.connect('filestore_v2.db', check_same_thread=False)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM files WHERE bundle_id = ?', (class_bundle_id,))
    files_data = cursor.fetchall()
    
    if not files_data:
        await query.message.reply_text("❌ Could not locate these files in your cloud.")
        conn.close()
        return
        
    class_name = files_data[0]['file_name']
    
    # Send the Header
    await query.message.reply_text(f"📁 **{class_name}** ⬇️", parse_mode=ParseMode.MARKDOWN)
    
    # Send all photos directly to the chat
    for file_data in files_data:
        try:
            await context.bot.send_photo(
                chat_id=update.effective_chat.id, 
                photo=file_data['file_id']
            )
            await asyncio.sleep(0.3) # Prevent API limits when sending back many photos
        except Exception as e:
            print(f"Failed to send a photo: {e}")
            
    conn.close()
