import logging
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

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    telegram_id = user.id
    username = user.username or "NoUsername"

    create_or_update_user(telegram_id, username)

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
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO game_history (game_code, challenged_player, prompt_type, prompt_text)
        VALUES (?, ?, ?, ?)
    """, (code, current_turn, context.user_data.get("selected_type"), selected_prompt))

    conn.commit()
    conn.close()

async def handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE):
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

    answer = update.message.text
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE game_history
        SET answer_text = ?
        WHERE game_code = ?
        AND challenged_player = ?
        AND answer_text IS NULL
        ORDER BY id DESC
        LIMIT 1
    """, (answer, code, telegram_id))

    conn.commit()
    conn.close()
    # Send answer to opponent
    other_player = player2_id if telegram_id == player1_id else player1_id

    await context.bot.send_message(
        chat_id=other_player,
        text=f"💬 Your opponent answered:\n\n{answer}"
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

def main():
    
    init_db()
    seed_prompts()

    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("newgame", newgame))
    app.add_handler(CommandHandler("join", join))
    app.add_handler(CallbackQueryHandler(handle_game_type, pattern="^gametype_"))
    app.add_handler(CallbackQueryHandler(handle_difficulty, pattern="^difficulty_"))
    app.add_handler(CommandHandler("restart", restart))
    app.add_handler(CallbackQueryHandler(handle_choice, pattern="^(truth|dare)$"))
    app.add_handler(CallbackQueryHandler(handle_prompt_selection, pattern="^select_"))
    app.add_handler(CommandHandler("resume", resume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))
    app.add_handler(CommandHandler("stop", stop))
    print("Bot is running...")
    app.run_polling()
    


if __name__ == "__main__":
    main()

