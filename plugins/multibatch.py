import asyncio
import re
from typing import List, Tuple, Optional

from pyrogram import Client, filters
from pyrogram.types import Message

# We will reuse your existing /batch "runner" to avoid duplicating logic.
# This repo almost certainly has it in plugins/batch.py
try:
    from plugins.batch import run_batch  # type: ignore
except Exception:
    run_batch = None

# Optional: if your project exposes a "userbot" client (many forks do), we try to import it
# ONLY for warmup/ping (doesn't send messages; just fetches first message).
try:
    from shared_client import userbot  # type: ignore
except Exception:
    userbot = None

# ---------- Config ----------
SLOT_DELAY_SECONDS = 60
MAX_SLOTS = 10
ASK_TIMEOUT = 600  # seconds

# Global guard: only one multibatch at a time (as you requested)
_MULTIBATCH_LOCK = asyncio.Lock()

# Track cancel requests per user
_MULTIBATCH_CANCELLED = set()


# ---------- Helpers ----------
_LINK_RE = re.compile(r"(?:https?://)?t\.me/(c/)?(?P<chat>[\w\d_]+)/(?P<msg>\d+)", re.IGNORECASE)


def _parse_message_link(link: str):
    """
    Parses:
      - https://t.me/c/<internal_id>/<msg_id>
      - https://t.me/<username>/<msg_id>
    Returns (chat, msg_id) where chat is int(-100...) or str(username)
    """
    link = link.strip()
    link = link.split("?")[0]  # remove query params like ?single
    m = _LINK_RE.search(link)
    if not m:
        return None, None

    is_private_c = bool(m.group(1))
    chat_part = m.group("chat")
    msg_id = int(m.group("msg"))

    if is_private_c:
        # /c/<id>/<msg> => real chat id is -100<id>
        if not chat_part.isdigit():
            return None, None
        chat = int(f"-100{chat_part}")
    else:
        # username or numeric id (rare)
        chat = chat_part

    return chat, msg_id


async def _warmup_link(client: Client, link: str) -> None:
    """
    "Ping" / warmup: fetch the first message to validate link & cache peer.
    Does NOT send anything to the chat.
    """
    chat, msg_id = _parse_message_link(link)
    if chat is None or msg_id is None:
        return

    # Prefer userbot if available (usually has access to private content)
    try:
        if userbot is not None:
            await userbot.get_messages(chat, msg_id)
            return
    except Exception:
        pass

    # Fallback: try with bot client (might fail for private)
    try:
        await client.get_messages(chat, msg_id)
    except Exception:
        pass


async def _call_run_batch(client: Client, message: Message, link: str, count: int):
    """
    Calls existing run_batch with multiple common signatures.
    Your repo's run_batch signature may differ; this tries a few safely.
    """
    if run_batch is None:
        await message.reply_text(
            "❌ Could not import `run_batch` from plugins/batch.py.\n"
            "Please ensure plugins/batch.py has a function named `run_batch`."
        )
        return

    uid = message.from_user.id if message.from_user else message.chat.id

    # Try common patterns:
    # 1) run_batch(userbot, bot_client, sender_id, link, count)
    try:
        if userbot is not None:
            return await run_batch(userbot, client, uid, link, count)
    except TypeError:
        pass
    except Exception:
        # If it fails for runtime reasons, continue to other signatures
        pass

    # 2) run_batch(client, uid, link, count)
    try:
        return await run_batch(client, uid, link, count)
    except TypeError:
        pass
    except Exception:
        pass

    # 3) run_batch(client, message, link, count)
    try:
        return await run_batch(client, message, link, count)
    except TypeError:
        pass
    except Exception:
        pass

    # 4) run_batch(client, link, count)
    try:
        return await run_batch(client, link, count)
    except TypeError:
        pass
    except Exception:
        pass

    await message.reply_text(
        "❌ Couldn't call `run_batch()` with any known signature.\n"
        "Open plugins/batch.py, check `def run_batch(...):` parameters, then adjust `_call_run_batch()`."
    )


async def _ask_text(client: Client, chat_id: int, prompt: str) -> str:
    """
    Uses pyromod-style ask/listen if your project has it.
    If your project doesn't have `client.ask`, install/enable pyromod.
    """
    if not hasattr(client, "ask"):
        raise RuntimeError(
            "This /multibatch implementation needs `client.ask` (pyromod).\n"
            "If your repo already uses /batch interactive prompts, pyromod is likely enabled.\n"
            "Otherwise, add pyromod and enable listen."
        )

    m = await client.ask(
        chat_id=chat_id,
        text=prompt,
        filters=filters.text,
        timeout=ASK_TIMEOUT
    )
    return (m.text or "").strip()


# ---------- Commands ----------
@Client.on_message(filters.command(["cancelmultibatch", "multicancel"]) & filters.private)
async def cancel_multibatch_cmd(client: Client, message: Message):
    uid = message.from_user.id
    _MULTIBATCH_CANCELLED.add(uid)
    await message.reply_text("✅ Multi-batch cancel requested. It will stop after the current slot finishes.")


@Client.on_message(filters.command("multibatch") & filters.private)
async def multibatch_cmd(client: Client, message: Message):
    uid = message.from_user.id

    # Hard rule: only ONE multibatch globally at a time
    if _MULTIBATCH_LOCK.locked():
        return await message.reply_text("⚠️ A multibatch is already running. Try again after it finishes.")

    async with _MULTIBATCH_LOCK:
        _MULTIBATCH_CANCELLED.discard(uid)

        try:
            # 1) ask slots
            slots_raw = await _ask_text(
                client,
                uid,
                f"How many slots do you want to book? (1-{MAX_SLOTS})\n\n"
                f"Send /cancelmultibatch to stop anytime."
            )

            if slots_raw.startswith("/"):
                return await message.reply_text("❌ Cancelled.")

            try:
                slots = int(slots_raw)
            except ValueError:
                return await message.reply_text("❌ Please send a number (example: 3). Run /multibatch again.")

            if slots < 1 or slots > MAX_SLOTS:
                return await message.reply_text(f"❌ Slots must be between 1 and {MAX_SLOTS}. Run /multibatch again.")

            queue: List[Tuple[str, int]] = []

            # 2) collect slot details
            for i in range(1, slots + 1):
                if uid in _MULTIBATCH_CANCELLED:
                    return await message.reply_text("❌ Multi-batch cancelled.")

                link = await _ask_text(client, uid, f"Slot {i}: ✅ Send batch link")
                if link.startswith("/"):
                    return await message.reply_text("❌ Cancelled.")

                count_raw = await _ask_text(client, uid, f"Slot {i}: ✅ How many messages?")
                if count_raw.startswith("/"):
                    return await message.reply_text("❌ Cancelled.")

                try:
                    count = int(count_raw)
                except ValueError:
                    return await message.reply_text("❌ Message count must be a number. Run /multibatch again.")

                if count < 1:
                    return await message.reply_text("❌ Message count must be >= 1. Run /multibatch again.")

                queue.append((link, count))

            await message.reply_text(
                "✅ Multi-batch queued!\n\n"
                + "\n".join([f"• Slot {idx+1}: {c} msgs" for idx, (_, c) in enumerate(queue)])
                + "\n\n▶️ Starting Slot 1 now..."
            )

            # 3) run slots sequentially
            for idx, (link, count) in enumerate(queue, start=1):
                if uid in _MULTIBATCH_CANCELLED:
                    await client.send_message(uid, "❌ Multi-batch stopped.")
                    break

                # Warmup remaining slots after finishing previous one (your “pinging slot2/slot3” idea)
                # For slot 1 start, also warmup slot2..slotN once quickly (doesn't spam chats)
                if idx == 1 and len(queue) > 1:
                    for (next_link, _) in queue[1:]:
                        await _warmup_link(client, next_link)

                if idx > 1:
                    await client.send_message(
                        uid,
                        f"⏳ Slot {idx}/{len(queue)} will start in {SLOT_DELAY_SECONDS} seconds..."
                    )
                    await asyncio.sleep(SLOT_DELAY_SECONDS)

                await client.send_message(
                    uid,
                    f"▶️ Starting Slot {idx}/{len(queue)}\n"
                    f"• Link: {link}\n"
                    f"• Messages: {count}"
                )

                await _call_run_batch(client, message, link, count)

                await client.send_message(uid, f"✅ Slot {idx}/{len(queue)} finished.")

                # After each slot, warmup remaining slots once (ping)
                if idx < len(queue):
                    for (next_link, _) in queue[idx:]:
                        await _warmup_link(client, next_link)

            await client.send_message(uid, "✅ Multi-batch completed.")

        except asyncio.TimeoutError:
            await message.reply_text("⏱️ Timed out waiting for your reply. Run /multibatch again.")
        except Exception as e:
            await message.reply_text(f"❌ Error in /multibatch: `{e}`")
