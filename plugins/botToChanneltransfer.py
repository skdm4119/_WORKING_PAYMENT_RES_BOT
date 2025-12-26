import asyncio
import logging
from pyrogram import Client, filters
from config import API_ID, API_HASH, STRING

# --- LOGGER SETUP ---
# This ensures logs appear in your Heroku/Render console with timestamps
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# State dictionary to remember the user's request
btc_states = {}

# --- STEP 1: User replies to a message with /botToChannel ---
@Client.on_message(filters.command("botToChannel"))
async def ask_count(bot, message):
    user_id = message.from_user.id
    user_name = message.from_user.first_name
    
    logger.info(f"📥 Command '/botToChannel' received from User: {user_id} ({user_name})")

    # 1. Check if user replied to a message
    if not message.reply_to_message:
        logger.warning(f"⚠️ User {user_id} used command without replying to a file.")
        await message.reply_text(
            "⚠️ **Incorrect Usage**\n\n"
            "1. Find the **first file** you want to send in our chat.\n"
            "2. **Reply** to that file with: `/botToChannel -100xxxxxxx`"
        )
        return

    # 2. Get Channel ID
    try:
        dest_channel = int(message.command[1])
    except (IndexError, ValueError):
        logger.error(f"❌ User {user_id} provided invalid channel ID.")
        await message.reply_text("❌ **Error:** You forgot the Channel ID or it's invalid.\nUse: `/botToChannel -100xxxxxxx`")
        return

    # 3. Save state
    start_id = message.reply_to_message.id
    btc_states[user_id] = {
        "dest_chat": dest_channel,
        "start_msg_id": start_id
    }
    
    logger.info(f"✅ State Saved for {user_id} -> Dest: {dest_channel} | StartMsg: {start_id}")

    # 4. Ask for quantity
    await message.reply_text(
        f"✅ **Starting Point Selected!** (ID: `{start_id}`)\n"
        f"Target Channel: `{dest_channel}`\n\n"
        "**How many files** do you want to transfer?\n"
        "_(Type a number, e.g., 50)_"
    )

# --- STEP 2: User sends the number ---
@Client.on_message(filters.regex(r"^\d+$"))
async def start_btc_transfer(bot, message):
    user_id = message.from_user.id
    
    # Check if this user has a pending request
    if user_id not in btc_states:
        return # Ignore random numbers from others

    logger.info(f"🔢 User {user_id} sent number: {message.text}")

    state = btc_states[user_id]
    count = int(message.text)
    dest_chat = state["dest_chat"]
    start_id = state["start_msg_id"]
    
    # Clear state
    del btc_states[user_id]

    status_msg = await message.reply_text(f"🚀 **Processing...**\nCopying {count} files to `{dest_chat}`")
    logger.info(f"🚀 Starting Batch Transfer: {count} files starting from ID {start_id}")

    # Start the User Client (using STRING session)
    async with Client("btc_worker", api_id=API_ID, api_hash=API_HASH, session_string=STRING) as user_app:
        success = 0
        failed = 0
        
        # Get bot username to read chat history
        bot_info = await bot.get_me()
        chat_target = bot_info.username 

        for i in range(count):
            current_id = start_id + i 
            try:
                # Fetch message
                msg = await user_app.get_messages(chat_target, current_id)
                
                # If message exists and has a file
                if msg and not msg.empty and (msg.document or msg.video or msg.photo or msg.audio):
                    # Copy to channel
                    await msg.copy(chat_id=dest_chat, caption=msg.caption)
                    success += 1
                    logger.info(f"✅ Copied Message ID {current_id} to {dest_chat}")
                    
                    # Sleep to prevent FloodWait
                    await asyncio.sleep(2) 
                else:
                    logger.info(f"⏩ Skipped Message ID {current_id} (No media or deleted)")
                    pass 

                # Update status every 10 files
                if i % 10 == 0:
                    await status_msg.edit_text(f"🔄 **Progress:** {i}/{count}\n✅ Copied: {success}")

            except Exception as e:
                logger.error(f"❌ Failed to copy Message ID {current_id}: {e}")
                failed += 1
                await asyncio.sleep(2)

    logger.info(f"🏁 Transfer Complete. Requested: {count}, Success: {success}, Failed: {failed}")
    await status_msg.edit_text(f"✅ **Done!**\nRequested: {count}\nCopied: {success}\nSkipped/Failed: {failed}")
