import aiosqlite

from config import DB_PATH

_db: aiosqlite.Connection | None = None

# ---- In-memory admin cache ----
_admin_cache: set[int] = set()


def is_admin(user_id: int) -> bool:
    """Check if user is admin (synchronous, uses in-memory cache)."""
    return user_id in _admin_cache


def get_admin_ids() -> set[int]:
    """Get all admin user IDs (from in-memory cache)."""
    return _admin_cache.copy()


async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=ON")
    return _db


async def close_db():
    global _db
    if _db:
        await _db.close()
        _db = None


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS file_groups (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            description     TEXT DEFAULT '',
            uploaded_by     INTEGER NOT NULL,
            is_hidden       BOOLEAN DEFAULT 0,
            protect_content BOOLEAN DEFAULT 0,
            created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS files (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER NOT NULL REFERENCES file_groups(id) ON DELETE CASCADE,
            file_id     TEXT NOT NULL,
            file_type   TEXT NOT NULL,
            file_name   TEXT DEFAULT '',
            sort_order  INTEGER DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS codes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id    INTEGER NOT NULL REFERENCES file_groups(id) ON DELETE CASCADE,
            code        TEXT UNIQUE NOT NULL,
            max_uses    INTEGER NOT NULL DEFAULT 0,
            used_count  INTEGER NOT NULL DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS bot_channels (
            chat_id     INTEGER PRIMARY KEY,
            title       TEXT DEFAULT '',
            type        TEXT DEFAULT '',
            invite_link TEXT DEFAULT '',
            is_admin    BOOLEAN DEFAULT 1,
            updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS channels (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_id  INTEGER UNIQUE NOT NULL,
            channel_link TEXT NOT NULL,
            title       TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            first_name  TEXT DEFAULT '',
            is_blocked  BOOLEAN DEFAULT 0,
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS watermark_config (
            id          INTEGER PRIMARY KEY CHECK (id = 1),
            enabled     BOOLEAN DEFAULT 0,
            text        TEXT DEFAULT '',
            font_size   INTEGER DEFAULT 36,
            position    TEXT DEFAULT 'center',
            opacity     REAL DEFAULT 0.3,
            color       TEXT DEFAULT '#FFFFFF',
            rotation    INTEGER DEFAULT 0,
            font_path   TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS file_group_channels (
            group_id    INTEGER NOT NULL REFERENCES file_groups(id) ON DELETE CASCADE,
            channel_id  INTEGER NOT NULL,
            channel_link TEXT DEFAULT '',
            title       TEXT DEFAULT '',
            PRIMARY KEY (group_id, channel_id)
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT DEFAULT '',
            created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE INDEX IF NOT EXISTS idx_files_group_id ON files(group_id);
        CREATE INDEX IF NOT EXISTS idx_codes_group_id ON codes(group_id);

        INSERT OR IGNORE INTO watermark_config (id) VALUES (1);
    """)
    # Migrations for existing databases
    try:
        await db.execute("SELECT is_hidden FROM file_groups LIMIT 1")
    except Exception:
        await db.execute("ALTER TABLE file_groups ADD COLUMN is_hidden BOOLEAN DEFAULT 0")
    try:
        await db.execute("SELECT protect_content FROM file_groups LIMIT 1")
    except Exception:
        await db.execute("ALTER TABLE file_groups ADD COLUMN protect_content BOOLEAN DEFAULT 0")
    await db.commit()
    await _load_admin_cache()


# ---- users ----

async def upsert_user(user_id: int, username: str = "", first_name: str = ""):
    db = await get_db()
    await db.execute(
        """INSERT INTO users (user_id, username, first_name)
           VALUES (?, ?, ?)
           ON CONFLICT(user_id) DO UPDATE SET username=excluded.username, first_name=excluded.first_name""",
        (user_id, username or "", first_name or ""),
    )
    await db.commit()


async def get_all_active_users() -> list[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM users WHERE is_blocked = 0")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def mark_user_blocked(user_id: int):
    db = await get_db()
    await db.execute("UPDATE users SET is_blocked = 1 WHERE user_id = ?", (user_id,))
    await db.commit()


async def get_user_count() -> tuple[int, int]:
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM users")
    total = (await cur.fetchone())[0]
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 0")
    active = (await cur.fetchone())[0]
    return total, active


# ---- file groups ----

async def create_file_group(uploaded_by: int, description: str = "") -> int:
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO file_groups (uploaded_by, description) VALUES (?, ?)",
        (uploaded_by, description),
    )
    await db.commit()
    return cursor.lastrowid


async def add_file_to_group(group_id: int, file_id: str, file_type: str, file_name: str, sort_order: int):
    db = await get_db()
    await db.execute(
        "INSERT INTO files (group_id, file_id, file_type, file_name, sort_order) VALUES (?, ?, ?, ?, ?)",
        (group_id, file_id, file_type, file_name or "", sort_order),
    )
    await db.commit()


async def get_file_group(group_id: int) -> dict | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM file_groups WHERE id = ?", (group_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_files_by_group(group_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM files WHERE group_id = ? ORDER BY sort_order", (group_id,)
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def delete_file_group(group_id: int):
    db = await get_db()
    await db.execute("DELETE FROM file_groups WHERE id = ?", (group_id,))
    await db.commit()


async def update_file_group_description(group_id: int, description: str):
    db = await get_db()
    await db.execute("UPDATE file_groups SET description = ? WHERE id = ?", (description, group_id))
    await db.commit()


async def get_file_groups_page(page: int, page_size: int, include_hidden: bool = False) -> tuple[list[dict], int]:
    db = await get_db()
    where = "" if include_hidden else "WHERE fg.is_hidden = 0"
    count_where = "" if include_hidden else "WHERE is_hidden = 0"
    cur = await db.execute(f"SELECT COUNT(*) FROM file_groups {count_where}")
    total = (await cur.fetchone())[0]
    cur = await db.execute(
        f"""SELECT fg.*,
                  COALESCE(fc.file_count, 0) AS file_count,
                  COALESCE(cc.code_count, 0) AS code_count
           FROM file_groups fg
           LEFT JOIN (SELECT group_id, COUNT(*) AS file_count FROM files GROUP BY group_id) fc
             ON fc.group_id = fg.id
           LEFT JOIN (SELECT group_id, COUNT(*) AS code_count FROM codes GROUP BY group_id) cc
             ON cc.group_id = fg.id
           {where}
           ORDER BY fg.created_at DESC LIMIT ? OFFSET ?""",
        (page_size, page * page_size),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows], total


async def get_file_group_count() -> int:
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM file_groups")
    return (await cur.fetchone())[0]


async def toggle_file_group_hidden(group_id: int) -> bool:
    """Toggle is_hidden flag. Returns the new value."""
    conn = await get_db()
    await conn.execute(
        "UPDATE file_groups SET is_hidden = NOT is_hidden WHERE id = ?", (group_id,)
    )
    await conn.commit()
    cur = await conn.execute("SELECT is_hidden FROM file_groups WHERE id = ?", (group_id,))
    row = await cur.fetchone()
    return bool(row[0]) if row else False


async def toggle_file_group_protect(group_id: int) -> bool:
    """Toggle protect_content flag. Returns the new value."""
    conn = await get_db()
    await conn.execute(
        "UPDATE file_groups SET protect_content = NOT protect_content WHERE id = ?", (group_id,)
    )
    await conn.commit()
    cur = await conn.execute("SELECT protect_content FROM file_groups WHERE id = ?", (group_id,))
    row = await cur.fetchone()
    return bool(row[0]) if row else False


# ---- codes ----

async def create_code(group_id: int, code: str, max_uses: int) -> int:
    """Create a new extraction code. max_uses=0 means unlimited."""
    db = await get_db()
    cursor = await db.execute(
        "INSERT INTO codes (group_id, code, max_uses) VALUES (?, ?, ?)",
        (group_id, code, max_uses),
    )
    await db.commit()
    return cursor.lastrowid


async def get_code_by_code(code: str) -> dict | None:
    db = await get_db()
    cur = await db.execute("SELECT * FROM codes WHERE code = ?", (code,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def increment_code_usage(code_id: int) -> bool:
    """Atomically increment usage. Returns False if code is already exhausted."""
    db = await get_db()
    cursor = await db.execute(
        "UPDATE codes SET used_count = used_count + 1 WHERE id = ? AND (max_uses = 0 OR used_count < max_uses)",
        (code_id,),
    )
    await db.commit()
    return cursor.rowcount > 0


async def is_code_valid(code: str) -> tuple[bool, dict | None]:
    """Check if code exists and still has remaining uses. Returns (valid, code_row)."""
    row = await get_code_by_code(code)
    if not row:
        return False, None
    # max_uses=0 means unlimited
    if row["max_uses"] > 0 and row["used_count"] >= row["max_uses"]:
        return False, row
    return True, row


async def get_codes_by_group(group_id: int) -> list[dict]:
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM codes WHERE group_id = ? ORDER BY created_at DESC", (group_id,)
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_codes_by_group_page(group_id: int, page: int, page_size: int) -> tuple[list[dict], int]:
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) FROM codes WHERE group_id = ?", (group_id,))
    total = (await cur.fetchone())[0]
    cur = await db.execute(
        "SELECT * FROM codes WHERE group_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (group_id, page_size, page * page_size),
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows], total


async def delete_code(code_id: int):
    db = await get_db()
    await db.execute("DELETE FROM codes WHERE id = ?", (code_id,))
    await db.commit()


async def code_exists(code: str) -> bool:
    db = await get_db()
    cur = await db.execute("SELECT 1 FROM codes WHERE code = ?", (code,))
    return await cur.fetchone() is not None


# ---- bot_channels (auto-tracked) ----

async def upsert_bot_channel(chat_id: int, title: str, chat_type: str, invite_link: str = ""):
    db = await get_db()
    await db.execute(
        """INSERT INTO bot_channels (chat_id, title, type, invite_link, is_admin, updated_at)
           VALUES (?, ?, ?, ?, 1, CURRENT_TIMESTAMP)
           ON CONFLICT(chat_id) DO UPDATE SET
             title=excluded.title, type=excluded.type,
             invite_link=CASE WHEN excluded.invite_link != '' THEN excluded.invite_link ELSE bot_channels.invite_link END,
             is_admin=1, updated_at=CURRENT_TIMESTAMP""",
        (chat_id, title or "", chat_type or "", invite_link or ""),
    )
    await db.commit()


async def remove_bot_channel(chat_id: int):
    db = await get_db()
    await db.execute("UPDATE bot_channels SET is_admin = 0 WHERE chat_id = ?", (chat_id,))
    await db.commit()


async def get_bot_admin_channels() -> list[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM bot_channels WHERE is_admin = 1")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---- channels (prerequisite) ----

async def add_prerequisite_channel(channel_id: int, channel_link: str, title: str):
    db = await get_db()
    await db.execute(
        """INSERT INTO channels (channel_id, channel_link, title)
           VALUES (?, ?, ?)
           ON CONFLICT(channel_id) DO UPDATE SET channel_link=excluded.channel_link, title=excluded.title""",
        (channel_id, channel_link, title),
    )
    await db.commit()


async def remove_prerequisite_channel(channel_id: int):
    db = await get_db()
    await db.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
    await db.commit()


async def get_prerequisite_channels() -> list[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM channels")
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


# ---- watermark config ----

async def get_watermark_config() -> dict:
    db = await get_db()
    cur = await db.execute("SELECT * FROM watermark_config WHERE id = 1")
    row = await cur.fetchone()
    return dict(row) if row else {
        "enabled": False, "text": "", "font_size": 36,
        "position": "center", "opacity": 0.3, "color": "#FFFFFF",
        "rotation": 0, "font_path": "",
    }


async def update_watermark_config(**kwargs):
    db = await get_db()
    allowed = {"enabled", "text", "font_size", "position", "opacity", "color", "rotation", "font_path"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    set_clause = ", ".join(f"{k} = ?" for k in fields)
    values = list(fields.values())
    await db.execute(f"UPDATE watermark_config SET {set_clause} WHERE id = 1", values)
    await db.commit()


# ---- admins ----

async def _load_admin_cache():
    """Load admin IDs from database into memory cache."""
    global _admin_cache
    conn = await get_db()
    cur = await conn.execute("SELECT user_id FROM admins")
    rows = await cur.fetchall()
    _admin_cache = {row[0] for row in rows}


async def add_admin(user_id: int, username: str = "") -> bool:
    """Add a new admin. Returns True if newly added."""
    conn = await get_db()
    cur = await conn.execute("SELECT 1 FROM admins WHERE user_id = ?", (user_id,))
    if await cur.fetchone():
        return False
    await conn.execute(
        "INSERT INTO admins (user_id, username) VALUES (?, ?)",
        (user_id, username),
    )
    await conn.commit()
    _admin_cache.add(user_id)
    return True


async def remove_admin(user_id: int):
    conn = await get_db()
    await conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
    await conn.commit()
    _admin_cache.discard(user_id)


# ---- file group channels (per-group prerequisite) ----

async def get_file_group_channels(group_id: int) -> list[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM file_group_channels WHERE group_id = ?", (group_id,)
    )
    rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def add_file_group_channel(group_id: int, channel_id: int, channel_link: str, title: str):
    conn = await get_db()
    await conn.execute(
        """INSERT OR IGNORE INTO file_group_channels (group_id, channel_id, channel_link, title)
           VALUES (?, ?, ?, ?)""",
        (group_id, channel_id, channel_link, title),
    )
    await conn.commit()


async def remove_file_group_channel(group_id: int, channel_id: int):
    conn = await get_db()
    await conn.execute(
        "DELETE FROM file_group_channels WHERE group_id = ? AND channel_id = ?",
        (group_id, channel_id),
    )
    await conn.commit()


async def remove_file_group_channel_all(channel_id: int):
    """Remove a channel from ALL file groups (used when unbinding)."""
    conn = await get_db()
    await conn.execute(
        "DELETE FROM file_group_channels WHERE channel_id = ?", (channel_id,)
    )
    await conn.commit()
