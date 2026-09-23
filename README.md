# Daymark

Daymark is a full-stack 31-day discipline and CS core tracker. The front end is plain HTML, CSS, and JavaScript, served by FastAPI. Each user gets an isolated progress record in SQLite.

## Run locally

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
uvicorn backend.main:app --reload
```

Open `http://127.0.0.1:8000`. Create an account to start tracking. The database is created as `daymark.db` on first run.

## Goal reminders

Temporary and long-term goals accept deadlines and show remaining time. Daymark checks unfinished goals at 50%, 75%, and 90% of their planned time and sends one reminder per threshold when SMTP is configured:

```powershell
$env:DAYMARK_SMTP_HOST = "smtp.example.com"
$env:DAYMARK_SMTP_PORT = "465"
$env:DAYMARK_SMTP_FROM = "daymark@example.com"
$env:DAYMARK_SMTP_PASSWORD = "your-app-password"
```

Without these variables, countdowns and pending states still work locally, but no email is sent.

## Structure

- `index.html` - app shell and dashboard markup
- `styles/app.css` - responsive Daymark visual system
- `scripts/app.js` - authentication, rendering, and progress interactions
- `backend/main.py` - FastAPI routes and SQLite persistence
