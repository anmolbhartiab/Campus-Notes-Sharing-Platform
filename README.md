# CampusNotes – Campus Notes Sharing Platform

Flask + SQLite + HTML/CSS/JS. PDFs are stored locally in `uploads/`.

## Run
```bash
python -m venv venv && source venv/bin/activate     # Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py                                        # open http://127.0.0.1:5000
```
The database (`campusnotes.db`) and admin account are created automatically.
**Admin login:** `admin@campusnotes.com` / `Admin@123` (change it in `init_db()` before deploying).

## Optional
- `export SECRET_KEY=...` – strong session key for production.
- `export ANTHROPIC_API_KEY=...` – Claude-powered summaries. Without it, a built-in extractive summariser is used. Quizzes are generated locally (fill-in-the-blank MCQs from PDF text).

## Structure
```
app.py            routes, DB schema, auth, CSRF, AI helpers
templates/        Jinja pages
static/style.css  blue/white responsive theme
uploads/          stored PDFs (random filenames)
```
## Security
Password hashing · session auth · CSRF token on every POST · PDF extension + magic-byte check · 10 MB limit · parameterised SQL · auto-escaped templates · delete limited to uploader/admin.

Note: summaries/quizzes need text-based PDFs; scanned images have no extractable text.
