import os
import re
import socket
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
import bcrypt
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "ao4.db")


def _hash_pw(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def _verify_pw(password: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), hashed.encode())
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS fics (
            id          TEXT PRIMARY KEY,
            title       TEXT NOT NULL,
            author      TEXT NOT NULL,
            summary     TEXT DEFAULT '',
            rating      TEXT DEFAULT 'G',
            word_count  INTEGER DEFAULT 0,
            created_at  TEXT NOT NULL,
            updated_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS chapters (
            id      TEXT PRIMARY KEY,
            fic_id  TEXT NOT NULL REFERENCES fics(id) ON DELETE CASCADE,
            number  INTEGER NOT NULL,
            title   TEXT DEFAULT '',
            content TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS tags (
            id       TEXT PRIMARY KEY,
            name     TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'freeform',
            UNIQUE(name, category)
        );

        CREATE TABLE IF NOT EXISTS fic_tags (
            fic_id TEXT NOT NULL REFERENCES fics(id) ON DELETE CASCADE,
            tag_id TEXT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY(fic_id, tag_id)
        );

        CREATE TABLE IF NOT EXISTS users (
            id            TEXT PRIMARY KEY,
            username      TEXT NOT NULL UNIQUE,
            email         TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            is_admin      INTEGER NOT NULL DEFAULT 0,
            created_at    TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS site_settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS sessions (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );
    """)
    # Migrations for existing databases
    for migration in [
        "ALTER TABLE fics ADD COLUMN author_id TEXT REFERENCES users(id)",
        "ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0",
    ]:
        try:
            conn.execute(migration)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    conn.close()
    _ensure_admin()


def _ensure_admin():
    """Create/promote the admin account on startup using ADMIN_* env vars."""
    username = os.environ.get("ADMIN_USERNAME", "")
    password = os.environ.get("ADMIN_PASSWORD", "")
    email    = os.environ.get("ADMIN_EMAIL", "admin@ao4.local")
    if not username or not password:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, is_admin FROM users WHERE LOWER(username) = LOWER(?)", (username,)
    ).fetchone()
    if row:
        if not row["is_admin"]:
            conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (row["id"],))
            conn.commit()
    else:
        uid = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        conn.execute(
            "INSERT INTO users(id, username, email, password_hash, is_admin, created_at) "
            "VALUES(?,?,?,?,1,?)",
            (uid, username, email, _hash_pw(password), now),
        )
        conn.commit()
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AO4",
    description="Archive of Our Four — fanfiction API",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ChapterIn(BaseModel):
    title: str = ""
    content: str


class FicIn(BaseModel):
    title: str
    author: str = ""
    summary: str = ""
    rating: str = "G"
    fandoms: List[str] = []
    characters: List[str] = []
    relationships: List[str] = []
    tags: List[str] = []
    chapters: List[ChapterIn]


class ChapterOut(BaseModel):
    id: str
    number: int
    title: str
    content: str


class FicOut(BaseModel):
    id: str
    title: str
    author: str
    author_id: Optional[str] = None
    summary: str
    rating: str
    word_count: int
    fandoms: List[str]
    characters: List[str]
    relationships: List[str]
    tags: List[str]
    chapter_count: int
    created_at: str
    updated_at: str


class FicDetail(FicOut):
    chapters: List[ChapterOut]


class ChapterAddIn(BaseModel):
    title: str = ""
    content: str


class RegisterIn(BaseModel):
    username: str
    email: str
    password: str


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: str
    username: str
    email: str
    is_admin: bool = False
    created_at: str


class AuthOut(BaseModel):
    token: str
    user: UserOut


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _get_current_user(authorization: Optional[str] = Header(None), conn=Depends(get_db)):
    """Returns the logged-in user dict, or None if not authenticated."""
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[7:]
    now = datetime.utcnow().isoformat()
    row = conn.execute(
        "SELECT u.id, u.username, u.email, u.is_admin, u.created_at FROM sessions s "
        "JOIN users u ON u.id = s.user_id "
        "WHERE s.token = ? AND s.expires_at > ?",
        (token, now),
    ).fetchone()
    return dict(row) if row else None


def require_user(user=Depends(_get_current_user)):
    if not user:
        raise HTTPException(401, "Authentication required")
    return user


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def _get_fic_tags(conn, fic_id: str, category: str) -> List[str]:
    rows = conn.execute(
        "SELECT t.name FROM tags t "
        "JOIN fic_tags ft ON ft.tag_id = t.id "
        "WHERE ft.fic_id = ? AND t.category = ? ORDER BY t.name",
        (fic_id, category),
    ).fetchall()
    return [r["name"] for r in rows]


def _fic_to_dict(conn, row) -> dict:
    fid = row["id"]
    keys = row.keys()
    chapter_count = conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE fic_id = ?", (fid,)
    ).fetchone()[0]
    return dict(
        id=fid,
        title=row["title"],
        author=row["author"],
        author_id=row["author_id"] if "author_id" in keys else None,
        summary=row["summary"],
        rating=row["rating"],
        word_count=row["word_count"],
        fandoms=_get_fic_tags(conn, fid, "fandom"),
        characters=_get_fic_tags(conn, fid, "character"),
        relationships=_get_fic_tags(conn, fid, "relationship"),
        tags=_get_fic_tags(conn, fid, "freeform"),
        chapter_count=chapter_count,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _upsert_tag(conn, name: str, category: str) -> str:
    row = conn.execute(
        "SELECT id FROM tags WHERE name = ? AND category = ?", (name, category)
    ).fetchone()
    if row:
        return row["id"]
    tid = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO tags(id, name, category) VALUES(?,?,?)", (tid, name, category)
    )
    return tid


def _attach_tags(conn, fic_id: str, names: List[str], category: str):
    for raw in names:
        name = raw.strip()
        if not name:
            continue
        tid = _upsert_tag(conn, name, category)
        conn.execute(
            "INSERT OR IGNORE INTO fic_tags(fic_id, tag_id) VALUES(?,?)", (fic_id, tid)
        )


def _safe_filename(title: str) -> str:
    return re.sub(r"[^\w\s-]", "", title).strip().replace(" ", "_") or "fic"


# ---------------------------------------------------------------------------
# Routes — auth
# ---------------------------------------------------------------------------

@app.post("/auth/register", response_model=AuthOut, status_code=201, summary="Create a new account")
def register(data: RegisterIn, conn=Depends(get_db)):
    if len(data.username) < 2 or len(data.username) > 50:
        raise HTTPException(400, "Username must be 2–50 characters")
    if not re.match(r'^[a-zA-Z0-9_-]+$', data.username):
        raise HTTPException(400, "Username may only contain letters, numbers, _ and -")
    if len(data.password) < 6:
        raise HTTPException(400, "Password must be at least 6 characters")
    if "@" not in data.email or "." not in data.email.split("@")[-1]:
        raise HTTPException(400, "Invalid email address")

    if conn.execute("SELECT id FROM users WHERE LOWER(username) = LOWER(?)", (data.username,)).fetchone():
        raise HTTPException(409, "Username already taken")
    if conn.execute("SELECT id FROM users WHERE LOWER(email) = LOWER(?)", (data.email,)).fetchone():
        raise HTTPException(409, "Email already registered")

    user_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    pw_hash = _hash_pw(data.password)

    conn.execute(
        "INSERT INTO users(id, username, email, password_hash, created_at) VALUES(?,?,?,?,?)",
        (user_id, data.username, data.email, pw_hash, now),
    )
    token = str(uuid.uuid4())
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
        (token, user_id, now, expires),
    )
    return {
        "token": token,
        "user": {"id": user_id, "username": data.username, "email": data.email, "is_admin": False, "created_at": now},
    }


@app.post("/auth/login", response_model=AuthOut, summary="Log in")
def login(data: LoginIn, conn=Depends(get_db)):
    row = conn.execute(
        "SELECT * FROM users WHERE LOWER(username) = LOWER(?)", (data.username,)
    ).fetchone()
    if not row or not _verify_pw(data.password, row["password_hash"]):
        raise HTTPException(401, "Invalid username or password")

    token = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    expires = (datetime.utcnow() + timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO sessions(token, user_id, created_at, expires_at) VALUES(?,?,?,?)",
        (token, row["id"], now, expires),
    )
    return {
        "token": token,
        "user": {"id": row["id"], "username": row["username"], "email": row["email"], "is_admin": bool(row["is_admin"]), "created_at": row["created_at"]},
    }


@app.get("/auth/me", response_model=UserOut, summary="Get current user")
def me(user=Depends(require_user)):
    return user


@app.post("/auth/logout", status_code=204, summary="Log out")
def logout(authorization: Optional[str] = Header(None), conn=Depends(get_db)):
    if authorization and authorization.startswith("Bearer "):
        conn.execute("DELETE FROM sessions WHERE token = ?", (authorization[7:],))


# ---------------------------------------------------------------------------
# Routes — fics
# ---------------------------------------------------------------------------

@app.post("/fics", response_model=FicOut, status_code=201, summary="Upload a fic")
def upload_fic(fic: FicIn, conn=Depends(get_db), user=Depends(_get_current_user)):
    if not fic.chapters:
        raise HTTPException(400, "At least one chapter is required")
    if fic.rating not in ("G", "T", "M", "E"):
        raise HTTPException(400, "Rating must be G, T, M, or E")

    author_id = user["id"] if user else None
    author = user["username"] if user else fic.author.strip()
    if not author:
        raise HTTPException(400, "Author name is required when not logged in")

    fic_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    wc = sum(_word_count(ch.content) for ch in fic.chapters)

    conn.execute(
        "INSERT INTO fics(id,title,author,author_id,summary,rating,word_count,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)",
        (fic_id, fic.title, author, author_id, fic.summary, fic.rating, wc, now, now),
    )
    for i, ch in enumerate(fic.chapters, 1):
        conn.execute(
            "INSERT INTO chapters(id,fic_id,number,title,content) VALUES(?,?,?,?,?)",
            (str(uuid.uuid4()), fic_id, i, ch.title, ch.content),
        )
    _attach_tags(conn, fic_id, fic.fandoms, "fandom")
    _attach_tags(conn, fic_id, fic.characters, "character")
    _attach_tags(conn, fic_id, fic.relationships, "relationship")
    _attach_tags(conn, fic_id, fic.tags, "freeform")

    row = conn.execute("SELECT * FROM fics WHERE id = ?", (fic_id,)).fetchone()
    return _fic_to_dict(conn, row)


@app.get("/fics", response_model=List[FicOut], summary="List / search fics")
def list_fics(
    q: Optional[str] = Query(None, description="Search title or author"),
    fandom: Optional[str] = Query(None, description="Filter by fandom (partial match)"),
    rating: Optional[str] = Query(None, description="Filter by rating: G T M E"),
    limit: int = Query(20, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    conn=Depends(get_db),
):
    sql = "SELECT DISTINCT f.* FROM fics f"
    joins: List[str] = []
    wheres: List[str] = []
    params: list = []

    if fandom:
        joins.append(
            "JOIN fic_tags ft ON ft.fic_id = f.id "
            "JOIN tags t ON t.id = ft.tag_id"
        )
        wheres.append("(t.name LIKE ? AND t.category = 'fandom')")
        params.append(f"%{fandom}%")
    if q:
        wheres.append("(f.title LIKE ? OR f.author LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    if rating:
        wheres.append("f.rating = ?")
        params.append(rating)

    if joins:
        sql += " " + " ".join(joins)
    if wheres:
        sql += " WHERE " + " AND ".join(wheres)
    sql += " ORDER BY f.created_at DESC LIMIT ? OFFSET ?"
    params += [limit, offset]

    rows = conn.execute(sql, params).fetchall()
    return [_fic_to_dict(conn, r) for r in rows]


@app.get("/fics/{fic_id}", response_model=FicDetail, summary="Get a fic with all chapters")
def get_fic(fic_id: str, conn=Depends(get_db)):
    row = conn.execute("SELECT * FROM fics WHERE id = ?", (fic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fic not found")

    base = _fic_to_dict(conn, row)
    chapter_rows = conn.execute(
        "SELECT * FROM chapters WHERE fic_id = ? ORDER BY number", (fic_id,)
    ).fetchall()
    base["chapters"] = [
        dict(id=r["id"], number=r["number"], title=r["title"], content=r["content"])
        for r in chapter_rows
    ]
    return base


@app.delete("/fics/{fic_id}", status_code=204, summary="Delete a fic")
def delete_fic(fic_id: str, conn=Depends(get_db), user=Depends(_get_current_user)):
    row = conn.execute("SELECT author_id FROM fics WHERE id = ?", (fic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fic not found")
    # Admin can delete anything; otherwise only the owner can delete account-posted works
    is_admin = user and user.get("is_admin")
    if row["author_id"] and not is_admin and (not user or user["id"] != row["author_id"]):
        raise HTTPException(403, "You can only delete your own works")
    conn.execute("DELETE FROM fics WHERE id = ?", (fic_id,))


# ---------------------------------------------------------------------------
# Routes — chapters
# ---------------------------------------------------------------------------

@app.post(
    "/fics/{fic_id}/chapters",
    response_model=FicDetail,
    status_code=201,
    summary="Append a chapter to an existing fic",
)
def add_chapter(fic_id: str, chapter: ChapterAddIn, conn=Depends(get_db), user=Depends(_get_current_user)):
    row = conn.execute("SELECT * FROM fics WHERE id = ?", (fic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fic not found")
    is_admin = user and user.get("is_admin")
    if row["author_id"] and not is_admin and (not user or user["id"] != row["author_id"]):
        raise HTTPException(403, "You can only add chapters to your own works")

    next_num = conn.execute(
        "SELECT COALESCE(MAX(number), 0) + 1 FROM chapters WHERE fic_id = ?", (fic_id,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO chapters(id,fic_id,number,title,content) VALUES(?,?,?,?,?)",
        (str(uuid.uuid4()), fic_id, next_num, chapter.title, chapter.content),
    )
    new_wc = conn.execute(
        "SELECT SUM(LENGTH(content) - LENGTH(REPLACE(content,' ','')) + 1) FROM chapters WHERE fic_id = ?",
        (fic_id,),
    ).fetchone()[0] or 0
    now = datetime.utcnow().isoformat()
    conn.execute(
        "UPDATE fics SET word_count = ?, updated_at = ? WHERE id = ?",
        (new_wc, now, fic_id),
    )

    row = conn.execute("SELECT * FROM fics WHERE id = ?", (fic_id,)).fetchone()
    base = _fic_to_dict(conn, row)
    chapter_rows = conn.execute(
        "SELECT * FROM chapters WHERE fic_id = ? ORDER BY number", (fic_id,)
    ).fetchall()
    base["chapters"] = [
        dict(id=r["id"], number=r["number"], title=r["title"], content=r["content"])
        for r in chapter_rows
    ]
    return base


# ---------------------------------------------------------------------------
# Routes — download
# ---------------------------------------------------------------------------

@app.get("/fics/{fic_id}/download", summary="Download a fic as .txt or .html")
def download_fic(
    fic_id: str,
    fmt: str = Query("txt", alias="format", description="txt or html"),
    conn=Depends(get_db),
):
    if fmt not in ("txt", "html"):
        raise HTTPException(400, "format must be txt or html")

    row = conn.execute("SELECT * FROM fics WHERE id = ?", (fic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fic not found")

    chapters = conn.execute(
        "SELECT * FROM chapters WHERE fic_id = ? ORDER BY number", (fic_id,)
    ).fetchall()
    fandoms = _get_fic_tags(conn, fic_id, "fandom")
    tags = _get_fic_tags(conn, fic_id, "freeform")
    characters = _get_fic_tags(conn, fic_id, "character")
    relationships = _get_fic_tags(conn, fic_id, "relationship")
    safe_name = _safe_filename(row["title"])

    if fmt == "txt":
        lines = [
            row["title"],
            f"by {row['author']}",
            f"Fandom: {', '.join(fandoms) or 'Original'}",
            f"Rating: {row['rating']}  |  Words: {row['word_count']:,}",
        ]
        if relationships:
            lines.append(f"Relationships: {', '.join(relationships)}")
        if characters:
            lines.append(f"Characters: {', '.join(characters)}")
        if tags:
            lines.append(f"Tags: {', '.join(tags)}")
        lines += [f"Summary: {row['summary']}", "=" * 60, ""]

        for ch in chapters:
            if len(chapters) > 1 or ch["title"]:
                header = f"Chapter {ch['number']}"
                if ch["title"]:
                    header += f": {ch['title']}"
                lines += [header, ""]
            lines += [ch["content"], ""]

        return PlainTextResponse(
            "\n".join(lines),
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.txt"'},
        )

    fandom_str = ", ".join(fandoms) or "Original"
    meta_bits = [f"by <strong>{row['author']}</strong>", fandom_str, f"Rated {row['rating']}", f"{row['word_count']:,} words"]
    if relationships:
        meta_bits.append(" | ".join(relationships))

    body_html = ""
    for ch in chapters:
        if len(chapters) > 1 or ch["title"]:
            heading = f"Chapter {ch['number']}"
            if ch["title"]:
                heading += f": {ch['title']}"
            body_html += f"<h2>{heading}</h2>\n"
        paras = "".join(
            f"<p>{p.strip()}</p>\n"
            for p in re.split(r"\n{2,}", ch["content"])
            if p.strip()
        )
        body_html += paras

    tag_pills = "".join(f'<span class="tag">{t}</span>' for t in tags) if tags else ""

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{row['title']}</title>
<style>
  *,*::before,*::after {{ box-sizing: border-box; }}
  body {{
    font-family: Georgia, 'Times New Roman', serif;
    max-width: 760px; margin: 2.5rem auto; padding: 0 1.5rem;
    line-height: 1.8; color: #1a1a1a; background: #fdfaf6;
  }}
  h1 {{ font-size: 2rem; margin-bottom: .2rem; }}
  .meta {{ color: #555; font-size: .9rem; margin-bottom: .75rem; }}
  .summary {{
    background: #f3ede5; border-left: 4px solid #8b0000;
    padding: .9rem 1.2rem; margin-bottom: 1.5rem;
    font-style: italic; color: #333;
  }}
  .tags {{ margin-bottom: 1.5rem; }}
  .tag {{
    display: inline-block; background: #e8e0d8; border-radius: 3px;
    padding: .15rem .5rem; font-size: .8rem; margin: .2rem .15rem; font-family: sans-serif;
  }}
  h2 {{ margin-top: 2.5rem; border-top: 1px solid #d0c8be; padding-top: 1rem; font-size: 1.3rem; }}
  p {{ margin: 0 0 1rem; }}
  .footer {{ margin-top: 3rem; font-size: .8rem; color: #aaa; font-family: sans-serif; border-top: 1px solid #e0d8d0; padding-top: .75rem; }}
</style>
</head>
<body>
<h1>{row['title']}</h1>
<div class="meta">{" &bull; ".join(meta_bits)}</div>
<div class="summary">{row['summary'] or '<em>No summary provided.</em>'}</div>
{f'<div class="tags">{tag_pills}</div>' if tag_pills else ''}
{body_html}
<div class="footer">Downloaded from AO4 &bull; {row['created_at'][:10]}</div>
</body>
</html>"""

    return HTMLResponse(
        html,
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.html"'},
    )


# ---------------------------------------------------------------------------
# Routes — stats + tags
# ---------------------------------------------------------------------------

@app.get("/stats", summary="Archive-wide statistics")
def get_stats(conn=Depends(get_db)):
    total_fics    = conn.execute("SELECT COUNT(*) FROM fics").fetchone()[0]
    total_words   = conn.execute("SELECT COALESCE(SUM(word_count), 0) FROM fics").fetchone()[0]
    total_authors = conn.execute("SELECT COUNT(DISTINCT author) FROM fics").fetchone()[0]
    total_fandoms = conn.execute("SELECT COUNT(*) FROM tags WHERE category = 'fandom'").fetchone()[0]
    total_users   = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {
        "fics":    total_fics,
        "words":   total_words,
        "authors": total_authors,
        "fandoms": total_fandoms,
        "users":   total_users,
    }


@app.get("/admin/settings", summary="Get site settings (admin only)")
def get_settings(user=Depends(require_user), conn=Depends(get_db)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    rows = conn.execute("SELECT key, value FROM site_settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


@app.patch("/admin/settings", summary="Update site settings (admin only)")
def update_settings(payload: dict, user=Depends(require_user), conn=Depends(get_db)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    for key, value in payload.items():
        conn.execute(
            "INSERT INTO site_settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )
    return {"ok": True}


@app.get("/admin/users", summary="List all users (admin only)")
def list_users(user=Depends(require_user), conn=Depends(get_db)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    rows = conn.execute(
        "SELECT id, username, email, is_admin, created_at FROM users ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


@app.delete("/admin/users/{user_id}", status_code=204, summary="Delete a user (admin only)")
def delete_user(user_id: str, user=Depends(require_user), conn=Depends(get_db)):
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    if user_id == user["id"]:
        raise HTTPException(400, "Cannot delete your own account")
    result = conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    if result.rowcount == 0:
        raise HTTPException(404, "User not found")


@app.get("/tags", summary="List all tags (useful for autocomplete)")
def list_tags(
    category: Optional[str] = Query(
        None, description="fandom | character | relationship | freeform"
    ),
    conn=Depends(get_db),
):
    valid_categories = ("fandom", "character", "relationship", "freeform")
    if category and category not in valid_categories:
        raise HTTPException(400, f"category must be one of {valid_categories}")

    if category:
        rows = conn.execute(
            "SELECT name, category FROM tags WHERE category = ? ORDER BY name",
            (category,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name, category FROM tags ORDER BY category, name"
        ).fetchall()
    return [{"name": r["name"], "category": r["category"]} for r in rows]


@app.get("/", include_in_schema=False)
def root():
    return {"status": "ok", "docs": "/docs"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    host = "0.0.0.0"
    port = int(os.environ.get("PORT", 8000))
    reload = os.environ.get("RELOAD", "true").lower() == "true"
    try:
        lan_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        lan_ip = "unknown"
    print(f"\n  AO4 running")
    print(f"  Local:   http://localhost:{port}")
    print(f"  Network: http://{lan_ip}:{port}")
    print(f"  Docs:    http://{lan_ip}:{port}/docs\n")
    uvicorn.run("main:app", host=host, port=port, reload=reload)
