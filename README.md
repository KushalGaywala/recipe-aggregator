# Recipe Box

A small self-hosted recipe app. Type the dish you want (plus optional notes like "vegetarian, serves 6"), and Claude writes the recipe. Every recipe is saved, so typing a dish you already have opens the saved one instead of writing a new one. You can add photos to any recipe later.

- **New dish:** generated once by Claude, then saved.
- **Existing dish:** the home page filters your recipes as you type and tells you when a dish is already saved. Submitting it opens the saved recipe; no API call is made.
- **Photos:** upload one or more (JPEG, PNG, WebP, GIF or AVIF, up to 20 MB each) from the recipe page. The first one becomes the recipe's cover image.

It's one container: Python (FastAPI), with SQLite and the uploaded photos stored in `/data`.

## Run it with Docker Compose

```bash
cp .env.example .env    # then put your ANTHROPIC_API_KEY in .env
docker compose up -d --build
```

Open http://localhost:8000.

## Deploy on Coolify

1. **New Resource → Public/Private Repository**, and pick this repo.
2. Build pack: **Dockerfile**. Set **Ports Exposes** to `8000`.
3. Under **Environment Variables**, add `ANTHROPIC_API_KEY` (and `APP_PASSWORD`, see below).
4. Under **Persistent Storage**, add a volume mounted at `/data`. **Without it, recipes and photos are lost on every redeploy.**
5. Set a domain and deploy. Coolify's health check can use `/health`.

You can also use the **Docker Compose** build pack instead: Coolify reads `docker-compose.yml`, creates the `recipe-data` volume, and lists its variables for you to fill in.

## Deploy on Portainer

1. **Stacks → Add stack → Repository.**
2. Repository URL: this repo; Compose path: `docker-compose.yml` (add credentials if the repo is private).
3. Under **Environment variables**, add `ANTHROPIC_API_KEY` and, optionally, `APP_PASSWORD`, `PORT`, `RECIPE_MODEL`, `RECIPE_EFFORT`.
4. **Deploy the stack.** Portainer builds the image and creates the `recipe-data` volume.

## Settings

| Variable | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | (none, required) | Your Anthropic API key, used to generate new recipes. Saved recipes still open without it. |
| `APP_PASSWORD` | empty (no login) | If set, the browser asks for a password (any username). **Set this if the app is reachable from the internet**, since every new recipe uses your API credits. |
| `RECIPE_MODEL` | `claude-opus-5-5` | The Claude model that writes recipes. |
| `RECIPE_EFFORT` | `medium` | How hard Claude thinks: `low`, `medium`, `high`, `xhigh` or `max`. Lower is faster and cheaper. |
| `PORT` | `8000` | Host port (Docker Compose only). |
| `DATA_DIR` | `/data` | Where the database and photos are stored inside the container. |

A new recipe usually takes 10–60 seconds to write. Opening saved recipes costs nothing.

## Data and backups

Everything lives in the `/data` volume:

- `recipes.db`: SQLite database with your recipes
- `uploads/`: your photos

To back up, copy that volume. If you bind-mount a host folder instead of using a named volume, make sure the container's user can write to it: `chown -R 1000:1000 /path/on/host`.

## Local development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
ANTHROPIC_API_KEY=... uvicorn app.main:app --reload
```

Data goes to `./data` when `DATA_DIR` isn't set.
