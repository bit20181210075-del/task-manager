from datetime import datetime, timedelta
import os
from typing import Optional

from flask import (
    Flask, render_template, request, redirect, url_for,
    flash, jsonify, abort, make_response, send_from_directory
)
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import or_
from werkzeug.exceptions import NotFound

# ====== إشعارات سطح المكتب (نحاول plyer ثم win10toast) ======
_notifier = None
try:
    from plyer import notification as _plyer_notify
    def _notify(title: str, message: str):
        _plyer_notify.notify(title=title, message=message, timeout=10)
    _notifier = "plyer"
except Exception:
    try:
        from win10toast import ToastNotifier
        _toast = ToastNotifier()
        def _notify(title: str, message: str):
            _toast.show_toast(title, message, duration=8, threaded=True)
        _notifier = "win10toast"
    except Exception:
        def _notify(title: str, message: str):
            print(f"[NOTIFY] {title}: {message}")
        _notifier = "stdout"

# ====== إعدادات التطبيق ======
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
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret")

# مدة التذكير قبل الاستحقاق بالدقائق
REMINDER_WINDOW_MIN = int(os.environ.get("REMINDER_WINDOW_MIN", "10"))

# >>> جديد: تنبيهات 4 مرات باليوم (٨، ١٠، ١٢، ٢ ظهراً) ضمن نافذة دقائق محددة
SLOT_HOURS = [8, 10, 12, 14]  # أربع فترات يومياً بفاصل ساعتين
SLOT_WINDOW_MIN = int(os.environ.get("SLOT_WINDOW_MIN", "10"))  # نافذة ±10 دقائق حول رأس الساعة

# مفتاح API بسيط
API_KEY = os.environ.get("API_KEY", "dev-api-key")

db = SQLAlchemy(app)


# ====== نموذج البيانات ======
class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(500))
    priority = db.Column(db.String(10), default="medium")          # low | medium | high
    due_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    done = db.Column(db.Boolean, default=False)
    last_notified_at = db.Column(db.DateTime, nullable=True)

    def to_dict(self):
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


# ====== أدوات مساعدة ======
@app.context_processor
def inject_now():
    return {"now": datetime.now()}

@app.after_request
def add_cors_headers(resp):
    if request.path.startswith("/api/"):
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type,X-API-KEY"
        resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,DELETE,OPTIONS"
    return resp

@app.route("/favicon.ico")
def favicon():
    return ("", 204)

def ensure_columns():
    with db.engine.begin() as conn:
        rows = conn.exec_driver_sql("PRAGMA table_info(task)").fetchall()
        cols = {r[1] for r in rows}
        if "priority" not in cols:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN priority TEXT DEFAULT 'medium'")
        if "due_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN due_at DATETIME")
        if "created_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN created_at DATETIME")
        if "last_notified_at" not in cols:
            conn.exec_driver_sql("ALTER TABLE task ADD COLUMN last_notified_at DATETIME")

def parse_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        if "T" in value:
            return datetime.fromisoformat(value)
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        try:
            return datetime.fromisoformat(value)
        except Exception:
            return None


# ====== الواجهة الرئيسية ======
@app.route("/")
def index():
    q = request.args.get("q", "").strip()
    pr = request.args.get("priority", "")
    only = request.args.get("only", "")             # open | done | ''
    sort = request.args.get("sort", "new")          # new | old | due

    query = Task.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))
    if pr:
        query = query.filter_by(priority=pr)
    if only == "open":
        query = query.filter_by(done=False)
    elif only == "done":
        query = query.filter_by(done=True)
    if sort == "old":
        query = query.order_by(Task.created_at.asc())
    elif sort == "due":
        query = query.order_by(Task.due_at.is_(None), Task.due_at.asc())
    else:
        query = query.order_by(Task.created_at.desc())

    tasks = query.all()
    total = len(tasks)
    done_count = sum(1 for t in tasks if t.done)
    open_count = total - done_count
    done_pct = int(round((done_count / total), 2) * 100) if total else 0

    return render_template(
        "index.html",
        tasks=tasks,
        total=total,
        done_count=done_count,
        open_count=open_count,
        done_pct=done_pct
    )


# ====== CRUD للواجهة ======
@app.route("/add", methods=["POST"])
def add():
    title = request.form.get("title", "").strip()
    description = (request.form.get("description") or "").strip() or None
    priority = request.form.get("priority", "medium")
    due_at = parse_datetime(request.form.get("due_at"))
    if not title:
        flash("العنوان مطلوب")
        return redirect(url_for("index"))
    db.session.add(Task(title=title, description=description, priority=priority, due_at=due_at))
    db.session.commit()
    flash("تمت إضافة المهمة ✅")
    return redirect(url_for("index"))

@app.route("/toggle/<int:task_id>", methods=["POST"])
def toggle(task_id):
    task = db.session.get(Task, task_id)
    if not task: raise NotFound()
    task.done = not task.done
    db.session.commit()
    flash("تم تحديث حالة المهمة")
    return redirect(url_for("index"))

@app.route("/delete/<int:task_id>", methods=["POST"])
def delete(task_id):
    task = db.session.get(Task, task_id)
    if not task: raise NotFound()
    db.session.delete(task)
    db.session.commit()
    flash("تم حذف المهمة 🗑️")
    return redirect(url_for("index"))

@app.route("/edit/<int:task_id>", methods=["GET", "POST"])
def edit(task_id):
    task = db.session.get(Task, task_id)
    if not task: raise NotFound()
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = (request.form.get("description") or "").strip() or None
        priority = request.form.get("priority", "medium")
        due_at = parse_datetime(request.form.get("due_at"))
        if not title:
            flash("العنوان مطلوب")
            return redirect(url_for("edit", task_id=task_id))
        task.title, task.description, task.priority, task.due_at = title, description, priority, due_at
        db.session.commit()
        flash("تم تحديث المهمة ✏️")
        return redirect(url_for("index"))
    return render_template("edit.html", task=task)


# ====== API بمفتاح بسيط ======
def require_api_key():
    key = request.headers.get("X-API-KEY") or request.args.get("api_key")
    if key != API_KEY:
        abort(make_response(jsonify(error="invalid_api_key"), 401))

@app.route("/api/tasks", methods=["GET"])
def api_list_tasks():
    require_api_key()
    q = request.args.get("q", "").strip()
    pr = request.args.get("priority", "")
    only = request.args.get("only", "")
    sort = request.args.get("sort", "new")
    query = Task.query
    if q:
        like = f"%{q}%"
        query = query.filter(or_(Task.title.ilike(like), Task.description.ilike(like)))
    if pr:
        query = query.filter_by(priority=pr)
    if only == "open":
        query = query.filter_by(done=False)
    elif only == "done":
        query = query.filter_by(done=True)
    if sort == "old":
        query = query.order_by(Task.created_at.asc())
    elif sort == "due":
        query = query.order_by(Task.due_at.is_(None), Task.due_at.asc())
    else:
        query = query.order_by(Task.created_at.desc())
    return jsonify([t.to_dict() for t in query.all()])

@app.route("/api/tasks/<int:task_id>", methods=["GET"])
def api_get_task(task_id):
    require_api_key()
    task = db.session.get(Task, task_id)
    if not task: abort(404)
    return jsonify(task.to_dict())

@app.route("/api/tasks", methods=["POST"])
def api_create_task():
    require_api_key()
    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    if not title:
        abort(make_response(jsonify(error="title_required"), 400))
    t = Task(
        title=title,
        description=(data.get("description") or None),
        priority=data.get("priority", "medium"),
        due_at=parse_datetime(data.get("due_at")),
        done=bool(data.get("done", False)),
    )
    db.session.add(t); db.session.commit()
    return jsonify(t.to_dict()), 201

@app.route("/api/tasks/<int:task_id>", methods=["PATCH"])
def api_update_task(task_id):
    require_api_key()
    task = db.session.get(Task, task_id)
    if not task: abort(404)
    data = request.get_json(force=True, silent=True) or {}
    if "title" in data: task.title = (data.get("title") or "").strip()
    if "description" in data: task.description = (data.get("description") or None)
    if "priority" in data: task.priority = data.get("priority") or "medium"
    if "due_at" in data: task.due_at = parse_datetime(data.get("due_at"))
    if "done" in data: task.done = bool(data.get("done"))
    db.session.commit()
    return jsonify(task.to_dict())

@app.route("/api/tasks/<int:task_id>", methods=["DELETE"])
def api_delete_task(task_id):
    require_api_key()
    task = db.session.get(Task, task_id)
    if not task: abort(404)
    db.session.delete(task); db.session.commit()
    return jsonify({"ok": True})

@app.route("/api/tasks/<int:task_id>/toggle", methods=["POST"])
def api_toggle_task(task_id):
    require_api_key()
    task = db.session.get(Task, task_id)
    if not task: abort(404)
    task.done = not task.done
    db.session.commit()
    return jsonify(task.to_dict())


# ====== PWA: manifest + service worker + صفحة أوفلاين ======
@app.route("/manifest.webmanifest")
def webmanifest():
    return send_from_directory(
        os.path.join(app.root_path, "static", "pwa"),
        "manifest.webmanifest",
        mimetype="application/manifest+json",
    )

@app.route("/sw.js")
def service_worker():
    resp = send_from_directory(
        os.path.join(app.root_path, "static", "pwa"),
        "sw.js",
        mimetype="application/javascript",
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    return resp

@app.route("/offline")
def offline():
    return render_template("offline.html")


@app.route("/health")
def health():
    return {"ok": True}


# ====== جدولة التذكير (APScheduler) ======
_scheduler = None

# --- مساعد: اكتشاف الفترة الحالية (٨/١٠/١٢/٢) ضمن نافذة دقائق محددة ---
def _current_slot(now: datetime) -> Optional[int]:
    for h in SLOT_HOURS:
        slot_time = now.replace(hour=h, minute=0, second=0, microsecond=0)
        if abs((now - slot_time).total_seconds()) <= SLOT_WINDOW_MIN * 60:
            return h
    return None

def check_due_tasks():
    now = datetime.now()
    window = timedelta(minutes=REMINDER_WINDOW_MIN)
    with app.app_context():
        # 1) تذكير المهام غير المنجزة القريبة من الاستحقاق والمتأخرة (اللوجيك السابق)
        tasks_with_due = Task.query.filter(Task.done.is_(False), Task.due_at.isnot(None)).all()
        for t in tasks_with_due:
            if t.due_at and now >= t.due_at:
                if not t.last_notified_at or (now - t.last_notified_at) >= timedelta(hours=1):
                    _notify("⏰ مهمة متأخرة", f"{t.title} — كان موعدها: {t.due_at.strftime('%Y-%m-%d %H:%M')}")
                    t.last_notified_at = now
            elif t.due_at and (t.due_at - now) <= window:
                if not t.last_notified_at:
                    mins = int((t.due_at - now).total_seconds() // 60)
                    _notify("🔔 تذكير مهمة", f"{t.title} — يتبقى ~{max(mins,0)} دقيقة")
                    t.last_notified_at = now

        # 2) >>> جديد: تذكير عام 4 مرات يومياً (٨، ١٠، ١٢، ٢) لكل مهمة غير منجزة
        slot = _current_slot(now)
        if slot is not None:
            open_tasks = Task.query.filter(Task.done.is_(False)).all()
            for t in open_tasks:
                # أرسل التنبيه لهذه المهمة مرة واحدة فقط داخل هذه الفترة في هذا اليوم
                if not t.last_notified_at or t.last_notified_at.date() != now.date() or t.last_notified_at.hour != slot:
                    msg = f"{t.title}"
                    if t.due_at:
                        msg += f" — الاستحقاق: {t.due_at.strftime('%Y-%m-%d %H:%M')}"
                    _notify("🔔 تذكير المهمات", msg)
                    t.last_notified_at = now

        db.session.commit()

def start_scheduler():
    global _scheduler
    if _scheduler: return
    from apscheduler.schedulers.background import BackgroundScheduler
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(check_due_tasks, "interval", minutes=1, id="due_checker", replace_existing=True)
    _scheduler.start()
    print(f"[SCHED] started (notifier={_notifier}, window={REMINDER_WINDOW_MIN}m, slots={SLOT_HOURS})")


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        ensure_columns()

    # شغّل المجدول
    try:
        start_scheduler()
    except Exception as e:
        print("[SCHED] failed:", e)

    # شغّل Flask محلياً أو على الاستضافة
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5000"))
    app.run(host=host, port=port, debug=False, use_reloader=False)

