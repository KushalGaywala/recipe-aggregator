import base64
import json
import os
import secrets
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .generator import GenerationError, generate_recipe

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "recipes.db"
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
MAX_PHOTO_BYTES = 20 * 1024 * 1024
BASE_DIR = Path(__file__).parent

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Recipe Box", docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
app.mount("/uploads", StaticFiles(directory=UPLOAD_DIR), name="uploads")
templates = Jinja2Templates(directory=BASE_DIR / "templates")


# ---------- database ----------

@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            yield conn
    finally:
        conn.close()


with db() as conn:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS recipes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            name_key TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL DEFAULT '',
            data TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS photos (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipe_id INTEGER NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )


def name_key(name: str) -> str:
    """Normalise a dish name so "Chicken  Tikka-Masala" and "chicken tikka masala" match."""
    name = unicodedata.normalize("NFKC", name).lower()
    kept = "".join(c if unicodedata.category(c)[0] in "LNM" else " " for c in name)
    return " ".join(kept.split())


def get_recipe_or_404(conn, recipe_id: int):
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="Recipe not found")
    return row


def delete_photo_file(filename: str) -> None:
    (UPLOAD_DIR / filename).unlink(missing_ok=True)


# ---------- optional password + basic headers ----------

def _authorized(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("basic "):
        return False
    try:
        _, _, password = base64.b64decode(auth[6:]).decode().partition(":")
    except ValueError:
        return False
    return secrets.compare_digest(password.encode(), APP_PASSWORD.encode())


@app.middleware("http")
async def guard(request: Request, call_next):
    if APP_PASSWORD and request.url.path != "/health" and not _authorized(request):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Recipe Box"'})
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# ---------- pages ----------

def render_index(request: Request, error: str = "", name: str = "", description: str = "", status: int = 200):
    with db() as conn:
        rows = conn.execute(
            """
            SELECT r.id, r.name, r.name_key, r.data,
                   (SELECT filename FROM photos p WHERE p.recipe_id = r.id ORDER BY p.id LIMIT 1) AS cover
            FROM recipes r ORDER BY r.name COLLATE NOCASE
            """
        ).fetchall()
    recipes = [
        {
            "id": r["id"],
            "name": r["name"],
            "key": r["name_key"],
            "summary": json.loads(r["data"]).get("summary", ""),
            "cover": r["cover"],
        }
        for r in rows
    ]
    return templates.TemplateResponse(
        request,
        "index.html",
        {"recipes": recipes, "error": error, "name": name, "description": description},
        status_code=status,
    )


@app.get("/")
def index(request: Request):
    return render_index(request)


@app.post("/recipes")
def create_recipe(
    request: Request,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
):
    name, description = name.strip(), description.strip()
    key = name_key(name)
    if not key:
        return render_index(request, "Enter the name of a dish.", name, description, 400)

    with db() as conn:
        existing = conn.execute("SELECT id FROM recipes WHERE name_key = ?", (key,)).fetchone()
    if existing:
        return RedirectResponse(f"/recipes/{existing['id']}?existing=1", status_code=303)

    try:
        recipe = generate_recipe(name, description)
    except GenerationError as e:
        return render_index(request, str(e), name, description, 502)

    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO recipes (name, name_key, description, data) VALUES (?, ?, ?, ?)",
                (name, key, description, json.dumps(recipe)),
            )
            recipe_id = cur.lastrowid
    except sqlite3.IntegrityError:  # someone saved the same dish while we were generating
        with db() as conn:
            recipe_id = conn.execute("SELECT id FROM recipes WHERE name_key = ?", (key,)).fetchone()["id"]
    return RedirectResponse(f"/recipes/{recipe_id}", status_code=303)


@app.get("/recipes/{recipe_id}")
def view_recipe(request: Request, recipe_id: int, existing: int = 0, error: str = ""):
    with db() as conn:
        row = get_recipe_or_404(conn, recipe_id)
        photos = conn.execute(
            "SELECT id, filename FROM photos WHERE recipe_id = ? ORDER BY id", (recipe_id,)
        ).fetchall()
    return templates.TemplateResponse(
        request,
        "recipe.html",
        {
            "recipe": row,
            "data": json.loads(row["data"]),
            "photos": photos,
            "existing": existing,
            "error": error,
        },
    )


@app.post("/recipes/{recipe_id}/delete")
def delete_recipe(recipe_id: int):
    with db() as conn:
        get_recipe_or_404(conn, recipe_id)
        filenames = [r["filename"] for r in conn.execute(
            "SELECT filename FROM photos WHERE recipe_id = ?", (recipe_id,)
        )]
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))
    for filename in filenames:
        delete_photo_file(filename)
    return RedirectResponse("/", status_code=303)


# ---------- photos ----------

def sniff_image(head: bytes) -> str | None:
    """Return a file extension based on the file's actual bytes, or None if it isn't a supported image."""
    if head.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return ".webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return ".avif"
    return None


@app.post("/recipes/{recipe_id}/photos")
def upload_photos(recipe_id: int, photos: Annotated[list[UploadFile], File()]):
    with db() as conn:
        get_recipe_or_404(conn, recipe_id)

    skipped = []
    saved = []
    for upload in photos:
        if not upload.filename:
            continue
        content = upload.file.read(MAX_PHOTO_BYTES + 1)
        ext = sniff_image(content[:16])
        if ext is None or len(content) > MAX_PHOTO_BYTES:
            skipped.append(upload.filename)
            continue
        filename = f"{uuid.uuid4().hex}{ext}"
        (UPLOAD_DIR / filename).write_bytes(content)
        saved.append(filename)

    if saved:
        with db() as conn:
            conn.executemany(
                "INSERT INTO photos (recipe_id, filename) VALUES (?, ?)",
                [(recipe_id, f) for f in saved],
            )

    url = f"/recipes/{recipe_id}#photos"
    if skipped:
        message = (
            "Skipped " + ", ".join(skipped)
            + " (only JPEG, PNG, WebP, GIF or AVIF images up to 20 MB are accepted)."
        )
        url = f"/recipes/{recipe_id}?error={quote(message, safe='')}#photos"
    return RedirectResponse(url, status_code=303)


@app.post("/photos/{photo_id}/delete")
def delete_photo(photo_id: int):
    with db() as conn:
        row = conn.execute("SELECT recipe_id, filename FROM photos WHERE id = ?", (photo_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Photo not found")
        conn.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    delete_photo_file(row["filename"])
    return RedirectResponse(f"/recipes/{row['recipe_id']}#photos", status_code=303)


@app.get("/health")
def health():
    return {"status": "ok"}
