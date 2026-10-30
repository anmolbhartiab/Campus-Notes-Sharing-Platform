import os, re, json, random, sqlite3, secrets, urllib.request
from datetime import datetime
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for, session,
                   flash, send_from_directory, abort)
from werkzeug.security import generate_password_hash, check_password_hash
from pypdf import PdfReader

BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__)
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY", "change-me-in-production"),
    MAX_CONTENT_LENGTH=10 * 1024 * 1024,           # 10 MB upload limit
    UPLOAD_FOLDER=os.path.join(BASE, "uploads"),
    DATABASE=os.path.join(BASE, "campusnotes.db"),
    SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

SUBJECTS = ["Mathematics", "Physics", "Chemistry", "Computer Science", "Electronics",
            "Mechanical", "Civil", "English", "Economics", "Management", "Biology", "Other"]
NOTE_SQL = """SELECT n.*, u.full_name AS uploader, c.name AS subject,
 (SELECT ROUND(AVG(rating),1) FROM reviews r WHERE r.note_id=n.id) AS avg_rating,
 (SELECT COUNT(*) FROM reviews r WHERE r.note_id=n.id) AS n_reviews
 FROM notes n JOIN users u ON u.id=n.user_id JOIN categories c ON c.id=n.category_id """

# ---------------- database ----------------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def q(sql, args=(), one=False):
    cur = db().execute(sql, args); rows = cur.fetchall()
    return (rows[0] if rows else None) if one else rows

def init_db():
    con = sqlite3.connect(app.config["DATABASE"])
    con.executescript("""
    CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, full_name TEXT NOT NULL,
      email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, is_admin INTEGER DEFAULT 0,
      created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL);
    CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
      category_id INTEGER NOT NULL REFERENCES categories(id), semester INTEGER NOT NULL,
      description TEXT, kind TEXT DEFAULT 'note', file_path TEXT NOT NULL,
      user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      downloads INTEGER DEFAULT 0, summary TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
    CREATE TABLE IF NOT EXISTS bookmarks(user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
      note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE, PRIMARY KEY(user_id,note_id));
    CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY AUTOINCREMENT,
      note_id INTEGER REFERENCES notes(id) ON DELETE CASCADE,
      user_id INTEGER REFERENCES users(id) ON DELETE CASCADE, rating INTEGER NOT NULL,
      comment TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(note_id,user_id));
    """)
    for s in SUBJECTS:
        con.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (s,))
    if not con.execute("SELECT 1 FROM users WHERE is_admin=1").fetchone():
        con.execute("INSERT INTO users(full_name,email,password_hash,is_admin) VALUES(?,?,?,1)",
                    ("Administrator", "admin@campusnotes.com", generate_password_hash("Admin@123")))
    con.commit(); con.close()

# ---------------- auth / csrf ----------------
def current_user():
    if "user" not in g:
        g.user = q("SELECT * FROM users WHERE id=?", (session.get("uid"),), one=True) if session.get("uid") else None
    return g.user

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not current_user():
            flash("Please log in first.", "warn"); return redirect(url_for("login", next=request.path))
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    @login_required
    def w(*a, **k):
        if not current_user()["is_admin"]: abort(403)
        return f(*a, **k)
    return w

def csrf_token():
    if "_csrf" not in session: session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]

@app.before_request
def csrf_protect():
    if request.method == "POST":
        t = session.get("_csrf")
        if not t or not secrets.compare_digest(t, request.form.get("csrf_token", "")):
            abort(400, "Invalid or missing CSRF token")

@app.context_processor
def inject():
    return dict(csrf_token=csrf_token, me=current_user(), SUBJECTS=SUBJECTS, now=datetime.now().year)

@app.template_filter("date")
def fmt_date(s): return (s or "")[:10]

@app.errorhandler(413)
def too_big(_): flash("File too large (max 10 MB).", "error"); return redirect(url_for("upload"))

# ---------------- AI helpers ----------------
def extract_text(path, pages=30):
    try:
        r = PdfReader(path)
        return " ".join((p.extract_text() or "") for p in r.pages[:pages]).replace("\n", " ")
    except Exception:
        return ""

def claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key: return None
    try:
        req = urllib.request.Request("https://api.anthropic.com/v1/messages", headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            data=json.dumps({"model": "claude-sonnet-4-6", "max_tokens": 700,
                             "messages": [{"role": "user", "content": prompt}]}).encode())
        return json.load(urllib.request.urlopen(req, timeout=40))["content"][0]["text"]
    except Exception:
        return None

def summarize(text, n=5):
    if len(text) < 80: return "Not enough readable text (scanned PDFs can't be summarised)."
    ai = claude("Summarise these study notes as 5 concise bullet points:\n\n" + text[:12000])
    if ai: return ai
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 30 < len(s) < 300]
    freq = {}
    for w in re.findall(r"[a-z]{4,}", text.lower()): freq[w] = freq.get(w, 0) + 1
    scored = sorted(range(len(sents)), key=lambda i: -sum(freq.get(w, 0) for w in re.findall(r"[a-z]{4,}", sents[i].lower())) / (len(sents[i].split()) + 5))[:n]
    return "\n".join("• " + sents[i] for i in sorted(scored)) or "Could not summarise."

def make_quiz(text, n=5):
    sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if 40 < len(s) < 220]
    pool = list({w for s in sents for w in re.findall(r"\b[A-Za-z]{6,}\b", s)})
    random.shuffle(sents); out = []
    for s in sents:
        c = re.findall(r"\b[A-Za-z]{6,}\b", s)
        if not c or len(pool) < 4: continue
        ans = max(c, key=len)
        opts = random.sample([w for w in pool if w.lower() != ans.lower()], 3) + [ans]
        random.shuffle(opts)
        out.append(dict(q=s.replace(ans, "_____", 1), options=opts, answer=ans))
        if len(out) == n: break
    return out

# ---------------- public pages ----------------
@app.route("/")
def index():
    recent = q(NOTE_SQL + "ORDER BY n.created_at DESC, n.id DESC LIMIT 6")
    top = q(NOTE_SQL + "ORDER BY n.downloads DESC LIMIT 6")
    stats = dict(notes=q("SELECT COUNT(*) c FROM notes", one=True)["c"],
                 users=q("SELECT COUNT(*) c FROM users", one=True)["c"],
                 downloads=q("SELECT COALESCE(SUM(downloads),0) c FROM notes", one=True)["c"])
    return render_template("index.html", recent=recent, top=top, stats=stats)

@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name, email, pw = request.form["full_name"].strip(), request.form["email"].strip().lower(), request.form["password"]
        err = None
        if not (2 <= len(name) <= 80): err = "Enter your full name."
        elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email): err = "Enter a valid email."
        elif len(pw) < 8 or not re.search(r"\d", pw) or not re.search(r"[A-Za-z]", pw): err = "Password needs 8+ characters with letters and numbers."
        elif pw != request.form.get("confirm"): err = "Passwords do not match."
        elif q("SELECT 1 FROM users WHERE email=?", (email,), one=True): err = "Email already registered."
        if err: flash(err, "error")
        else:
            db().execute("INSERT INTO users(full_name,email,password_hash) VALUES(?,?,?)", (name, email, generate_password_hash(pw))); db().commit()
            flash("Account created! Please log in.", "success"); return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q("SELECT * FROM users WHERE email=?", (request.form["email"].strip().lower(),), one=True)
        if u and check_password_hash(u["password_hash"], request.form["password"]):
            session.clear(); session["uid"] = u["id"]
            nxt = request.args.get("next", "")
            return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("admin" if u["is_admin"] else "dashboard"))
        flash("Invalid email or password.", "error")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
def logout():
    session.clear(); flash("Logged out.", "success"); return redirect(url_for("index"))

# ---------------- notes ----------------
@app.route("/browse")
def browse():
    sql, args, w = NOTE_SQL, [], []
    s = request.args
    if s.get("q"): w.append("(n.title LIKE ? OR n.description LIKE ?)"); args += [f"%{s['q']}%"] * 2
    if s.get("subject"): w.append("c.name=?"); args.append(s["subject"])
    if s.get("semester", "").isdigit(): w.append("n.semester=?"); args.append(int(s["semester"]))
    if s.get("kind") in ("note", "paper"): w.append("n.kind=?"); args.append(s["kind"])
    order = {"popular": "n.downloads DESC", "rating": "avg_rating DESC"}.get(s.get("sort"), "n.created_at DESC, n.id DESC")
    notes = q(sql + ("WHERE " + " AND ".join(w) if w else "") + " ORDER BY " + order, args)
    return render_template("browse.html", notes=notes, f=s)

@app.route("/note/<int:nid>")
def note(nid):
    n = q(NOTE_SQL + "WHERE n.id=?", (nid,), one=True) or abort(404)
    reviews = q("SELECT r.*, u.full_name FROM reviews r JOIN users u ON u.id=r.user_id WHERE note_id=? ORDER BY r.created_at DESC", (nid,))
    bm = current_user() and q("SELECT 1 FROM bookmarks WHERE user_id=? AND note_id=?", (current_user()["id"], nid), one=True)
    related = q(NOTE_SQL + "WHERE n.category_id=? AND n.id!=? ORDER BY n.downloads DESC LIMIT 3", (n["category_id"], nid))
    return render_template("note.html", n=n, reviews=reviews, bookmarked=bool(bm), related=related)

@app.route("/download/<int:nid>")
def download(nid):
    n = q("SELECT * FROM notes WHERE id=?", (nid,), one=True) or abort(404)
    db().execute("UPDATE notes SET downloads=downloads+1 WHERE id=?", (nid,)); db().commit()
    safe = re.sub(r"[^\w\- ]", "", n["title"]).strip() or "notes"
    return send_from_directory(app.config["UPLOAD_FOLDER"], n["file_path"], as_attachment=True, download_name=safe + ".pdf")

@app.route("/view/<int:nid>")
def view_pdf(nid):
    n = q("SELECT * FROM notes WHERE id=?", (nid,), one=True) or abort(404)
    return send_from_directory(app.config["UPLOAD_FOLDER"], n["file_path"], mimetype="application/pdf")

@app.route("/upload", methods=["GET", "POST"])
@login_required
def upload():
    if request.method == "POST":
        f, title = request.files.get("file"), request.form["title"].strip()
        desc, sem = request.form.get("description", "").strip()[:1000], request.form.get("semester", "")
        cat = q("SELECT id FROM categories WHERE name=?", (request.form.get("subject"),), one=True)
        err = None
        if not (3 <= len(title) <= 120): err = "Title must be 3–120 characters."
        elif not cat: err = "Select a valid subject."
        elif not sem.isdigit() or not 1 <= int(sem) <= 8: err = "Semester must be 1–8."
        elif not f or not f.filename: err = "Choose a PDF file."
        elif not f.filename.lower().endswith(".pdf"): err = "Only PDF files are allowed."
        elif f.stream.read(5) != b"%PDF-": err = "File is not a valid PDF."
        if err: flash(err, "error")
        else:
            f.stream.seek(0)
            fname = secrets.token_hex(12) + ".pdf"
            f.save(os.path.join(app.config["UPLOAD_FOLDER"], fname))
            kind = "paper" if request.form.get("kind") == "paper" else "note"
            cur = db().execute("INSERT INTO notes(title,category_id,semester,description,kind,file_path,user_id) VALUES(?,?,?,?,?,?,?)",
                               (title, cat["id"], int(sem), desc, kind, fname, current_user()["id"])); db().commit()
            flash("Uploaded successfully!", "success"); return redirect(url_for("note", nid=cur.lastrowid))
    return render_template("upload.html")

@app.route("/note/<int:nid>/delete", methods=["POST"])
@login_required
def delete_note(nid):
    n = q("SELECT * FROM notes WHERE id=?", (nid,), one=True) or abort(404)
    if n["user_id"] != current_user()["id"] and not current_user()["is_admin"]: abort(403)
    db().execute("DELETE FROM notes WHERE id=?", (nid,)); db().commit()
    try: os.remove(os.path.join(app.config["UPLOAD_FOLDER"], n["file_path"]))
    except OSError: pass
    flash("Note deleted.", "success")
    return redirect(url_for("admin" if current_user()["is_admin"] and request.form.get("from") == "admin" else "dashboard"))

@app.route("/note/<int:nid>/bookmark", methods=["POST"])
@login_required
def bookmark(nid):
    uid = current_user()["id"]
    if q("SELECT 1 FROM bookmarks WHERE user_id=? AND note_id=?", (uid, nid), one=True):
        db().execute("DELETE FROM bookmarks WHERE user_id=? AND note_id=?", (uid, nid))
    else:
        q("SELECT id FROM notes WHERE id=?", (nid,), one=True) or abort(404)
        db().execute("INSERT INTO bookmarks VALUES(?,?)", (uid, nid))
    db().commit(); return redirect(url_for("note", nid=nid))

@app.route("/note/<int:nid>/review", methods=["POST"])
@login_required
def review(nid):
    r = request.form.get("rating", "")
    if not r.isdigit() or not 1 <= int(r) <= 5: flash("Choose a rating 1–5.", "error")
    else:
        db().execute("INSERT INTO reviews(note_id,user_id,rating,comment) VALUES(?,?,?,?) ON CONFLICT(note_id,user_id) DO UPDATE SET rating=excluded.rating, comment=excluded.comment",
                     (nid, current_user()["id"], int(r), request.form.get("comment", "").strip()[:500])); db().commit()
        flash("Review saved.", "success")
    return redirect(url_for("note", nid=nid))

@app.route("/note/<int:nid>/summary", methods=["POST"])
@login_required
def make_summary(nid):
    n = q("SELECT * FROM notes WHERE id=?", (nid,), one=True) or abort(404)
    if not n["summary"]:
        s = summarize(extract_text(os.path.join(app.config["UPLOAD_FOLDER"], n["file_path"])))
        db().execute("UPDATE notes SET summary=? WHERE id=?", (s, nid)); db().commit()
    return redirect(url_for("note", nid=nid) + "#summary")

@app.route("/note/<int:nid>/quiz")
@login_required
def quiz(nid):
    n = q("SELECT * FROM notes WHERE id=?", (nid,), one=True) or abort(404)
    qs = make_quiz(extract_text(os.path.join(app.config["UPLOAD_FOLDER"], n["file_path"])))
    return render_template("quiz.html", n=n, qs=qs)

# ---------------- dashboards ----------------
@app.route("/dashboard")
@login_required
def dashboard():
    uid = current_user()["id"]
    mine = q(NOTE_SQL + "WHERE n.user_id=? ORDER BY n.created_at DESC", (uid,))
    saved = q(NOTE_SQL + "JOIN bookmarks b ON b.note_id=n.id WHERE b.user_id=? ", (uid,))
    rec = q(NOTE_SQL + """WHERE n.user_id!=? AND n.category_id IN (SELECT category_id FROM notes WHERE user_id=?
            UNION SELECT n2.category_id FROM bookmarks b JOIN notes n2 ON n2.id=b.note_id WHERE b.user_id=?)
            ORDER BY n.downloads DESC LIMIT 6""", (uid, uid, uid)) or q(NOTE_SQL + "ORDER BY n.downloads DESC LIMIT 6")
    return render_template("dashboard.html", mine=mine, saved=saved, rec=rec,
                           dl=sum(n["downloads"] for n in mine))

@app.route("/profile", methods=["POST"])
@login_required
def profile():
    name, pw = request.form["full_name"].strip(), request.form.get("password", "")
    if not 2 <= len(name) <= 80: flash("Invalid name.", "error")
    elif pw and (len(pw) < 8 or not re.search(r"\d", pw) or not re.search(r"[A-Za-z]", pw)): flash("Password needs 8+ chars with letters and numbers.", "error")
    else:
        db().execute("UPDATE users SET full_name=? WHERE id=?", (name, current_user()["id"]))
        if pw: db().execute("UPDATE users SET password_hash=? WHERE id=?", (generate_password_hash(pw), current_user()["id"]))
        db().commit(); flash("Profile updated.", "success")
    return redirect(url_for("dashboard"))

@app.route("/admin")
@admin_required
def admin():
    stats = dict(users=q("SELECT COUNT(*) c FROM users", one=True)["c"], notes=q("SELECT COUNT(*) c FROM notes", one=True)["c"],
                 downloads=q("SELECT COALESCE(SUM(downloads),0) c FROM notes", one=True)["c"],
                 reviews=q("SELECT COUNT(*) c FROM reviews", one=True)["c"])
    users = q("SELECT u.*, (SELECT COUNT(*) FROM notes WHERE user_id=u.id) AS n_notes FROM users u ORDER BY created_at DESC")
    return render_template("admin.html", stats=stats, users=users, notes=q(NOTE_SQL + "ORDER BY n.created_at DESC"),
                           activity=q("SELECT 'upload' t, n.title x, u.full_name who, n.created_at at FROM notes n JOIN users u ON u.id=n.user_id UNION ALL SELECT 'review', n.title, u.full_name, r.created_at FROM reviews r JOIN users u ON u.id=r.user_id JOIN notes n ON n.id=r.note_id UNION ALL SELECT 'signup', '', full_name, created_at FROM users ORDER BY at DESC LIMIT 10"))

@app.route("/admin/user/<int:uid>/delete", methods=["POST"])
@admin_required
def delete_user(uid):
    if uid == current_user()["id"]: flash("You can't delete yourself.", "error")
    else:
        for n in q("SELECT file_path FROM notes WHERE user_id=?", (uid,)):
            try: os.remove(os.path.join(app.config["UPLOAD_FOLDER"], n["file_path"]))
            except OSError: pass
        db().execute("DELETE FROM users WHERE id=?", (uid,)); db().commit(); flash("Student removed.", "success")
    return redirect(url_for("admin"))

init_db()
if __name__ == "__main__":
    app.run(debug=True)
