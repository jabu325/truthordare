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
        f"Share this code with your friend:\n\n"
        f"🔑 {code}\n\n"
        f"They must use:\n"
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

            keyboard = [
                [InlineKeyboardButton("🎯 Truth", callback_data="truth")],
                [InlineKeyboardButton("🔥 Dare", callback_data="dare")]
            ]

            reply_markup = InlineKeyboardMarkup(keyboard)

            await context.bot.send_message(
                chat_id=player1_id,
                text="🎮 Game started!\n\nChoose Truth or Dare:",
                reply_markup=reply_markup
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

    difficulty = "easy"  # temporary until we wire difficulty selection
    prompts = get_random_prompts(choice, difficulty)
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

    # Only the NON-current player should select the prompt
    if telegram_id == current_turn:
        await query.answer("❌ You cannot select this!", show_alert=True)
        return

    prompts = get_game_prompt_options(code)

    if not prompts:
        await query.edit_message_text("❌ No prompts found.")
        return

    index = int(query.data.split("_")[1])
    selected_prompt = prompts[index]

    # Send final challenge to the current_turn player
    await context.bot.send_message(
        chat_id=current_turn,
        text=f"🎯 Your challenge:\n\n{selected_prompt}"
    )
    # Update game state to waiting for answer
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET state = ?
        WHERE code = ?
    """, ("AWAITING_ANSWER", code))

    conn.commit()
    conn.close()

    await query.edit_message_text("✅ Prompt selected!")
   
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

    await query.edit_message_text("✅ Prompt selected!")

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
    app.add_handler(CallbackQueryHandler(handle_choice, pattern="^(truth|dare)$"))
    app.add_handler(CallbackQueryHandler(handle_prompt_selection, pattern="^select_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_answer))
    app.add_handler(CommandHandler("stop", stop))
    print("Bot is running...")
    app.run_polling()
    


if __name__ == "__main__":
    main()

