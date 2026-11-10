import os
import re
import socket
import sqlite3
import uuid
from datetime import datetime
from typing import List, Optional

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel

DB_PATH = os.environ.get("DB_PATH", "ao4.db")


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
    """)
    conn.close()


init_db()

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="AO4",
    description="Archive of Our Four — fanfiction API",
    version="0.1.0",
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
    author: str
    summary: str = ""
    rating: str = "G"          # G  T  M  E
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
    chapter_count = conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE fic_id = ?", (fid,)
    ).fetchone()[0]
    return dict(
        id=fid,
        title=row["title"],
        author=row["author"],
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
# Routes — fics
# ---------------------------------------------------------------------------

@app.post("/fics", response_model=FicOut, status_code=201, summary="Upload a fic")
def upload_fic(fic: FicIn, conn=Depends(get_db)):
    """Upload a new fic with one or more chapters."""
    if not fic.chapters:
        raise HTTPException(400, "At least one chapter is required")
    if fic.rating not in ("G", "T", "M", "E"):
        raise HTTPException(400, "Rating must be G, T, M, or E")

    fic_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    wc = sum(_word_count(ch.content) for ch in fic.chapters)

    conn.execute(
        "INSERT INTO fics(id,title,author,summary,rating,word_count,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (fic_id, fic.title, fic.author, fic.summary, fic.rating, wc, now, now),
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
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    conn=Depends(get_db),
):
    """Return a paginated list of fics, with optional search and filters."""
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
def delete_fic(fic_id: str, conn=Depends(get_db)):
    result = conn.execute("DELETE FROM fics WHERE id = ?", (fic_id,))
    if result.rowcount == 0:
        raise HTTPException(404, "Fic not found")


# ---------------------------------------------------------------------------
# Routes — chapters
# ---------------------------------------------------------------------------

@app.post(
    "/fics/{fic_id}/chapters",
    response_model=FicDetail,
    status_code=201,
    summary="Append a chapter to an existing fic",
)
def add_chapter(fic_id: str, chapter: ChapterAddIn, conn=Depends(get_db)):
    row = conn.execute("SELECT * FROM fics WHERE id = ?", (fic_id,)).fetchone()
    if not row:
        raise HTTPException(404, "Fic not found")

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

    # ---- plain text ----
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

    # ---- html ----
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
# Routes — tags
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    return {"status": "ok", "docs": "/docs"}


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
