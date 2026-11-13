import logging
from pathlib import Path
from telegram import Update
from database import join_game
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import CallbackQueryHandler, MessageHandler, filters
from database import get_active_game_by_user
from database import end_game_by_user
from database import seed_prompts
from database import get_connection
from database import (
    get_random_prompts,
    update_game_prompt_options,
    get_game_prompt_options,
    get_game_by_user,
    get_transcript,
    get_user_display_name,
    record_answer,
    record_prompt,
    get_full_game_state_by_user,
    restart_game_by_user,
    switch_turn
)
from database import (
    init_db,
    create_or_update_user,
    user_in_active_game,
    create_game
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from config import TOKEN
from admin_server import start_server

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)


async def safe_reply(message, text):
    try:
        await message.reply_text(text)
    except Exception:
        logging.getLogger(__name__).exception("Could not send media status reply")

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    telegram_id = user.id
    username = user.username or "NoUsername"
    display_name = user.full_name or username

    create_or_update_user(telegram_id, username, display_name)

    await update.message.reply_text(
        "Welcome to Truth or Dare 🎭\n\n"
        "Use /newgame to start a game."
    )

async def newgame(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id

    if user_in_active_game(telegram_id):
        await update.message.reply_text(
            "❌ You are already in an active game."
        )
        return

    code = create_game(telegram_id)

    await update.message.reply_text(
        f"🎮 Game created!\n\n"
        f"Share this code with your friend so they can join the truth or dare game:\n\n"
        f"🔑 {code}\n\n"
        f"They must click https://t.me/TruthDare225_bot , then use:\n"
    )
    await update.message.reply_text(
        f"/join {code}"
    )
async def join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id
    create_or_update_user(
        telegram_id,
        user.username or "NoUsername",
        user.full_name or user.username or str(telegram_id),
    )

    if len(context.args) != 1:
        await update.message.reply_text("Usage: /join CODE")
        return

    code = context.args[0].upper()

    if user_in_active_game(telegram_id):
        await update.message.reply_text(
            "❌ You are already in an active game."
        )
        return

    result = join_game(code, telegram_id)

    if result == "NOT_FOUND":
        await update.message.reply_text("❌ Game not found.")
    elif result == "NOT_AVAILABLE":
        await update.message.reply_text("❌ Game is not available.")
    elif result == "CANNOT_JOIN_SELF":
        await update.message.reply_text("❌ You cannot join your own game.")
    elif result == "FULL":
        await update.message.reply_text("❌ Game is already full.")
    elif result == "SUCCESS":
        await update.message.reply_text(
            "✅ You joined the game!\n\n"
            "Waiting for Player 1..."
        )

        # Notify Player 1 with buttons
        game = get_active_game_by_user(telegram_id)
        if game:
            code, player1_id, player2_id, current_turn, state = game

            # Ask Player1 to choose game type
            keyboard = [
                [InlineKeyboardButton("👥 Friends", callback_data="gametype_friends")],
                [InlineKeyboardButton("🫂 Best Friends", callback_data="gametype_best_friends")],
                [InlineKeyboardButton("❤️ Couples", callback_data="gametype_couples")]
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=player1_id,
                text="🎮 Both players joined!\n\nChoose game type:",
                reply_markup=reply_markup
            )
            
async def restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id

    result = restart_game_by_user(telegram_id)

    if not result:
        await update.message.reply_text("❌ No active game to restart.")
        return

    player1_id = result["player1_id"]
    player2_id = result["player2_id"]

    # Notify both players
    keyboard = [
        [InlineKeyboardButton("👥 Friends", callback_data="gametype_friends")],
        [InlineKeyboardButton("🫂 Best Friends", callback_data="gametype_best_friends")],
        [InlineKeyboardButton("❤️ Couples", callback_data="gametype_couples")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=player1_id,
        text="🔄 Game restarted!\n\nChoose game type:",
        reply_markup=reply_markup
    )

    if player2_id:
        await context.bot.send_message(
            chat_id=player2_id,
            text="🔄 The game has been restarted.\nWaiting for Player 1 to choose game type..."
        )

async def handle_game_type(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = user.id

    game = get_active_game_by_user(telegram_id)
    if not game:
        return

    code, player1_id, player2_id, current_turn, state = game

    # Only player1 chooses type
    if telegram_id != player1_id:
        await query.answer("Only Player 1 chooses game type!", show_alert=True)
        return

    game_type = query.data.replace("gametype_", "")

    # Save temporarily in context
    context.user_data["game_type"] = game_type

    # Now ask difficulty
    keyboard = [
        [InlineKeyboardButton("Easy", callback_data="difficulty_easy")],
        [InlineKeyboardButton("Hard", callback_data="difficulty_hard")]
    ]

    if game_type in ["best_friends", "couples"]:
        keyboard.append(
            [InlineKeyboardButton("Spicy", callback_data="difficulty_spicy")]
        )

    await query.edit_message_text(
        "Choose difficulty:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    ) 

async def handle_difficulty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = user.id

    game = get_active_game_by_user(telegram_id)
    if not game:
        return

    code, player1_id, player2_id, current_turn, state = game

    if telegram_id != player1_id:
        return

    difficulty = query.data.split("_")[1]
    game_type = context.user_data.get("game_type")

    # Save to DB
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET game_type = ?, difficulty = ?, state = ?
        WHERE code = ?
    """, (game_type, difficulty, "CHOOSING_TYPE", code))

    conn.commit()
    conn.close()

    # Now start game properly
    keyboard = [
        [InlineKeyboardButton("🎯 Truth", callback_data="truth")],
        [InlineKeyboardButton("🔥 Dare", callback_data="dare")]
    ]

    await query.edit_message_text(
        f"Game Mode: {game_type}\n"
        f"Difficulty: {difficulty}\n\n"
        f"🎮 Game starting!\n\nChoose Truth or Dare:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

    
async def stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id

    result = end_game_by_user(telegram_id)

    if not result:
        await update.message.reply_text(
            "❌ You are not in an active game."
        )
        return

    await update.message.reply_text(
        "🛑 Game ended successfully."
    )

    # Notify the other player
    other_player = None

    if result["player1_id"] == telegram_id:
        other_player = result["player2_id"]
    else:
        other_player = result["player1_id"]

    if other_player:
        try:
            await context.bot.send_message(
                chat_id=other_player,
                text="🛑 The game has been ended by the other player."
            )
        except:
            pass

async def handle_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = user.id

    game = get_active_game_by_user(telegram_id)

    if not game:
        await query.edit_message_text("❌ No active game found.")
        return

    code, player1_id, player2_id, current_turn, state = game

    if telegram_id != current_turn:
        await query.answer("❌ Not your turn!", show_alert=True)
        return

    choice = query.data  # truth or dare

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT game_type, difficulty
        FROM games
        WHERE code = ?
    """, (code,))

    settings = cursor.fetchone()
    conn.close()

    if not settings:
        await query.edit_message_text("Game settings missing.")
        return

    game_type, difficulty = settings

    prompts = get_random_prompts(choice, difficulty, game_type)
    update_game_prompt_options(code, choice, prompts)
    if len(prompts) < 3:
        await query.edit_message_text("❌ Not enough prompts available.")
        return

    # Send options to the OTHER player
    other_player = player2_id if telegram_id == player1_id else player1_id

    keyboard = [
        [InlineKeyboardButton(prompts[0], callback_data=f"select_0")],
        [InlineKeyboardButton(prompts[1], callback_data=f"select_1")],
        [InlineKeyboardButton(prompts[2], callback_data=f"select_2")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    # Save prompts temporarily in context
    context.user_data["current_prompts"] = prompts
    context.user_data["selected_type"] = choice

    await context.bot.send_message(
        chat_id=other_player,
        text=f"🎯 Choose one {choice.upper()} for your opponent:",
        reply_markup=reply_markup
    )

    await query.edit_message_text(
        f"✅ You selected {choice.upper()}.\nWaiting for opponent to choose..."
    )
    # Set state to CHOOSING_PROMPT
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET state = ?
        WHERE code = ?
    """, ("CHOOSING_PROMPT", code))

    conn.commit()
    conn.close()

async def resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id

    game = get_full_game_state_by_user(telegram_id)

    if not game:
        await update.message.reply_text("❌ No active game found.")
        return

    (code, player1_id, player2_id, current_turn, state,
     game_type, difficulty, current_prompt,
     selected_prompt, selected_type) = game

    other_player = player2_id if telegram_id == player1_id else player1_id

    if state == "CHOOSING_TYPE":
        if telegram_id == current_turn:
            keyboard = [
                [InlineKeyboardButton("🎯 Truth", callback_data="truth")],
                [InlineKeyboardButton("🔥 Dare", callback_data="dare")]
            ]
            await update.message.reply_text(
                "Your turn! Choose Truth or Dare:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text("⏳ Waiting for opponent...")

    elif state == "CHOOSING_PROMPT":
        prompts = selected_prompt.split("||") if selected_prompt else []
        if prompts and telegram_id != current_turn:
            keyboard = [
                [InlineKeyboardButton(prompts[0], callback_data="select_0")],
                [InlineKeyboardButton(prompts[1], callback_data="select_1")],
                [InlineKeyboardButton(prompts[2], callback_data="select_2")]
            ]
            await update.message.reply_text(
                f"Choose one {selected_type.upper()} for your opponent:",
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        else:
            await update.message.reply_text("⏳ Waiting for selection...")

    elif state == "AWAITING_ANSWER":
        if telegram_id == current_turn:
            await update.message.reply_text(
                f"🎯 Your challenge:\n\n{current_prompt}"
            )
        else:
            await update.message.reply_text("⏳ Waiting for opponent to answer...")

    else:
        await update.message.reply_text("Game state unclear.")

    
async def handle_prompt_selection(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    telegram_id = user.id

    game = get_active_game_by_user(telegram_id)

    if not game:
        await query.edit_message_text("❌ No active game found.")
        return

    code, player1_id, player2_id, current_turn, state = game

    # Step 2A: Ensure we are actually waiting for prompt selection
    if state != "CHOOSING_PROMPT":
        await query.answer("⚠ Already selected!", show_alert=True)
        return

    # Only the NON-current player selects the prompt
    if telegram_id == current_turn:
        await query.answer("❌ You cannot select this!", show_alert=True)
        return

    # Step 2B: Lock immediately (prevents double-click)
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET state = ?
        WHERE code = ?
    """, ("LOCKED", code))

    conn.commit()
    conn.close()

    prompts = get_game_prompt_options(code)

    if not prompts:
        await query.edit_message_text("❌ No prompts found.")
        return

    index = int(query.data.split("_")[1])
    selected_prompt = prompts[index]
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET current_prompt = ?
        WHERE code = ?
    """, (selected_prompt, code))

    conn.commit()
    conn.close()

    # Send challenge to the player whose turn it is
    await context.bot.send_message(
        chat_id=current_turn,
        text=f"🎯 Your challenge:\n\n{selected_prompt}"
    )

    # Step 2C: Now set state to waiting for answer
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET state = ?
        WHERE code = ?
    """, ("AWAITING_ANSWER", code))

    conn.commit()
    conn.close()

    await query.edit_message_text("✅ Prompt selected!... waiting for response.")
    record_prompt(
        code=code,
        challenged_player=current_turn,
        challenged_player_name=(
            get_user_display_name(current_turn)
        ),
        selected_by=telegram_id,
        selected_by_name=get_user_display_name(telegram_id),
        prompt_type=context.user_data.get("selected_type") or "unknown",
        prompt_text=selected_prompt,
    )

async def _finish_answer(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    answer_type: str,
    answer_text: str | None = None,
    answer_file_id: str | None = None,
    answer_file_unique_id: str | None = None,
    answer_media_kind: str | None = None,
    answer_media_path: str | None = None,
    answer_caption: str | None = None,
):
    user = update.effective_user
    telegram_id = user.id

    game = get_active_game_by_user(telegram_id)

    if not game:
        return

    code, player1_id, player2_id, current_turn, state = game

    # Only accept answers if game is waiting for answer
    if state != "AWAITING_ANSWER":
        return

    # Only the challenged player (current_turn) can answer
    if telegram_id != current_turn:
        return

    answerer_name = user.full_name or user.username or str(telegram_id)
    record_answer(
        code=code,
        answerer_id=telegram_id,
        answerer_name=answerer_name,
        answer_type=answer_type,
        answer_text=answer_text,
        answer_file_id=answer_file_id,
        answer_file_unique_id=answer_file_unique_id,
        answer_media_kind=answer_media_kind,
        answer_media_path=answer_media_path,
        answer_caption=answer_caption,
    )
    # Send answer to opponent
    other_player = player2_id if telegram_id == player1_id else player1_id

    media_caption = (
        f"{answerer_name} answered with {answer_media_kind or 'media'}."
        + (f"\n\n{answer_caption}" if answer_caption else "")
    )
    if answer_media_kind == "photo":
        await context.bot.send_photo(
            chat_id=other_player,
            photo=answer_file_id,
            caption=f"🖼️ {media_caption}",
        )
    elif answer_media_kind == "video":
        await context.bot.send_video(
            chat_id=other_player,
            video=answer_file_id,
            caption=f"🎥 {media_caption}",
        )
    elif answer_media_kind == "animation":
        await context.bot.send_animation(
            chat_id=other_player,
            animation=answer_file_id,
            caption=f"🎞️ {media_caption}",
        )
    elif answer_media_kind in {"image_document", "video_document"}:
        await context.bot.send_document(
            chat_id=other_player,
            document=answer_file_id,
            caption=f"📎 {media_caption}",
        )
    else:
        await context.bot.send_message(
            chat_id=other_player,
            text=f"💬 {answerer_name} answered:\n\n{answer_text}"
        )

    # Determine next turn
    next_turn = other_player

    # Switch turn properly now
    switch_turn(code, next_turn)

    # Send Truth/Dare buttons to next player
    keyboard = [
        [InlineKeyboardButton("🎯 Truth", callback_data="truth")],
        [InlineKeyboardButton("🔥 Dare", callback_data="dare")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await context.bot.send_message(
        chat_id=next_turn,
        text="Your turn! Choose Truth or Dare:",
        reply_markup=reply_markup
    )
    # Reset state back to choosing type
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET state = ?
        WHERE code = ?
    """, ("CHOOSING_TYPE", code))

    conn.commit()
    conn.close()


async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _finish_answer(
        update,
        context,
        answer_type="text",
        answer_text=update.message.text,
    )


async def handle_image_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    document_name = (message.document.file_name or "").lower() if message.document else ""
    image_document = (
        message.document
        and (
            (message.document.mime_type or "").startswith("image/")
            or document_name.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".heic", ".avif"))
        )
    )
    video_document = (
        message.document
        and (
            (message.document.mime_type or "").startswith("video/")
            or document_name.endswith((".mp4", ".mov", ".mkv", ".webm", ".avi", ".mpeg", ".m4v"))
        )
    )
    if message.photo:
        media = max(message.photo, key=lambda item: (item.width or 0) * (item.height or 0))
        media_kind = "photo"
    elif message.video:
        media = message.video
        media_kind = "video"
    elif message.animation:
        media = message.animation
        media_kind = "animation"
    elif image_document:
        media = message.document
        media_kind = "image_document"
    elif video_document:
        media = message.document
        media_kind = "video_document"
    else:
        await safe_reply(message, "Please send a supported image or video as your answer.")
        return

    try:
        telegram_file = await context.bot.get_file(media.file_id)
    except Exception:
        logging.getLogger(__name__).exception("Could not retrieve media with get_file")
        await safe_reply(message, "I could not retrieve that media from Telegram. Please try sending it again.")
        return
    media_dir = Path(__file__).resolve().parent / "media" / "answers"
    media_dir.mkdir(parents=True, exist_ok=True)
    extension = Path(telegram_file.file_path or "answer.bin").suffix.lower() or ".bin"
    if not extension.isalnum() and not (
        extension.startswith(".") and extension[1:].replace("_", "").isalnum()
    ):
        extension = ".bin"
    local_path = media_dir / f"{media.file_unique_id}{extension}"
    try:
        await telegram_file.download_to_drive(
            custom_path=local_path,
            read_timeout=180,
            write_timeout=60,
            connect_timeout=30,
            pool_timeout=30,
        )
    except Exception:
        logging.getLogger(__name__).exception("Could not download media")
        await safe_reply(message, "I could not download that media. Please try a smaller file or send it again.")
        return

    await _finish_answer(
        update,
        context,
        answer_type="media",
        answer_file_id=media.file_id,
        answer_file_unique_id=media.file_unique_id,
        answer_media_kind=media_kind,
        answer_media_path=str(local_path),
        answer_caption=message.caption,
    )


async def transcript(update: Update, context: ContextTypes.DEFAULT_TYPE):
    game = get_game_by_user(update.effective_user.id)

    if not game:
        await update.message.reply_text("❌ No game session found.")
        return

    code, player1_id, player2_id, state = game
    rows = get_transcript(code)
    if not rows:
        await update.message.reply_text(
            f"📝 Session {code} has no completed questions yet."
        )
        return

    lines = [
        f"📝 *Truth or Dare transcript*",
        f"Session: `{code}`",
        f"Status: `{state}`",
        f"Players: `{player1_id}` and `{player2_id or 'waiting'}`",
        "",
    ]

    for index, row in enumerate(rows, start=1):
        (
            challenged_name,
            challenged_id,
            selected_name,
            selected_id,
            prompt_type,
            prompt_text,
            answerer_name,
            answerer_id,
            answer_type,
            answer_text,
            answer_file_id,
            answer_caption,
            created_at,
            answered_at,
        ) = row
        lines.extend([
            f"*Round {index}* ({prompt_type.upper()})",
            f"Question chosen by {selected_name} (`{selected_id}`)",
            f"For {challenged_name} (`{challenged_id}`): {prompt_text}",
        ])
        if answer_type and answer_type not in {"text", "pending"} or answer_file_id:
            answer_summary = f"📎 {answer_type or 'Media'} answer"
            if answer_caption:
                answer_summary += f": {answer_caption}"
        elif answer_text:
            answer_summary = answer_text
        else:
            answer_summary = "(no answer yet)"
        if answerer_name:
            lines.append(
                f"Answer by {answerer_name} (`{answerer_id}`): {answer_summary}"
            )
        else:
            lines.append(f"Answer: {answer_summary}")
        lines.append(f"Recorded: {created_at or 'unknown'}")
        if answered_at:
            lines.append(f"Answered: {answered_at}")
        lines.append("")

    transcript_text = "\n".join(lines)
    for start in range(0, len(transcript_text), 3800):
        await update.message.reply_text(
            transcript_text[start:start + 3800],
            parse_mode="Markdown",
        )

def main():
    
    init_db()
    seed_prompts()

    admin_server = start_server()
    admin_host, admin_port = admin_server.server_address
    print(f"Admin server listening at http://{admin_host}:{admin_port}", flush=True)

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(10)
        .get_updates_connect_timeout(10)
        .get_updates_pool_timeout(10)
        .read_timeout(180)
        .write_timeout(60)
        .connect_timeout(30)
        .pool_timeout(30)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CallbackQueryHandler(handle_game_type, pattern="^gametype_"))
    app.add_handler(CallbackQueryHandler(handle_difficulty, pattern="^difficulty_"))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern="^(truth|dare)$"))
    app.add_handler(CallbackQueryHandler(handle_prompt_selection, pattern="^select_"))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(CommandHandler("transcript", transcript))
    app.add_handler(
        MessageHandler(
            filters.PHOTO
            | filters.VIDEO
            | filters.ANIMATION
            | filters.Document.ALL,
            handle_image_answer,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))
    app.add_handler(CommandHandler("stop", stop))
    print("Bot is running...")
    try:
        app.run_polling(
            poll_interval=0.0,
            timeout=20,
            bootstrap_retries=-1,
        )
    finally:
        admin_server.shutdown()
    


if __name__ == "__main__":
    main()

