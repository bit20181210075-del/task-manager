from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from typing import Optional

import requests

from flask import (
    Flask,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, or_
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL")
DB_PATH = os.environ.get("SQLITE_PATH", os.path.join(app.root_path, "tasks.db"))
if DATABASE_URL:
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "change-me-in-production")
app.config["REMINDER_WINDOW_MIN"] = int(os.environ.get("REMINDER_WINDOW_MIN", "10"))
app.config["REMINDER_POLL_SECONDS"] = int(os.environ.get("REMINDER_POLL_SECONDS", "60"))
app.config["TELEGRAM_BOT_TOKEN"] = os.environ.get("TELEGRAM_BOT_TOKEN", "8706088814:AAF9yz41489u0Jr8wyzHvwJ1IxVcrF_0A")
app.config["TELEGRAM_CHAT_ID"] = os.environ.get("TELEGRAM_CHAT_ID", "6497025227")
app.config["TELEGRAM_REMINDERS_ENABLED"] = os.environ.get("TELEGRAM_REMINDERS_ENABLED", "1") == "1"

db = SQLAlchemy(app)


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(255), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    tasks = db.relationship("Task", backref="owner", lazy=True)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(500))
    priority = db.Column(db.String(10), default="medium", nullable=False)  # low | medium | high
    due_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    done = db.Column(db.Boolean, default=False, nullable=False)
    last_notified_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "priority": self.priority,
            "due_at": self.due_at.isoformat() if self.due_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "done": self.done,
            "last_notified_at": self.last_notified_at.isoformat() if self.last_notified_at else None,
        }




def telegram_is_configured() -> bool:
    return bool(app.config.get("TELEGRAM_REMINDERS_ENABLED") and app.config.get("TELEGRAM_BOT_TOKEN") and app.config.get("TELEGRAM_CHAT_ID"))


def send_telegram_notification(message: str) -> bool:
    if not telegram_is_configured():
        print("TELEGRAM STATUS:", "disabled or not configured")
        print("TELEGRAM RESPONSE:", "missing token/chat_id or TELEGRAM_REMINDERS_ENABLED=0")
        return False

    url = f"https://api.telegram.org/bot{app.config['TELEGRAM_BOT_TOKEN']}/sendMessage"
    data = {
        "chat_id": app.config["TELEGRAM_CHAT_ID"],
        "text": message,
    }
    try:
        response = requests.post(url, data=data, timeout=15)
        print("TELEGRAM STATUS:", response.status_code)
        print("TELEGRAM RESPONSE:", response.text)
        return response.ok
    except Exception as exc:
        print("TELEGRAM STATUS:", "exception")
        print("TELEGRAM RESPONSE:", str(exc))
        return False


def collect_due_notifications(now: Optional[datetime] = None) -> list[tuple[Task, str, str]]:
    now = now or datetime.utcnow()
    reminder_window = timedelta(minutes=app.config["REMINDER_WINDOW_MIN"])
    tasks = Task.query.filter(Task.done.is_(False), Task.due_at.isnot(None)).all()
    items: list[tuple[Task, str, str]] = []

    for task in tasks:
        if task.due_at is None:
            continue

        should_send = False
        title = "Task reminder"
        body = ""

        if task.due_at <= now:
            if not task.last_notified_at or (now - task.last_notified_at) >= timedelta(minutes=30):
                title = "Overdue task"
                body = f"{task.title} was due at {task.due_at.strftime('%Y-%m-%d %H:%M')}"
                should_send = True
        elif task.due_at - now <= reminder_window:
            if not task.last_notified_at:
                remaining = max(int((task.due_at - now).total_seconds() // 60), 0)
                title = "Upcoming task"
                body = f"{task.title} is due in about {remaining} minutes."
                should_send = True

        if should_send:
            items.append((task, title, body))

    return items


def dispatch_due_notifications() -> int:
    now = datetime.utcnow()
    items = collect_due_notifications(now=now)
    sent_count = 0

    for task, title, body in items:
        owner_name = task.owner.username if task.owner else "Unknown user"
        owner_email = task.owner.email if task.owner else ""
        message = f"🔔 {title}\nTask: {task.title}\n{body}\nOwner: {owner_name}{' (' + owner_email + ')' if owner_email else ''}"
        if send_telegram_notification(message):
            task.last_notified_at = now
            sent_count += 1

    if sent_count:
        db.session.commit()
    return sent_count


def reminder_worker_loop() -> None:
    while True:
        try:
            with app.app_context():
                dispatch_due_notifications()
        except Exception:
            pass
        time.sleep(max(app.config["REMINDER_POLL_SECONDS"], 15))


_worker_started = False


def start_reminder_worker() -> None:
    global _worker_started
    if _worker_started or not telegram_is_configured():
        return
    _worker_started = True
    thread = threading.Thread(target=reminder_worker_loop, daemon=True)
    thread.start()

# ---------- helpers ----------
def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    value = value.strip()
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def current_user() -> Optional[User]:
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def login_required(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        if not current_user():
            flash("Please log in to continue.", "warning")
            return redirect(url_for("login", next=request.path))
        return view_func(*args, **kwargs)

    return wrapper


def claim_unassigned_tasks_for_user(user: User) -> None:
    unassigned = Task.query.filter(Task.user_id.is_(None)).all()
    if not unassigned:
        return
    for task in unassigned:
        task.user_id = user.id
    db.session.commit()


def user_task_query(user: User):
    return Task.query.filter(Task.user_id == user.id)


@app.context_processor
def inject_globals():
    return {
        "now": datetime.utcnow(),
        "current_user": current_user(),
        "reminder_poll_seconds": app.config["REMINDER_POLL_SECONDS"],
    }


@app.after_request
def add_no_cache_headers(resp):
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/health")
def health():
    return {"ok": True}


# ---------- auth ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if current_user():
        return redirect(url_for("index"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        email = (request.form.get("email") or "").strip().lower()
        password = request.form.get("password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not username or not email or not password:
            flash("All fields are required.", "danger")
            return render_template("register.html")
        if len(password) < 8:
            flash("Password must be at least 8 characters.", "danger")
            return render_template("register.html")
        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return render_template("register.html")
        if User.query.filter(or_(User.username == username, User.email == email)).first():
            flash("That username or email is already in use.", "danger")
            return render_template("register.html")

        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        claim_unassigned_tasks_for_user(user)
        session["user_id"] = user.id
        flash("Account created successfully.", "success")
        return redirect(url_for("index"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user():
        return redirect(url_for("index"))

    if request.method == "POST":
        identity = (request.form.get("identity") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter(or_(User.username == identity, User.email == identity.lower())).first()
        if not user or not user.check_password(password):
            flash("Invalid username/email or password.", "danger")
            return render_template("login.html")

        session.clear()
        session["user_id"] = user.id
        claim_unassigned_tasks_for_user(user)
        flash(f"Welcome back, {user.username}.", "success")
        return redirect(request.args.get("next") or url_for("index"))

    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    flash("You have been signed out.", "info")
    return redirect(url_for("login"))


@app.route("/security", methods=["GET", "POST"])
@login_required
def security():
    user = current_user()
    assert user is not None

    if request.method == "POST":
        current_password = request.form.get("current_password") or ""
        new_password = request.form.get("new_password") or ""
        confirm_password = request.form.get("confirm_password") or ""

        if not user.check_password(current_password):
            flash("Current password is incorrect.", "danger")
            return render_template("security.html")
        if len(new_password) < 8:
            flash("New password must be at least 8 characters.", "danger")
            return render_template("security.html")
        if new_password != confirm_password:
            flash("New passwords do not match.", "danger")
            return render_template("security.html")

        user.set_password(new_password)
        db.session.commit()
        flash("Password updated successfully.", "success")
        return redirect(url_for("security"))

    return render_template("security.html")


# ---------- tasks ----------
@app.route("/")
@login_required
def index():
    user = current_user()
    assert user is not None
    claim_unassigned_tasks_for_user(user)

    q = request.args.get("q", "").strip()
    pr = request.args.get("priority", "")
    only = request.args.get("only", "")
    sort = request.args.get("sort", "new")

    query = user_task_query(user)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))
    if pr:
        query = query.filter(Task.priority == pr)
    if only == "open":
        query = query.filter(Task.done.is_(False))
    elif only == "done":
        query = query.filter(Task.done.is_(True))

    if sort == "old":
        query = query.order_by(Task.created_at.asc())
    elif sort == "due":
        query = query.order_by(Task.due_at.is_(None), Task.due_at.asc(), Task.created_at.desc())
    else:
        query = query.order_by(Task.created_at.desc())

    tasks = query.all()
    total = len(tasks)
    done_count = sum(1 for task in tasks if task.done)
    open_count = total - done_count
    done_pct = int(round((done_count / total) * 100)) if total else 0
    overdue_count = sum(1 for task in tasks if task.due_at and task.due_at < datetime.utcnow() and not task.done)

    return render_template(
        "index.html",
        tasks=tasks,
        total=total,
        done_count=done_count,
        open_count=open_count,
        done_pct=done_pct,
        overdue_count=overdue_count,
    )


@app.route("/add", methods=["POST"])
@login_required
def add_task():
    user = current_user()
    assert user is not None

    title = (request.form.get("title") or "").strip()
    description = (request.form.get("description") or "").strip() or None
    priority = request.form.get("priority", "medium")
    due_at = parse_datetime(request.form.get("due_at"))

    if not title:
        flash("Title is required.", "danger")
        return redirect(url_for("index"))

    task = Task(
        user_id=user.id,
        title=title,
        description=description,
        priority=priority if priority in {"low", "medium", "high"} else "medium",
        due_at=due_at,
    )
    db.session.add(task)
    db.session.commit()
    flash("Task added successfully.", "success")
    return redirect(url_for("index"))


@app.route("/toggle/<int:task_id>", methods=["POST"])
@login_required
def toggle_task(task_id: int):
    user = current_user()
    assert user is not None
    task = user_task_query(user).filter(Task.id == task_id).first_or_404()
    task.done = not task.done
    db.session.commit()
    flash("Task status updated.", "success")
    return redirect(url_for("index"))


@app.route("/delete/<int:task_id>", methods=["POST"])
@login_required
def delete_task(task_id: int):
    user = current_user()
    assert user is not None
    task = user_task_query(user).filter(Task.id == task_id).first_or_404()
    db.session.delete(task)
    db.session.commit()
    flash("Task deleted.", "info")
    return redirect(url_for("index"))


@app.route("/edit/<int:task_id>", methods=["GET", "POST"])
@login_required
def edit_task(task_id: int):
    user = current_user()
    assert user is not None
    task = user_task_query(user).filter(Task.id == task_id).first_or_404()

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        description = (request.form.get("description") or "").strip() or None
        priority = request.form.get("priority", "medium")
        due_at = parse_datetime(request.form.get("due_at"))

        if not title:
            flash("Title is required.", "danger")
            return redirect(url_for("edit_task", task_id=task.id))

        task.title = title
        task.description = description
        task.priority = priority if priority in {"low", "medium", "high"} else "medium"
        task.due_at = due_at
        db.session.commit()
        flash("Task updated successfully.", "success")
        return redirect(url_for("index"))

    return render_template("edit.html", task=task)


# ---------- reminders ----------
@app.route("/api/reminders")
@login_required
def api_reminders():
    user = current_user()
    assert user is not None

    task_items = [item for item in collect_due_notifications() if item[0].user_id == user.id]
    notifications = [{"id": task.id, "title": title, "body": body} for task, title, body in task_items]

    if notifications:
        now = datetime.utcnow()
        for task, _, _ in task_items:
            task.last_notified_at = now
        db.session.commit()

    return jsonify({"notifications": notifications})



@app.route("/api/test-telegram", methods=["GET", "POST"])
def api_test_telegram():
    user = current_user()
    who = user.username if user else "guest"
    ok = send_telegram_notification(f"✅ Telegram test from Task Manager\nUser: {who}")
    return jsonify({
        "ok": ok,
        "configured": telegram_is_configured(),
        "user": who,
        "method": request.method,
    })


# ---------- migrations ----------
def ensure_columns() -> None:
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if "task" not in table_names:
        return

    cols = {col["name"] for col in inspector.get_columns("task")}
    statements = []

    if "priority" not in cols:
        statements.append("ALTER TABLE task ADD COLUMN priority VARCHAR(10) DEFAULT 'medium'")
    if "due_at" not in cols:
        statements.append("ALTER TABLE task ADD COLUMN due_at TIMESTAMP NULL")
    if "created_at" not in cols:
        statements.append("ALTER TABLE task ADD COLUMN created_at TIMESTAMP NULL")
    if "last_notified_at" not in cols:
        statements.append("ALTER TABLE task ADD COLUMN last_notified_at TIMESTAMP NULL")
    if "user_id" not in cols:
        statements.append("ALTER TABLE task ADD COLUMN user_id INTEGER NULL")

    if statements:
        with db.engine.begin() as conn:
            for stmt in statements:
                conn.exec_driver_sql(stmt)

    with db.engine.begin() as conn:
        if "created_at" in {col["name"] for col in inspect(db.engine).get_columns("task")}:
            conn.exec_driver_sql("UPDATE task SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
            conn.exec_driver_sql("UPDATE task SET priority = 'medium' WHERE priority IS NULL")


with app.app_context():
    db.create_all()
    ensure_columns()

start_reminder_worker()

print("APP BOOTSTRAP: database ready")
print("TELEGRAM CONFIGURED:", telegram_is_configured())


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    print(f"APP STARTED on {host}:{port}")
    app.run(host=host, port=port, debug=False)
    
@app.route('/api/test-telegram', methods=['GET', 'POST'])
def test_telegram():
    return {
        "status": "ok",
        "message": "Telegram route works!"
    }
