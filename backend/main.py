from datetime import date, datetime, timedelta
import os
import smtplib
from email.message import EmailMessage
import hashlib
import secrets
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
import psycopg
from psycopg.rows import dict_row
from psycopg.errors import UniqueViolation

ROOT = Path(__file__).resolve().parent.parent
DATABASE_URL = os.getenv('DATABASE_URL')
SESSION_COOKIE = 'daymark_session'
app = FastAPI(title='Daymark API')

class Credentials(BaseModel):
    email: EmailStr
    password: str
    name: str = ''

class ProgressUpdate(BaseModel):
    done: bool

class DailyTaskCreate(BaseModel):
    name: str
    type: str
    time: str
    motive: str

class MonthlyGoalCreate(BaseModel):
    title: str
    motive: str = ''

class MonthlySubgoalCreate(BaseModel):
    title: str

class TimedGoalCreate(BaseModel):
    title: str
    motive: str = ''
    deadline: datetime

class TimedSubtaskCreate(BaseModel):
    title: str
    deadline: datetime

class NoteCreate(BaseModel):
    content: str

class SubgoalUpdate(BaseModel):
    done: bool | None = None
    title: str | None = None
    order: int | None = None

class TimedSubtaskUpdate(BaseModel):
    done: bool | None = None
    title: str | None = None
    order: int | None = None

class DatabaseConnection:
    def __init__(self):
        if not DATABASE_URL:
            raise RuntimeError('DATABASE_URL is not configured')
        self.connection = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception, traceback):
        if exception_type:
            self.connection.rollback()
        else:
            self.connection.commit()
        self.connection.close()

    def execute(self, query, parameters=()):
        return self.connection.execute(query.replace('?', '%s'), parameters)

    def executescript(self, script):
        for statement in script.split(';'):
            if statement.strip():
                self.execute(statement)


def db():
    return DatabaseConnection()

def init_db():
    with db() as connection:
        connection.executescript('''
        CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS progress (user_id INTEGER NOT NULL, day INTEGER NOT NULL, task INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(user_id, day, task));
        CREATE TABLE IF NOT EXISTS sector_progress (user_id INTEGER NOT NULL, sector TEXT NOT NULL, item INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL, PRIMARY KEY(user_id, sector, item));
        CREATE TABLE IF NOT EXISTS daily_tasks (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, day INTEGER NOT NULL, name TEXT NOT NULL, type TEXT NOT NULL, time TEXT NOT NULL, motive TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, task_date TEXT);
        CREATE TABLE IF NOT EXISTS monthly_goals (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, month TEXT NOT NULL, title TEXT NOT NULL, motive TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS monthly_subgoals (id BIGSERIAL PRIMARY KEY, goal_id INTEGER NOT NULL, title TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS timed_goals (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, kind TEXT NOT NULL, title TEXT NOT NULL, motive TEXT NOT NULL, created_at TEXT NOT NULL, deadline TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS timed_subtasks (id BIGSERIAL PRIMARY KEY, goal_id INTEGER NOT NULL, title TEXT NOT NULL, created_at TEXT NOT NULL, deadline TEXT NOT NULL, done INTEGER NOT NULL DEFAULT 0, sort_order INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS goal_notifications (goal_id INTEGER NOT NULL, threshold INTEGER NOT NULL, sent_at TEXT NOT NULL, PRIMARY KEY(goal_id, threshold));
        CREATE TABLE IF NOT EXISTS notes (id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, day INTEGER NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL);
        ''')

@app.on_event('startup')
def startup():
    init_db()

def password_hash(password):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), b'daymark-v1', 120000).hex()

def get_user(authorization: str = Header(default=''), session_cookie: str = Cookie(default='', alias=SESSION_COOKIE)):
    token = authorization[7:] if authorization.startswith('Bearer ') else session_cookie
    if not token:
        raise HTTPException(401, 'Sign in to continue')
    with db() as connection:
        row = connection.execute('SELECT u.* FROM users u JOIN sessions s ON s.user_id = u.id WHERE s.token = ? AND s.expires_at > ?', (token, datetime.utcnow().isoformat())).fetchone()
    if not row:
        raise HTTPException(401, 'Your session has expired')
    return row

def create_session(user_id):
    token = secrets.token_urlsafe(32)
    with db() as connection:
        connection.execute('INSERT INTO sessions VALUES (?, ?, ?)', (token, user_id, (datetime.utcnow() + timedelta(days=14)).isoformat()))
    return token

def reset_daily_tasks_for_today(user_id, current_date):
    with db() as connection:
        todays_tasks = connection.execute('SELECT id FROM daily_tasks WHERE user_id = ? AND task_date = ?', (user_id, current_date.isoformat())).fetchall()
        if todays_tasks:
            return
        previous_day = connection.execute(
            'SELECT task_date FROM daily_tasks WHERE user_id = ? ORDER BY task_date DESC LIMIT 1',
            (user_id,),
        ).fetchone()
        if not previous_day:
            return
        task_rows = connection.execute(
            'SELECT name, type, time, motive FROM daily_tasks WHERE user_id = ? AND task_date = ? ORDER BY id',
            (user_id, previous_day['task_date']),
        ).fetchall()
        if not task_rows:
            return
        now = datetime.utcnow().isoformat()
        for row in task_rows:
            connection.execute(
                'INSERT INTO daily_tasks (user_id, day, name, type, time, motive, done, created_at, task_date) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)',
                (user_id, current_date.day, row['name'], row['type'], row['time'], row['motive'], now, current_date.isoformat()),
            )

def send_time_reminder(user, goal, threshold):
    host = os.getenv('DAYMARK_SMTP_HOST')
    sender = os.getenv('DAYMARK_SMTP_FROM')
    password = os.getenv('DAYMARK_SMTP_PASSWORD')
    if not host or not sender or not password:
        return
    message = EmailMessage()
    message['Subject'] = f"Daymark reminder: {goal['title']} needs attention"
    message['From'] = sender
    message['To'] = user['email']
    message.set_content(f"Your goal '{goal['title']}' is still pending and {threshold}% of its planned time has passed. Open Daymark and choose the next action.")
    with smtplib.SMTP_SSL(os.getenv('DAYMARK_SMTP_PORT', '465')) as server:
        server.login(sender, password)
        server.send_message(message)

def timed_goal_status(user, goal):
    now = datetime.utcnow()
    created = datetime.fromisoformat(goal['created_at'])
    deadline = datetime.fromisoformat(goal['deadline'])
    total_seconds = max((deadline - created).total_seconds(), 1)
    remaining_seconds = max((deadline - now).total_seconds(), 0)
    elapsed_percent = min(100, max(0, round((total_seconds - remaining_seconds) / total_seconds * 100)))
    return {**dict(goal), 'done': bool(goal['done']), 'status': 'completed' if goal['done'] else ('overdue' if remaining_seconds == 0 else 'pending'), 'remaining_seconds': int(remaining_seconds), 'elapsed_percent': elapsed_percent}

def check_goal_reminders(user, goals):
    for goal in goals:
        if goal['done']:
            continue
        status = timed_goal_status(user, goal)
        for threshold in (50, 75, 90):
            if status['elapsed_percent'] >= threshold:
                with db() as connection:
                    exists = connection.execute('SELECT 1 FROM goal_notifications WHERE goal_id = ? AND threshold = ?', (goal['id'], threshold)).fetchone()
                    if not exists:
                        try:
                            send_time_reminder(user, goal, threshold)
                        except (OSError, smtplib.SMTPException):
                            pass
                        finally:
                            connection.execute('INSERT INTO goal_notifications VALUES (?, ?, ?) ON CONFLICT DO NOTHING', (goal['id'], threshold, datetime.utcnow().isoformat()))

def dashboard(user):
    current_date = date.today()
    current_day = current_date.day
    current_month = current_date.strftime('%Y-%m')
    reset_daily_tasks_for_today(user['id'], current_date)
    with db() as connection:
        rows = connection.execute('SELECT task, done FROM progress WHERE user_id = ? AND day = ?', (user['id'], current_day)).fetchall()
        completed_days = connection.execute('SELECT COUNT(DISTINCT day) FROM progress WHERE user_id = ? AND done = 1 GROUP BY day HAVING COUNT(*) = 3', (user['id'],)).fetchall()
        today_note = connection.execute('SELECT content FROM notes WHERE user_id = ? AND day = ? ORDER BY id DESC LIMIT 1', (user['id'], current_day)).fetchone()
        sector_rows = connection.execute('SELECT sector, item, done FROM sector_progress WHERE user_id = ?', (user['id'],)).fetchall()
        daily_rows = connection.execute('SELECT id, name, type, time, motive, done FROM daily_tasks WHERE user_id = ? AND task_date = ? ORDER BY id', (user['id'], current_date.isoformat())).fetchall()
        goal_rows = connection.execute('SELECT id, title, motive FROM monthly_goals WHERE user_id = ? AND month = ? ORDER BY id', (user['id'], current_month)).fetchall()
        subgoal_rows = connection.execute('SELECT id, goal_id, title, done, sort_order FROM monthly_subgoals WHERE goal_id IN (SELECT id FROM monthly_goals WHERE user_id = ? AND month = ?) ORDER BY sort_order, id', (user['id'], current_month)).fetchall()
        timed_rows = connection.execute('SELECT id, kind, title, motive, created_at, deadline, done FROM timed_goals WHERE user_id = ? ORDER BY deadline', (user['id'],)).fetchall()
        month_rows = connection.execute('SELECT task_date, COUNT(*) AS total, SUM(done) AS completed FROM daily_tasks WHERE user_id = ? AND task_date LIKE ? GROUP BY task_date ORDER BY task_date DESC', (user['id'], f'{current_month}-%')).fetchall()
        month_totals = connection.execute('SELECT COUNT(*) AS total, COALESCE(SUM(done), 0) AS completed FROM daily_tasks WHERE user_id = ? AND task_date LIKE ?', (user['id'], f'{current_month}-%')).fetchone()
        timed_subtask_rows = connection.execute('SELECT id, goal_id, title, created_at, deadline, done, sort_order FROM timed_subtasks WHERE goal_id IN (SELECT id FROM timed_goals WHERE user_id = ?) ORDER BY sort_order, id', (user['id'],)).fetchall()
    tasks = [{'task': index, 'done': any(row['task'] == index and row['done'] for row in rows)} for index in range(3)]
    daily_tasks = [dict(row) for row in daily_rows]
    subgoals_by_goal = {}
    for row in subgoal_rows:
        subgoals_by_goal.setdefault(row['goal_id'], []).append({'id': row['id'], 'title': row['title'], 'done': bool(row['done'])})
    monthly_goals = [{'id': row['id'], 'title': row['title'], 'motive': row['motive'], 'subgoals': subgoals_by_goal.get(row['id'], [])} for row in goal_rows]
    monthly_goals = [{**goal, 'status': 'completed' if goal['subgoals'] and all(item['done'] for item in goal['subgoals']) else 'pending'} for goal in monthly_goals]
    timed_subtasks = {}
    for row in timed_subtask_rows:
        timed_subtasks.setdefault(row['goal_id'], []).append(timed_goal_status(user, row))
    timed_goals = [{**timed_goal_status(user, row), 'subtasks': timed_subtasks.get(row['id'], [])} for row in timed_rows]
    check_goal_reminders(user, timed_rows)
    sectors = {name: [] for name in ('month', 'temporary', 'longterm')}
    for row in sector_rows:
        sectors.setdefault(row['sector'], []).append({'item': row['item'], 'done': bool(row['done'])})
    monthly_total = month_totals['total'] if month_totals else 0
    monthly_completed = month_totals['completed'] if month_totals else 0
    analysis = {'month': current_month, 'total_tasks': monthly_total, 'completed_tasks': monthly_completed, 'completion_rate': round(monthly_completed / monthly_total * 100) if monthly_total else 0, 'active_days': len(month_rows), 'days': [dict(row) for row in month_rows]}
    return {'user': {'name': user['name'], 'email': user['email']}, 'date': current_date.isoformat(), 'date_label': current_date.strftime('%A, %d %B %Y'), 'day': current_day, 'daily_tasks': daily_tasks, 'monthly_goals': monthly_goals, 'timed_goals': timed_goals, 'analysis': analysis, 'tasks': tasks, 'sectors': sectors, 'days_complete': len(completed_days), 'streak': len(completed_days), 'note': today_note['content'] if today_note else ''}

def set_session_cookie(response: Response, token: str, request: Request):
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite='lax', secure=request.url.scheme == 'https')

@app.post('/api/auth/register')
def register(credentials: Credentials, request: Request, response: Response):
    if len(credentials.password) < 8:
        raise HTTPException(400, 'Password must be at least 8 characters')
    name = credentials.name.strip() or credentials.email.split('@')[0]
    try:
        with db() as connection:
            cursor = connection.execute('INSERT INTO users (name, email, password_hash, created_at) VALUES (?, ?, ?, ?) RETURNING id', (name, credentials.email.lower(), password_hash(credentials.password), datetime.utcnow().isoformat()))
            user_id = cursor.fetchone()['id']
    except UniqueViolation:
        raise HTTPException(409, 'An account with that email already exists')
    token = create_session(user_id)
    set_session_cookie(response, token, request)
    return {'token': token}

@app.post('/api/auth/login')
def login(credentials: Credentials, request: Request, response: Response):
    with db() as connection:
        user = connection.execute('SELECT * FROM users WHERE email = ?', (credentials.email.lower(),)).fetchone()
    if not user or user['password_hash'] != password_hash(credentials.password):
        raise HTTPException(401, 'Email or password is incorrect')
    token = create_session(user['id'])
    set_session_cookie(response, token, request)
    return {'token': token}

@app.post('/api/auth/logout')
def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {'ok': True}

@app.get('/api/me/dashboard')
def get_dashboard(user = Depends(get_user)):
    return dashboard(user)

@app.put('/api/progress/{task}')
def update_progress(task: int, update: ProgressUpdate, user = Depends(get_user)):
    if task not in range(3):
        raise HTTPException(400, 'Unknown task')
    current_day = date.today().day
    with db() as connection:
        connection.execute('INSERT INTO progress VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, day, task) DO UPDATE SET done = excluded.done, updated_at = excluded.updated_at', (user['id'], current_day, task, int(update.done), datetime.utcnow().isoformat()))
    return dashboard(user)

@app.post('/api/daily-tasks')
def create_daily_task(task: DailyTaskCreate, user = Depends(get_user)):
    values = [task.name.strip(), task.type.strip(), task.time.strip(), task.motive.strip()]
    if not all(values):
        raise HTTPException(400, 'Complete every task field')
    current_day = date.today().day
    with db() as connection:
        connection.execute('INSERT INTO daily_tasks (user_id, day, name, type, time, motive, created_at, task_date) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', (user['id'], current_day, *values, datetime.utcnow().isoformat(), date.today().isoformat()))
    return dashboard(user)

@app.put('/api/daily-tasks/{task_id}')
def update_daily_task(task_id: int, update: ProgressUpdate, user = Depends(get_user)):
    with db() as connection:
        cursor = connection.execute('UPDATE daily_tasks SET done = ? WHERE id = ? AND user_id = ?', (int(update.done), task_id, user['id']))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Task not found')
    return dashboard(user)

@app.delete('/api/daily-tasks/{task_id}')
def delete_daily_task(task_id: int, user = Depends(get_user)):
    with db() as connection:
        cursor = connection.execute('DELETE FROM daily_tasks WHERE id = ? AND user_id = ?', (task_id, user['id']))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Task not found')
    return dashboard(user)

@app.post('/api/monthly-goals')
def create_monthly_goal(goal: MonthlyGoalCreate, user = Depends(get_user)):
    title = goal.title.strip()
    if not title:
        raise HTTPException(400, 'Give your goal a title')
    with db() as connection:
        connection.execute('INSERT INTO monthly_goals (user_id, month, title, motive, created_at) VALUES (?, ?, ?, ?, ?)', (user['id'], date.today().strftime('%Y-%m'), title, goal.motive.strip(), datetime.utcnow().isoformat()))
    return dashboard(user)

@app.delete('/api/monthly-goals/{goal_id}')
def delete_monthly_goal(goal_id: int, user = Depends(get_user)):
    with db() as connection:
        goal = connection.execute('SELECT id FROM monthly_goals WHERE id = ? AND user_id = ?', (goal_id, user['id'])).fetchone()
        if not goal:
            raise HTTPException(404, 'Goal not found')
        connection.execute('DELETE FROM monthly_subgoals WHERE goal_id = ?', (goal_id,))
        connection.execute('DELETE FROM monthly_goals WHERE id = ?', (goal_id,))
    return dashboard(user)

@app.post('/api/monthly-goals/{goal_id}/subgoals')
def create_monthly_subgoal(goal_id: int, subgoal: MonthlySubgoalCreate, user = Depends(get_user)):
    title = subgoal.title.strip()
    with db() as connection:
        owned = connection.execute('SELECT id FROM monthly_goals WHERE id = ? AND user_id = ?', (goal_id, user['id'])).fetchone()
        if not owned:
            raise HTTPException(404, 'Goal not found')
        if not title:
            raise HTTPException(400, 'Give your subgoal a title')
        next_order = connection.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM monthly_subgoals WHERE goal_id = ?', (goal_id,)).fetchone()['next_order']
        connection.execute('INSERT INTO monthly_subgoals (goal_id, title, created_at, sort_order) VALUES (?, ?, ?, ?)', (goal_id, title, datetime.utcnow().isoformat(), next_order))
    return dashboard(user)

@app.put('/api/monthly-subgoals/{subgoal_id}')
def update_monthly_subgoal(subgoal_id: int, update: SubgoalUpdate, user = Depends(get_user)):
    with db() as connection:
        owned = connection.execute('SELECT goal_id, title FROM monthly_subgoals WHERE id = ? AND goal_id IN (SELECT id FROM monthly_goals WHERE user_id = ?)', (subgoal_id, user['id'])).fetchone()
        if not owned:
            raise HTTPException(404, 'Subgoal not found')

        if update.title is not None:
            title = update.title.strip()
            if not title:
                raise HTTPException(400, 'Give your subgoal a title')
            connection.execute('UPDATE monthly_subgoals SET title = ? WHERE id = ?', (title, subgoal_id))

        if update.done is not None:
            connection.execute('UPDATE monthly_subgoals SET done = ? WHERE id = ?', (int(update.done), subgoal_id))

        if update.order is not None:
            rows = connection.execute('SELECT id, sort_order FROM monthly_subgoals WHERE goal_id = ? ORDER BY sort_order, id', (owned['goal_id'],)).fetchall()
            ids = [row['id'] for row in rows]
            if subgoal_id not in ids:
                raise HTTPException(404, 'Subgoal not found')
            current_index = ids.index(subgoal_id)
            target_index = max(0, min(len(ids) - 1, update.order))
            ids.pop(current_index)
            ids.insert(target_index, subgoal_id)
            for index, row_id in enumerate(ids):
                connection.execute('UPDATE monthly_subgoals SET sort_order = ? WHERE id = ?', (index, row_id))
    return dashboard(user)

@app.delete('/api/monthly-subgoals/{subgoal_id}')
def delete_monthly_subgoal(subgoal_id: int, user = Depends(get_user)):
    with db() as connection:
        cursor = connection.execute('DELETE FROM monthly_subgoals WHERE id = ? AND goal_id IN (SELECT id FROM monthly_goals WHERE user_id = ?)', (subgoal_id, user['id']))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Subgoal not found')
    return dashboard(user)

@app.post('/api/timed-goals/{kind}')
def create_timed_goal(kind: str, goal: TimedGoalCreate, user = Depends(get_user)):
    if kind not in ('temporary', 'longterm'):
        raise HTTPException(400, 'Unknown timed goal type')
    if not goal.title.strip() or goal.deadline <= datetime.utcnow():
        raise HTTPException(400, 'Choose a future deadline and a goal title')
    with db() as connection:
        connection.execute('INSERT INTO timed_goals (user_id, kind, title, motive, created_at, deadline) VALUES (?, ?, ?, ?, ?, ?)', (user['id'], kind, goal.title.strip(), goal.motive.strip(), datetime.utcnow().isoformat(), goal.deadline.replace(tzinfo=None).isoformat()))
    return dashboard(user)

@app.put('/api/timed-goals/{goal_id}')
def update_timed_goal(goal_id: int, update: ProgressUpdate, user = Depends(get_user)):
    with db() as connection:
        cursor = connection.execute('UPDATE timed_goals SET done = ? WHERE id = ? AND user_id = ?', (int(update.done), goal_id, user['id']))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Timed goal not found')
    return dashboard(user)

@app.delete('/api/timed-goals/{goal_id}')
def delete_timed_goal(goal_id: int, user = Depends(get_user)):
    with db() as connection:
        owned = connection.execute('SELECT id FROM timed_goals WHERE id = ? AND user_id = ?', (goal_id, user['id'])).fetchone()
        if not owned:
            raise HTTPException(404, 'Timed goal not found')
        connection.execute('DELETE FROM timed_subtasks WHERE goal_id = ?', (goal_id,))
        cursor = connection.execute('DELETE FROM timed_goals WHERE id = ? AND user_id = ?', (goal_id, user['id']))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Timed goal not found')
    return dashboard(user)

@app.post('/api/timed-goals/{goal_id}/subtasks')
def create_timed_subtask(goal_id: int, subtask: TimedSubtaskCreate, user = Depends(get_user)):
    if not subtask.title.strip() or subtask.deadline <= datetime.utcnow():
        raise HTTPException(400, 'Choose a future deadline and a subtask title')
    with db() as connection:
        owned = connection.execute('SELECT id FROM timed_goals WHERE id = ? AND user_id = ?', (goal_id, user['id'])).fetchone()
        if not owned:
            raise HTTPException(404, 'Timed goal not found')
        next_order = connection.execute('SELECT COALESCE(MAX(sort_order), -1) + 1 AS next_order FROM timed_subtasks WHERE goal_id = ?', (goal_id,)).fetchone()['next_order']
        connection.execute('INSERT INTO timed_subtasks (goal_id, title, created_at, deadline, sort_order) VALUES (?, ?, ?, ?, ?)', (goal_id, subtask.title.strip(), datetime.utcnow().isoformat(), subtask.deadline.replace(tzinfo=None).isoformat(), next_order))
    return dashboard(user)

@app.put('/api/timed-subtasks/{subtask_id}')
def update_timed_subtask(subtask_id: int, update: TimedSubtaskUpdate, user = Depends(get_user)):
    with db() as connection:
        owned = connection.execute('SELECT goal_id, title FROM timed_subtasks WHERE id = ? AND goal_id IN (SELECT id FROM timed_goals WHERE user_id = ?)', (subtask_id, user['id'])).fetchone()
        if not owned:
            raise HTTPException(404, 'Subtask not found')

        if update.title is not None:
            title = update.title.strip()
            if not title:
                raise HTTPException(400, 'Give your subtask a title')
            connection.execute('UPDATE timed_subtasks SET title = ? WHERE id = ?', (title, subtask_id))

        if update.done is not None:
            connection.execute('UPDATE timed_subtasks SET done = ? WHERE id = ?', (int(update.done), subtask_id))

        if update.order is not None:
            rows = connection.execute('SELECT id, sort_order FROM timed_subtasks WHERE goal_id = ? ORDER BY sort_order, id', (owned['goal_id'],)).fetchall()
            ids = [row['id'] for row in rows]
            if subtask_id not in ids:
                raise HTTPException(404, 'Subtask not found')
            current_index = ids.index(subtask_id)
            target_index = max(0, min(len(ids) - 1, update.order))
            ids.pop(current_index)
            ids.insert(target_index, subtask_id)
            for index, row_id in enumerate(ids):
                connection.execute('UPDATE timed_subtasks SET sort_order = ? WHERE id = ?', (index, row_id))
    return dashboard(user)

@app.delete('/api/timed-subtasks/{subtask_id}')
def delete_timed_subtask(subtask_id: int, user = Depends(get_user)):
    with db() as connection:
        cursor = connection.execute('DELETE FROM timed_subtasks WHERE id = ? AND goal_id IN (SELECT id FROM timed_goals WHERE user_id = ?)', (subtask_id, user['id']))
    if cursor.rowcount == 0:
        raise HTTPException(404, 'Subtask not found')
    return dashboard(user)

@app.put('/api/sectors/{sector}/{item}')
def update_sector(sector: str, item: int, update: ProgressUpdate, user = Depends(get_user)):
    if sector not in ('month', 'temporary', 'longterm') or item < 0 or item > 4:
        raise HTTPException(400, 'Unknown sector item')
    with db() as connection:
        connection.execute('INSERT INTO sector_progress VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_id, sector, item) DO UPDATE SET done = excluded.done, updated_at = excluded.updated_at', (user['id'], sector, item, int(update.done), datetime.utcnow().isoformat()))
    return dashboard(user)

@app.post('/api/notes')
def save_note(note: NoteCreate, user = Depends(get_user)):
    current_day = date.today().day
    with db() as connection:
        connection.execute('INSERT INTO notes (user_id, day, content, created_at) VALUES (?, ?, ?, ?)', (user['id'], current_day, note.content.strip(), datetime.utcnow().isoformat()))
    return {'saved': True}

app.mount('/styles', StaticFiles(directory=ROOT / 'styles'), name='styles')
app.mount('/scripts', StaticFiles(directory=ROOT / 'scripts'), name='scripts')
app.mount('/assets', StaticFiles(directory=ROOT / 'assets'), name='assets')

@app.get('/')
def index():
    return FileResponse(ROOT / 'index.html')

@app.get('/favicon.ico')
def favicon():
    return FileResponse(ROOT / 'assets' / 'daymark-favicon.svg', media_type='image/svg+xml')
