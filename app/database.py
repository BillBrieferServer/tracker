import aiosqlite
import os

DB_PATH = os.environ.get("DB_PATH", "/app/data/tracker.db")


async def get_db():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    return db


async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            calorie_target INTEGER NOT NULL DEFAULT 2000,
            protein_target INTEGER NOT NULL DEFAULT 100
        );

        CREATE TABLE IF NOT EXISTS foods (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            calories INTEGER NOT NULL,
            protein_g REAL NOT NULL,
            serving_description TEXT DEFAULT '',
            source TEXT NOT NULL DEFAULT 'manual' CHECK(source IN ('manual','claude')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            food_id INTEGER REFERENCES foods(id) ON DELETE SET NULL,
            food_name TEXT NOT NULL,
            calories INTEGER NOT NULL,
            protein_g REAL NOT NULL,
            servings REAL NOT NULL DEFAULT 1.0,
            logged_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            log_date DATE NOT NULL DEFAULT (DATE('now','localtime'))
        );

        CREATE INDEX IF NOT EXISTS idx_foods_name ON foods(name COLLATE NOCASE);
        CREATE INDEX IF NOT EXISTS idx_entries_user_date ON entries(user_id, log_date);
    """)

    cur = await db.execute("SELECT COUNT(*) AS n FROM users")
    row = await cur.fetchone()
    if row["n"] == 0:
        await db.execute(
            "INSERT INTO users (name, calorie_target, protein_target) VALUES (?,?,?)",
            ("Steve", 2000, 140),
        )
        await db.execute(
            "INSERT INTO users (name, calorie_target, protein_target) VALUES (?,?,?)",
            ("Julie", 1600, 100),
        )

    await db.commit()
    await db.close()
