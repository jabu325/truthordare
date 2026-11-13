import sqlite3
from pathlib import Path
import random
import string

DB_NAME = Path(__file__).resolve().parent / "truth_or_dare.db"

def get_connection():
    return sqlite3.connect(DB_NAME)

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    # USERS TABLE
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        telegram_id INTEGER UNIQUE NOT NULL,
        username TEXT,
        display_name TEXT
    )
    """)

    # PROMPTS TABLE
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            difficulty TEXT NOT NULL,
            game_type TEXT NOT NULL,
            text TEXT NOT NULL
        )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS game_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        game_code TEXT,
        challenged_player INTEGER,
        challenged_player_name TEXT,
        selected_by INTEGER,
        selected_by_name TEXT,
        prompt_type TEXT,
        prompt_text TEXT,
        answer_text TEXT,
        answerer_id INTEGER,
        answerer_name TEXT,
        answer_type TEXT,
        answer_file_id TEXT,
        answer_file_unique_id TEXT,
        answer_media_kind TEXT,
        answer_media_path TEXT,
        answer_caption TEXT,
        answered_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    cursor.execute("""
    CREATE TABLE IF NOT EXISTS game_sessions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        chat_id INTEGER NOT NULL,
        game_type TEXT NOT NULL,
        difficulty TEXT NOT NULL,
        current_turn INTEGER DEFAULT 0,
        current_prompt_id INTEGER,
        state TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
     """)
    
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS used_prompts (
    session_id INTEGER,
    prompt_id INTEGER,
    PRIMARY KEY (session_id, prompt_id)
    )
    """)

    # GAMES TABLE
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS games (
        code TEXT PRIMARY KEY,
        player1_id INTEGER NOT NULL,
        player2_id INTEGER,
        current_turn INTEGER,
        state TEXT DEFAULT 'WAITING',
        game_type TEXT,
        difficulty TEXT,
        current_prompt TEXT,
        selected_prompt TEXT,
        selected_type TEXT
    )
    """)

    _ensure_columns(cursor, "users", {
        "display_name": "TEXT",
    })
    _ensure_columns(cursor, "game_history", {
        "challenged_player_name": "TEXT",
        "selected_by": "INTEGER",
        "selected_by_name": "TEXT",
        "answerer_id": "INTEGER",
        "answerer_name": "TEXT",
        "answer_type": "TEXT",
        "answer_file_id": "TEXT",
        "answer_file_unique_id": "TEXT",
        "answer_media_kind": "TEXT",
        "answer_media_path": "TEXT",
        "answer_caption": "TEXT",
        "answered_at": "TIMESTAMP",
    })

    conn.commit()
    conn.close()

def _ensure_columns(cursor, table_name: str, columns: dict):
    cursor.execute(f"PRAGMA table_info({table_name})")
    existing = {row[1] for row in cursor.fetchall()}
    for column_name, column_type in columns.items():
        if column_name not in existing:
            cursor.execute(
                f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_type}"
            )


def create_or_update_user(
    telegram_id: int,
    username: str,
    display_name: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        INSERT INTO users (telegram_id, username, display_name)
        VALUES (?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
        username=excluded.username,
        display_name=COALESCE(excluded.display_name, users.display_name)
    """, (telegram_id, username, display_name))

    conn.commit()
    conn.close()

def generate_game_code(length=6):
    return ''.join(random.choices(string.ascii_uppercase + string.digits, k=length))


def user_in_active_game(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT * FROM games
        WHERE (player1_id = ? OR player2_id = ?)
        AND state != 'ENDED'
    """, (telegram_id, telegram_id))

    game = cursor.fetchone()
    conn.close()
    return game is not None


def create_game(player1_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    code = generate_game_code()

    # Ensure uniqueness
    cursor.execute("SELECT 1 FROM games WHERE code = ?", (code,))
    while cursor.fetchone():
        code = generate_game_code()
        cursor.execute("SELECT 1 FROM games WHERE code = ?", (code,))

    cursor.execute("""
        INSERT INTO games (code, player1_id, current_turn, state)
        VALUES (?, ?, ?, ?)
    """, (code, player1_id, player1_id, "WAITING_FOR_PLAYER"))

    conn.commit()
    conn.close()

    return code

def join_game(code: str, player2_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT player1_id, player2_id, state
        FROM games
        WHERE code = ?
    """, (code,))
    
    game = cursor.fetchone()

    if not game:
        conn.close()
        return "NOT_FOUND"

    player1_id, existing_player2, state = game

    if state != "WAITING_FOR_PLAYER":
        conn.close()
        return "NOT_AVAILABLE"

    if player1_id == player2_id:
        conn.close()
        return "CANNOT_JOIN_SELF"

    if existing_player2 is not None:
        conn.close()
        return "FULL"

    # Assign player2 and update state
    cursor.execute("""
        UPDATE games
        SET player2_id = ?, state = ?
        WHERE code = ?
    """, (player2_id, "CHOOSING_TYPE", code))

    conn.commit()
    conn.close()

    return "SUCCESS"

def end_game_by_user(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT code, player1_id, player2_id
        FROM games
        WHERE (player1_id = ? OR player2_id = ?)
        AND state != 'ENDED'
    """, (telegram_id, telegram_id))

    game = cursor.fetchone()

    if not game:
        conn.close()
        return None

    code, player1_id, player2_id = game

    cursor.execute("""
        UPDATE games
        SET state = 'ENDED'
        WHERE code = ?
    """, (code,))

    conn.commit()
    conn.close()

    return {
        "code": code,
        "player1_id": player1_id,
        "player2_id": player2_id
    }

def restart_game_by_user(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT code, player1_id, player2_id
        FROM games
        WHERE (player1_id = ? OR player2_id = ?)
        AND state != 'ENDED'
    """, (telegram_id, telegram_id))

    game = cursor.fetchone()

    if not game:
        conn.close()
        return None

    code, player1_id, player2_id = game

    cursor.execute("""
        UPDATE games
        SET state = 'CHOOSING_TYPE',
            current_turn = ?,
            game_type = NULL,
            difficulty = NULL,
            current_prompt = NULL,
            selected_prompt = NULL,
            selected_type = NULL
        WHERE code = ?
    """, (player1_id, code))

    conn.commit()
    conn.close()

    return {
        "code": code,
        "player1_id": player1_id,
        "player2_id": player2_id
    }

def get_full_game_state_by_user(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT code,
               player1_id,
               player2_id,
               current_turn,
               state,
               game_type,
               difficulty,
               current_prompt,
               selected_prompt,
               selected_type
        FROM games
        WHERE (player1_id = ? OR player2_id = ?)
        AND state != 'ENDED'
    """, (telegram_id, telegram_id))

    game = cursor.fetchone()
    conn.close()
    return game


def get_active_game_by_user(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT code, player1_id, player2_id, current_turn, state
        FROM games
        WHERE (player1_id = ? OR player2_id = ?)
        AND state != 'ENDED'
    """, (telegram_id, telegram_id))

    game = cursor.fetchone()
    conn.close()

    return game


def get_game_by_user(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT code, player1_id, player2_id, state
        FROM games
        WHERE player1_id = ? OR player2_id = ?
        ORDER BY rowid DESC
        LIMIT 1
    """, (telegram_id, telegram_id))
    game = cursor.fetchone()
    conn.close()
    return game


def get_user_display_name(telegram_id: int):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT display_name, username FROM users WHERE telegram_id = ?",
        (telegram_id,),
    )
    result = cursor.fetchone()
    conn.close()
    if not result:
        return str(telegram_id)
    return result[0] or result[1] or str(telegram_id)


def record_prompt(
    code: str,
    challenged_player: int,
    challenged_player_name: str,
    selected_by: int,
    selected_by_name: str,
    prompt_type: str,
    prompt_text: str,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO game_history (
            game_code,
            challenged_player,
            challenged_player_name,
            selected_by,
            selected_by_name,
            prompt_type,
            prompt_text
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        code,
        challenged_player,
        challenged_player_name,
        selected_by,
        selected_by_name,
        prompt_type,
        prompt_text,
    ))
    conn.commit()
    conn.close()


def record_answer(
    code: str,
    answerer_id: int,
    answerer_name: str,
    answer_type: str,
    answer_text: str | None = None,
    answer_file_id: str | None = None,
    answer_file_unique_id: str | None = None,
    answer_media_kind: str | None = None,
    answer_media_path: str | None = None,
    answer_caption: str | None = None,
):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE game_history
        SET answer_text = ?,
            answerer_id = ?,
            answerer_name = ?,
            answer_type = ?,
            answer_file_id = ?,
            answer_file_unique_id = ?,
            answer_media_kind = ?,
            answer_media_path = ?,
            answer_caption = ?,
            answered_at = CURRENT_TIMESTAMP
        WHERE id = (
            SELECT id FROM game_history
            WHERE game_code = ?
              AND challenged_player = ?
              AND answer_text IS NULL
              AND answer_file_id IS NULL
            ORDER BY id DESC
            LIMIT 1
        )
    """, (
        answer_text,
        answerer_id,
        answerer_name,
        answer_type,
        answer_file_id,
        answer_file_unique_id,
        answer_media_kind,
        answer_media_path,
        answer_caption,
        code,
        answerer_id,
    ))
    updated = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return updated


def get_transcript(code: str):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT challenged_player_name,
               challenged_player,
               selected_by_name,
               selected_by,
               prompt_type,
               prompt_text,
               answerer_name,
               answerer_id,
               answer_type,
               answer_text,
               answer_file_id,
               answer_caption,
               created_at,
               answered_at
        FROM game_history
        WHERE game_code = ?
        ORDER BY id
    """, (code,))
    rows = cursor.fetchall()
    conn.close()
    return rows


def seed_prompts():
    """
    Seeds the prompts table with all truth and dare questions if empty.
    Uses game_type, difficulty, and type fields.
    """
    conn = get_connection()
    cursor = conn.cursor()

    # Prevent duplicate seeding
    cursor.execute("SELECT COUNT(*) FROM prompts")
    if cursor.fetchone()[0] > 0:
        conn.close()
        return

    # FULL DATASET goes here (replace with your real questions)
    prompts = [
        # Example format for your real prompts:
        # ("truth", "easy", "friends", "Your question text here"),
        # ("dare", "spicy", "couples", "Your dare text here"),
        # ...
    ]

    cursor.executemany("""
        INSERT INTO prompts (type, difficulty, game_type, text)
        VALUES (?, ?, ?, ?)
    """, prompts)

    conn.commit()
    conn.close()


def get_random_prompts(prompt_type, difficulty, game_type):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT text
        FROM prompts
        WHERE type = ?
        AND difficulty = ?
        AND game_type = ?
        ORDER BY RANDOM()
        LIMIT 3
    """, (prompt_type, difficulty, game_type))

    rows = cursor.fetchall()
    conn.close()

    return [row[0] for row in rows]


def update_game_prompt_options(code: str, selected_type: str, prompts: list):
    conn = get_connection()
    cursor = conn.cursor()

    # Store prompts as a single string separated by ||
    prompts_str = "||".join(prompts)

    cursor.execute("""
        UPDATE games
        SET selected_type = ?, selected_prompt = ?
        WHERE code = ?
    """, (selected_type, prompts_str, code))

    conn.commit()
    conn.close()

def get_game_prompt_options(code: str):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        SELECT selected_prompt
        FROM games
        WHERE code = ?
    """, (code,))

    result = cursor.fetchone()
    conn.close()

    if not result or not result[0]:
        return []

    return result[0].split("||")


def switch_turn(code: str, new_turn: int):
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("""
        UPDATE games
        SET current_turn = ?, state = ?
        WHERE code = ?
    """, (new_turn, "CHOOSING_TYPE", code))

    conn.commit()
    conn.close()