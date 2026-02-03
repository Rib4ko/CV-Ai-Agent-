import os
import uuid
import re
import base64
from io import BytesIO
from flask import Flask, render_template, request, send_file, session, redirect, url_for, flash, jsonify
from dotenv import load_dotenv
import json
from werkzeug.utils import secure_filename
from pathlib import Path
from supabase import create_client, Client
import openai
from xhtml2pdf import pisa
import pdfplumber
from PIL import Image
import urllib.parse

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-secret-change-me")

# --- CONFIGURATION ---
load_dotenv()
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 

# Check Key on Startup
api_key = os.getenv("OPENROUTER_API_KEY")
if not api_key:
    print("❌ ERROR: OPENROUTER_API_KEY missing from environment!")

client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=api_key,
    default_headers={
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "Resume Builder App",
    }
)

# --- SUPABASE CLIENT ---
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY") or os.getenv("SUPABASE_SERVICE_KEY") or os.getenv("SUPABASE_ANON_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ WARNING: SUPABASE_URL or SUPABASE_KEY missing. Supabase auth will not work!")
else:
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)


# --- FOLDERS ---
PDF_FOLDER = 'static/resumes'
USER_UPLOADS = os.path.join(PDF_FOLDER, 'user_uploads')
os.makedirs(PDF_FOLDER, exist_ok=True)
os.makedirs(USER_UPLOADS, exist_ok=True)

# Limit for stored resume text to avoid huge blobs
MAX_RESUME_TEXT = int(os.getenv('MAX_RESUME_TEXT', 50000))  # chars

USERS_FILE = 'users.json'
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({}, f)

# Reviews should NOT be kept locally. Use Supabase to persist reviews.
# If a local reviews.json exists from a prior version, remove it to avoid storing reviews locally.
try:
    if os.path.exists('reviews.json'):
        try:
            os.remove('reviews.json')
            append_debug_log('Removed local reviews.json to avoid saving reviews locally')
        except Exception:
            pass
except Exception:
    pass


def append_review(entry):
    """Persist a review to Supabase 'reviews' table. Returns True on success, False otherwise."""
    if not (SUPABASE_URL and SUPABASE_KEY):
        append_debug_log('Supabase not configured; cannot save review')
        return False
    try:
        payload = {
            'user_id': entry.get('user_id'),
            'review': entry.get('feedback') or entry.get('review') or '',
            'filename': entry.get('filename'),
            'created_at': entry.get('ts')
        }
        res = supabase.table('reviews').insert(payload).execute()
        # If response is dict-shaped, check for errors
        if isinstance(res, dict):
            if res.get('error'):
                append_debug_log('Supabase review insert error: ' + safe_repr_response(res.get('error')))
                return False
            return True
        else:
            # SDK-like response: assume success if no explicit error attribute
            if getattr(res, 'error', None):
                append_debug_log('Supabase review insert error: ' + safe_repr_response(getattr(res, 'error')))
                return False
            return True
    except Exception as e:
        append_debug_log('Supabase review exception: ' + safe_repr_response(e))
        try:
            app.logger.exception('Failed to save review to supabase: %s', safe_repr_response(e))
        except Exception:
            pass
        return False

def load_users():
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_user(user_id):
    users = load_users()
    return users.get(user_id)

def set_user_resume_text(user_id, resume_text, filename=None):
    """Store a truncated resume text and optional original filename in the local users.json.
    We no longer persist the binary PDF to disk.
    """
    users = load_users()
    user = users.setdefault(user_id, {})
    if resume_text is None:
        # clear
        user.pop('resume_text', None)
        user.pop('resume_name', None)
    else:
        # sanitize and truncate
        try:
            resume_text = re.sub(r'[\x00-\x08\x0B-\x1F\x7F]', ' ', resume_text)
        except Exception:
            pass
        user['resume_text'] = resume_text[:MAX_RESUME_TEXT]
        if filename:
            user['resume_name'] = filename
    users[user_id] = user
    save_users(users)


def remove_user_resume(user_id):
    users = load_users()
    user = users.get(user_id, {})
    if 'resume_text' in user:
        del user['resume_text']
    if 'resume_name' in user:
        del user['resume_name']
    users[user_id] = user
    save_users(users)

def remove_user_resume(user_id):
    users = load_users()
    user = users.get(user_id, {})
    if 'resume' in user:
        del user['resume']
    users[user_id] = user
    save_users(users)


def extract_user_info(user_obj):
    """Return (id, email) from various Supabase user response shapes.
    user_obj may be a dict, or a SDK object with attributes like 'id' and 'identity_data'.
    """
    if not user_obj:
        return (None, None)
    # Dict-like
    if isinstance(user_obj, dict):
        uid = user_obj.get('id') or user_obj.get('user_id') or user_obj.get('sub')
        email = user_obj.get('email')
        if not email:
            ident = user_obj.get('identity_data') or user_obj.get('identity')
            if isinstance(ident, dict):
                email = ident.get('email')
        return (uid, email)

    # Object-like
    uid = getattr(user_obj, 'id', None) or getattr(user_obj, 'user_id', None) or getattr(user_obj, 'sub', None)
    email = getattr(user_obj, 'email', None)
    ident = getattr(user_obj, 'identity_data', None) or getattr(user_obj, 'identity', None)
    if not email and isinstance(ident, dict):
        email = ident.get('email')
    elif not email and ident is not None:
        email = getattr(ident, 'email', None)
    return (uid, email)


def safe_repr_response(obj):
    """Return a safe string representation of an object without invoking risky IO.
    Tries several strategies (dict -> json, response.text/json, fallback to repr)
    and never raises.
    """
    try:
        if obj is None:
            return 'None'
        if isinstance(obj, dict):
            try:
                return json.dumps(obj, default=str, ensure_ascii=False)
            except Exception:
                return repr(obj)
        # Some SDK responses are objects with .json() or .text
        if hasattr(obj, 'json'):
            try:
                return json.dumps(obj.json(), default=str, ensure_ascii=False)
            except Exception:
                pass
        if hasattr(obj, 'text'):
            try:
                t = obj.text
                if isinstance(t, bytes):
                    try:
                        return t.decode('utf-8', errors='replace')
                    except Exception:
                        return repr(t)
                return str(t)
            except Exception:
                pass
        return repr(obj)
    except Exception as e:
        return f"<unrepresentable response: {e}>"


def append_debug_log(msg):
    """Append a timestamped message to supabase_debug.log robustly.
    Falls back to app.logger when file I/O fails. Designed to never raise.
    """
    try:
        import datetime
        ts = datetime.datetime.utcnow().isoformat() + 'Z'
        with open('supabase_debug.log', 'a', encoding='utf-8') as f:
            f.write(f"[{ts}] {msg}\n")
    except Exception:
        try:
            app.logger.debug("append_debug_log failed: %s", safe_repr_response(msg))
        except Exception:
            pass

# Ensure the debug file exists early so user can see entries
try:
    append_debug_log('Application started')
except Exception:
    pass

# --- HELPER: IMAGE TO BASE64 ---
def process_profile_photo(image_file):
    try:
        img = Image.open(image_file)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Center Crop
        width, height = img.size
        new_size = min(width, height)
        left = (width - new_size) / 2
        top = (height - new_size) / 2
        right = (width + new_size) / 2
        bottom = (height + new_size) / 2
        img = img.crop((left, top, right, bottom))

        # Resize (Smaller 200x200 is fine for resumes)
        img = img.resize((200, 200), Image.Resampling.LANCZOS)

        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)
        img_str = base64.b64encode(buffer.read()).decode()
        return f"data:image/jpeg;base64,{img_str}"

    except Exception as e:
        try:
            import traceback
            app.logger.exception("Image processing error: %s\n%s", safe_repr_response(e), traceback.format_exc())
        except Exception:
            try:
                import traceback
                with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                    f.write("Image processing error: " + safe_repr_response(e) + "\n")
                    f.write("Traceback:\n" + traceback.format_exc() + "\n")
            except Exception:
                pass
        return None


# --- HELPER: PDF TEXT ---
def extract_pdf_text(pdf_file):
    text = ""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        try:
            import traceback
            app.logger.exception("Error reading PDF: %s\n%s", safe_repr_response(e), traceback.format_exc())
        except Exception:
            try:
                import traceback
                with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                    f.write("Error reading PDF: " + safe_repr_response(e) + "\n")
                    f.write("Traceback:\n" + traceback.format_exc() + "\n")
            except Exception:
                pass
        return None
    return text


# --- AI GENERATION (Updated for Brevity) ---
def generate_resume_content(data_me, job):
    try:
        response = client.chat.completions.create(
            model="google/gemini-2.0-flash-001",
            messages=[
                {
                    "role": "system",
                    "content": "You are a Resume Architect. Output ONLY valid HTML code. No markdown."
                },
                {
                    "role": "user",
                    "content": f"""
                    INPUTS:
                    - Candidate: {data_me}
                    - Job: {job}

                    INSTRUCTIONS:
                    1. Rewrite resume to align with job.
                    2. Use EXACT HTML structure provided.
                    3. ALWAYS include the '[[PROFILE_PHOTO]]' placeholder.
                    
                    *** CRITICAL ONE-PAGE CONSTRAINTS ***
                    - Professional Summary: Max 2-3 sentences.
                    - Experience: Limit to the 3 most relevant roles.
                    - Bullet Points: Max 3-4 concise bullet points per role.
                    - Focus on high-impact keywords to save space.
                    
                    STRUCTURE:
                    <div class="resume-wrapper">
                        <table class="header-table">
                            <tr>
                                <td class="photo-cell">
                                    <img src="[[PROFILE_PHOTO]]" class="profile-pic" />
                                </td>
                                <td class="info-cell">
                                    <h1>CANDIDATE NAME</h1>
                                    <p class="contact-info">Phone | Email | LinkedIn | GitHub</p>
                                </td>
                            </tr>
                        </table>
                        <hr class="header-line">

                        <div class="section">
                            <h2>Professional Summary</h2>
                            <p>...</p>
                        </div>

                        <div class="section">
                            <h2>Experience</h2>
                            <div class="job-entry">
                                <table class="job-header">
                                    <tr>
                                        <td class="job-title"><strong>Title</strong> at <strong>Company</strong></td>
                                        <td class="job-date">Dates</td>
                                    </tr>
                                </table>
                                <ul>
                                    <li>Point 1...</li>
                                    <li>Point 2...</li>
                                </ul>
                            </div>
                        </div>

                        <div class="section">
                            <h2>Education</h2>
                            <p>...</p>
                        </div>

                        <div class="section">
                            <h2>Skills</h2>
                            <p>...</p>
                        </div>
                    </div>
                    """
                }
            ],
            temperature=0.4,
        )
        return re.sub(r'```html|```', '', response.choices[0].message.content).strip()
    except Exception as e:
        try:
            import traceback
            try:
                app.logger.exception("AI Error: %s", safe_repr_response(e))
            except Exception:
                pass
            append_debug_log("AI Error: " + safe_repr_response(e))
            append_debug_log("Traceback: " + traceback.format_exc())
        except Exception:
            pass
        raise e


def inject_contact_icons(html):
    """
    Replace items inside <p class="contact-info">...</p> with PNG icons from
    the `static/resumes/icons` folder embedded as base64 data URIs. Falls back
    to plain text if a PNG is missing.
    """
    if not html:
        return html

    icon_dir = os.path.join(PDF_FOLDER, 'icons')
    mapping = {
        'email': 'email.png',
        'phone': 'phone.png',
        'linkedin': 'linkedin.png',
        'github': 'github.png'
    }

    icons = {}
    for key, fname in mapping.items():
        p = os.path.join(icon_dir, fname)
        try:
            with open(p, 'rb') as f:
                icons[key] = 'data:image/png;base64,' + base64.b64encode(f.read()).decode()
        except Exception:
            icons[key] = None

    def repl(m):
        prefix = m.group(1)
        content = m.group(2)

        # Email
        if icons.get('email'):
            content = re.sub(r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})',
                             lambda mm: f'<span class="contact-item"><img src="{icons["email"]}" class="contact-icon" />{mm.group(1)}</span>',
                             content)
        else:
            content = re.sub(r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})',
                             lambda mm: f'<span class="contact-item">{mm.group(1)}</span>',
                             content)

        # Phone (simple heuristic)
        if icons.get('phone'):
            content = re.sub(r'(\+?\d[\d\-\s]{6,}\d)',
                             lambda mm: f'<span class="contact-item"><img src="{icons["phone"]}" class="contact-icon" />{mm.group(1)}</span>',
                             content)
        else:
            content = re.sub(r'(\+?\d[\d\-\s]{6,}\d)',
                             lambda mm: f'<span class="contact-item">{mm.group(1)}</span>',
                             content)

        # LinkedIn (URL or the word LinkedIn, allow domain without protocol)
        if icons.get('linkedin'):
            content = re.sub(r'((?:https?://)?(?:www\.)?linkedin\.com[^\s<|]*)|(?:LinkedIn)',
                             lambda mm: (
                                 f'<span class="contact-item"><img src="{icons["linkedin"]}" class="contact-icon" />'
                                 + (f'<a href="{mm.group(0) if mm.group(0).startswith("http") else "https://"+mm.group(0)}">{mm.group(0)}</a>' if ("linkedin" in mm.group(0).lower() and "." in mm.group(0)) else mm.group(0))
                                 + '</span>'
                             ),
                             content)
        else:
            content = re.sub(r'((?:https?://)?(?:www\.)?linkedin\.com[^\s<|]*)|(?:LinkedIn)',
                             lambda mm: f'<span class="contact-item">{mm.group(0)}</span>',
                             content)

        # GitHub (URL or the word GitHub, allow domain without protocol)
        if icons.get('github'):
            content = re.sub(r'((?:https?://)?(?:www\.)?github\.com[^\s<|]*)|(?:GitHub)',
                             lambda mm: (
                                 f'<span class="contact-item"><img src="{icons["github"]}" class="contact-icon" />'
                                 + (f'<a href="{mm.group(0) if mm.group(0).startswith("http") else "https://"+mm.group(0)}">{mm.group(0)}</a>' if ("github" in mm.group(0).lower() and "." in mm.group(0)) else mm.group(0))
                                 + '</span>'
                             ),
                             content)
        else:
            content = re.sub(r'((?:https?://)?(?:www\.)?github\.com[^\s<|]*)|(?:GitHub)',
                             lambda mm: f'<span class="contact-item">{mm.group(0)}</span>',
                             content)

        # Clean up separators
        content = content.replace('|', '<span class="sep">|</span>')

        return f"{prefix}{content}{m.group(3)}"

    return re.sub(r'(<p class="contact-info">)(.*?)(</p>)', repl, html, flags=re.S)

# --- PDF GENERATION (Updated for Tight CSS) ---
def create_pdf(html_content, output_path):
    full_html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            /* TIGHT MARGINS FOR 1-PAGE LAYOUT */
            @page {{ size: letter; margin: 0.4in; }} 
            
            body {{ 
                font-family: Helvetica, Arial, sans-serif; 
                font-size: 9pt;           /* Smaller font */
                line-height: 1.25;        /* Tighter lines */
                color: #333; 
            }}

            .header-table {{ width: 100%; margin-bottom: 10px; }}
            .photo-cell {{ width: 90px; vertical-align: top; }}
            .info-cell {{ vertical-align: middle; padding-left: 10px; }}

            .profile-pic {{
                width: 80px; height: 80px;  /* Smaller photo */
                object-fit: cover;
                border-radius: 6px;
                border: 1px solid #ccc;
            }}

            h1 {{ font-size: 16pt; margin: 0; color: #1a365d; text-transform: uppercase; }}
            .contact-info {{ color: #666; font-size: 8pt; margin-top: 2px; }}
            .contact-item {{ display: inline-block; margin-right: 8px; color: #666; font-size: 8pt; vertical-align: middle; }}
            .contact-icon {{ width: 12px; height: 12px; vertical-align: middle; margin-right: 12px; display: inline-block; }}
            .sep {{ margin: 0 8px; color: #bbb; }}
            .header-line {{ border: 0; border-bottom: 2px solid #2c3e50; margin: 5px 0 10px 0; }}

            /* Compact Headers */
            h2 {{ 
                font-size: 10.5pt; 
                color: #2980b9; 
                border-bottom: 1px solid #ddd; 
                margin-top: 10px;         /* Less space above */
                margin-bottom: 4px;       /* Less space below */
                text-transform: uppercase; 
                font-weight: bold; 
            }}

            .job-header {{ width: 100%; margin-bottom: 1px; }}
            .job-title {{ text-align: left; font-size: 9.5pt; }}
            .job-date {{ text-align: right; font-weight: bold; color: #555; font-size: 8.5pt; }}

            /* Tighter Lists */
            ul {{ padding-left: 15px; margin: 0; margin-top: 2px; }}
            li {{ margin-bottom: 1px; }} /* Very tight list items */
            
            p {{ margin: 0 0 3px 0; }}
        </style>
    </head>
    <body>
        {html_content}
    </body>
    </html>
    """
    with open(output_path, "wb") as result_file:
        pisa_status = pisa.CreatePDF(src=full_html, dest=result_file)
    return not pisa_status.err


# --- ROUTES: AUTH + MAIN ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if not email or not password:
            flash('Missing email or password', 'error')
            return redirect(url_for('login'))
        if not (SUPABASE_URL and SUPABASE_KEY):
            flash('Auth backend not configured on server.', 'error')
            return redirect(url_for('login'))
        try:
            # Try the modern method first, fall back to older method if not available
            try:
                res = supabase.auth.sign_in_with_password({ 'email': email, 'password': password })
            except AttributeError:
                res = supabase.auth.sign_in({ 'email': email, 'password': password })

            # Safely log the raw response for troubleshooting without risking OSError
            resp_str = safe_repr_response(res)
            try:
                app.logger.debug("Supabase sign_in response: %s", resp_str)
            except Exception:
                # Last-resort: append to a local log file (won't crash request)
                try:
                    with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                        f.write("Supabase sign_in response: " + resp_str + "\n")
                except Exception:
                    pass

            # Check for explicit error field
            err = None
            if isinstance(res, dict):
                err = res.get('error') or (res.get('data') or {}).get('error') or res.get('message')
            else:
                err = getattr(res, 'error', None) or getattr(res, 'message', None)

            if err:
                flash(f'Login error: {err}', 'error')
                return redirect(url_for('login'))

            # Extract user in multiple possible shapes
            user = None
            if isinstance(res, dict):
                data = res.get('data') or res
                user = data.get('user') or (data.get('session') or {}).get('user') or res.get('user')
            else:
                user = getattr(res, 'user', None)
                if not user:
                    sess = getattr(res, 'session', None)
                    if sess and isinstance(sess, dict):
                        user = sess.get('user')

            if user:
                uid, email_addr = extract_user_info(user)
                if uid:
                    session['user_id'] = uid
                    if email_addr:
                        session['email'] = email_addr
                    # ensure a profiles row exists for this user
                    if SUPABASE_URL and SUPABASE_KEY:
                        try:
                            supabase.table('profiles').upsert({'id': uid, 'full_name': None}).execute()
                        except Exception as e:
                            try:
                                app.logger.exception("Supabase profiles upsert error (login): %s", safe_repr_response(e))
                            except Exception:
                                try:
                                    with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                                        f.write("Supabase profiles upsert error (login): " + safe_repr_response(e) + "\n")
                                except Exception:
                                    pass
                    flash('Logged in.', 'success')
                    return redirect(url_for('index'))
                else:
                        try:
                            app.logger.warning("Login: could not extract uid from user object: %s", safe_repr_response(user))
                        except Exception:
                            try:
                                with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                                    f.write("Login: could not extract uid from user object: " + safe_repr_response(user) + "\n")
                            except Exception:
                                pass
                        flash('Login failed. Unexpected auth response.', 'error')
                        return redirect(url_for('login'))
            # If we didn't get a user, give a helpful hint about verification
            flash('Login failed. Check credentials or verify your email if required.', 'error')
            return redirect(url_for('login'))
        except Exception as e:
            msg = safe_repr_response(e)
            try:
                app.logger.exception("Login exception: %s", msg)
            except Exception:
                try:
                    with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                        f.write("Login exception: " + msg + "\n")
                except Exception:
                    pass
            flash('Login error: An internal error occurred', 'error')
            return redirect(url_for('login'))
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')
        if not email or not password:
            flash('Missing email or password', 'error')
            return redirect(url_for('register'))
        if not (SUPABASE_URL and SUPABASE_KEY):
            flash('Auth backend not configured on server.', 'error')
            return redirect(url_for('register'))
        try:
            res = supabase.auth.sign_up({ 'email': email, 'password': password })
            if getattr(res, 'error', None):
                flash(str(res.error), 'error')
                return redirect(url_for('register'))
            data = res.get('data') if isinstance(res, dict) else res
            user = None
            if isinstance(data, dict):
                user = data.get('user') or data.get('session', {}).get('user')
            if user:
                uid, email_addr = extract_user_info(user)
                if uid:
                    session['user_id'] = uid
                    if email_addr:
                        session['email'] = email_addr
                    # ensure profile row exists for this new user
                    if SUPABASE_URL and SUPABASE_KEY:
                        try:
                            supabase.table('profiles').upsert({'id': uid, 'full_name': None}).execute()
                        except Exception as e:
                            try:
                                app.logger.exception("Supabase profiles upsert error (register): %s", safe_repr_response(e))
                            except Exception:
                                try:
                                    with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                                        f.write("Supabase profiles upsert error (register): " + safe_repr_response(e) + "\n")
                                except Exception:
                                    pass
                    flash('Account created. Check your email for confirmation if required.', 'success')
                    return redirect(url_for('index'))
                else:
                    flash('Registration complete. Please verify your email if required.', 'info')
                    return redirect(url_for('login'))
            else:
                flash('Registration complete. Please verify your email if required.', 'info')
                return redirect(url_for('login'))
        except Exception as e:
            flash(f'Registration error: {e}', 'error')
            return redirect(url_for('register'))
    return render_template('register.html')


@app.route('/logout')
def logout():
    session.pop('user_id', None)
    session.pop('email', None)
    flash('Logged out', 'info')
    return redirect(url_for('index'))


@app.route('/delete_resume', methods=['POST'])
def delete_resume():
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('index'))
    user = get_user(user_id)
    if user and (user.get('resume_text') or user.get('resume_name')):
        # remove local JSON reference
        remove_user_resume(user_id)

        # Deactivate active resume rows in Supabase for this user
        if SUPABASE_URL and SUPABASE_KEY:
            try:
                try:
                    supabase.table('resumes').update({'is_active': False}).eq('user_id', user_id).eq('is_active', True).execute()
                except Exception:
                    pass
            except Exception as e:
                try:
                    app.logger.exception("Supabase resume lookup/delete error: %s", safe_repr_response(e))
                except Exception:
                    try:
                        with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                            f.write("Supabase resume lookup/delete error: " + safe_repr_response(e) + "\n")
                    except Exception:
                        pass
        flash('Saved resume removed', 'success')
    return redirect(url_for('index'))


@app.route('/submit_review', methods=['POST'])
def submit_review():
    try:
        data = request.get_json(force=True)
        feedback = (data.get('feedback') or '').strip()
        filename = data.get('filename')
        user_id = session.get('user_id')
        if not feedback:
            return jsonify({'ok': False, 'error': 'Empty feedback'}), 400
        entry = {
            'id': uuid.uuid4().hex,
            'user_id': user_id,
            'filename': filename,
            'feedback': feedback,
            'ts': __import__('datetime').datetime.utcnow().isoformat() + 'Z'
        }
        ok = append_review(entry)
        if ok:
            return jsonify({'ok': True}), 200
        else:
            return jsonify({'ok': False, 'error': 'Feedback service unavailable'}), 503
    except Exception as e:
        try:
            app.logger.exception("submit_review error: %s", safe_repr_response(e))
        except Exception:
            pass
        append_debug_log('submit_review error: ' + safe_repr_response(e))
        return jsonify({'ok': False, 'error': 'Server error'}), 500


@app.route("/", methods=["GET", "POST"])
def index():
    user_id = session.get('user_id')
    saved_resume = None
    if user_id:
        user = get_user(user_id)
        # Determine if user has a saved resume text (we no longer save binary PDFs)
        if user and user.get('resume_text'):
            saved_resume = True
            saved_resume_name = user.get('resume_name') or None
        else:
            saved_resume = None
            saved_resume_name = None
    else:
        saved_resume = None
        saved_resume_name = None

    if request.method == "POST":
        # Debug: log incoming request fields to help reproduce client issues
        try:
            app.logger.debug(
                "Index POST received: job_present=%s, user_data_len=%d, user_pdf_present=%s, saved_resume=%s",
                bool(request.form.get('job-post')),
                len(request.form.get('user-data') or ""),
                bool(request.files.get('user-pdf') and request.files.get('user-pdf').filename),
                bool(saved_resume)
            )
        except Exception:
            pass

        # Persist a short record so we can trace requests even if logging is misconfigured
        try:
            append_debug_log(f"Index POST received: job_present={bool(request.form.get('job-post'))}, user_data_len={len(request.form.get('user-data') or '')}, user_pdf_present={bool(request.files.get('user-pdf') and request.files.get('user-pdf').filename)}, saved_resume={bool(saved_resume)}")
        except Exception:
            pass

        job_post = request.form.get("job-post")
        text_input = request.form.get("user-data")
        user_pdf = request.files.get('user-pdf')
        profile_pic = request.files.get('profile-pic')

        final_data = ""
        # If a new PDF was uploaded, save it (and remember it for logged-in users)
        if user_pdf and user_pdf.filename != '':
            # Read PDF in-memory and extract text. Do NOT persist the binary file.
            try:
                pdf_bytes = user_pdf.read()
            except Exception:
                pdf_bytes = None
            buffer = None
            if pdf_bytes:
                try:
                    buffer = BytesIO(pdf_bytes)
                except Exception:
                    buffer = None

            extracted = extract_pdf_text(buffer) if buffer is not None else None
            if extracted:
                final_data = extracted
            if user_id:
                # remember per-user resume text (local JSON)
                try:
                    set_user_resume_text(user_id, extracted or "", filename=user_pdf.filename)
                    saved_resume = True
                    saved_resume_name = user_pdf.filename
                except Exception:
                    pass

                # Prepare resume_text (sanitize and cap)
                resume_text = (extracted or "")
                # Remove control characters except common whitespace
                resume_text = re.sub(r'[\x00-\x08\x0B-\x1F\x7F]', ' ', resume_text)
                resume_text = resume_text.strip()
                if len(resume_text) > MAX_RESUME_TEXT:
                    resume_text = resume_text[:MAX_RESUME_TEXT]

                # Persist metadata to Supabase (resumes table) and set profiles.current_resume
                if SUPABASE_URL and SUPABASE_KEY:
                    try:
                        # mark previous active resumes inactive for this user
                        try:
                            supabase.table('resumes').update({'is_active': False}).eq('user_id', user_id).eq('is_active', True).execute()
                        except Exception:
                            # ignore failures for the deactivate step
                            pass

                        res = supabase.table('resumes').insert({
                            'user_id': user_id,
                            'original_filename': user_pdf.filename,
                            'storage_path': None,
                            'mime_type': user_pdf.content_type,
                            'size': len(pdf_bytes) if pdf_bytes is not None else None,
                            'resume_text': resume_text,
                            'is_active': True
                        }).execute()

                        inserted = None
                        if isinstance(res, dict):
                            inserted = (res.get('data') or [None])[0]
                        else:
                            inserted = getattr(res, 'data', [None])[0] if hasattr(res, 'data') else None

                        if inserted and inserted.get('id'):
                            supabase.table('profiles').upsert({'id': user_id, 'current_resume': inserted['id']}).execute()
                    except Exception as e:
                        try:
                            app.logger.exception("Supabase resume persist error: %s", safe_repr_response(e))
                        except Exception:
                            try:
                                with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                                    f.write("Supabase resume persist error: " + safe_repr_response(e) + "\n")
                            except Exception:
                                pass
        elif user_id and saved_resume:
            # Prefer persisted resume_text from Supabase (faster) and fall back to local saved text
            final_data = None
            if SUPABASE_URL and SUPABASE_KEY:
                try:
                    # fetch the active resume for this user
                    res = supabase.table('resumes').select('resume_text').eq('user_id', user_id).eq('is_active', True).limit(1).execute()
                    data = None
                    if isinstance(res, dict):
                        data = (res.get('data') or [None])[0]
                    else:
                        data = getattr(res, 'data', [None])[0] if hasattr(res, 'data') else None
                    if data and data.get('resume_text'):
                        final_data = data.get('resume_text')
                except Exception as e:
                    try:
                        app.logger.exception("Supabase resume fetch error: %s", safe_repr_response(e))
                    except Exception:
                        try:
                            with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                                f.write("Supabase resume fetch error: " + safe_repr_response(e) + "\n")
                        except Exception:
                            pass
            if not final_data:
                # fall back to local saved text in users.json
                user = get_user(user_id)
                if user and user.get('resume_text'):
                    final_data = user.get('resume_text')

        if not final_data:
            final_data = text_input

        base64_photo = ""
        if profile_pic and profile_pic.filename != '':
            try:
                app.logger.debug("Processing photo: %s", safe_repr_response(profile_pic.filename))
            except Exception:
                pass
            base64_photo = process_profile_photo(profile_pic)

        if not final_data or not job_post:
            # Provide a clear flash for client-side UX and log it
            try:
                app.logger.debug("Generation aborted: missing data or job_post. final_data_present=%s, job_post_present=%s", bool(final_data), bool(job_post))
            except Exception:
                pass
            flash('Missing data or job post. Upload a PDF or paste your resume text, and include a job description.', 'error')
            return render_template("index.html", error="Missing data or job post.", saved_resume=saved_resume, saved_resume_name=saved_resume_name, user_id=user_id)

        try:
            # Persist a start-of-generation record
            try:
                append_debug_log(f"Starting generation: saved_resume={bool(saved_resume)}, user_pdf={getattr(user_pdf, 'filename', None)}, job_len={len(job_post or '')}, final_data_len={len(final_data) if isinstance(final_data, str) else 'N/A'}")
            except Exception:
                pass

            try:
                app.logger.debug("Generating HTML for job post")
            except Exception:
                pass

            # 1) Generate HTML content
            try:
                # sanitize inputs to avoid null bytes and other problematic control characters
                try:
                    if isinstance(final_data, str) and '\x00' in final_data:
                        append_debug_log('Sanitizing final_data: removing null bytes')
                        final_data = final_data.replace('\x00', '')
                    if isinstance(job_post, str) and '\x00' in job_post:
                        append_debug_log('Sanitizing job_post: removing null bytes')
                        job_post = job_post.replace('\x00', '')
                except Exception:
                    pass

                html = generate_resume_content(final_data, job_post)
                if isinstance(html, str) and '\x00' in html:
                    append_debug_log('Sanitizing generated html: removing null bytes')
                    html = html.replace('\x00', '')
                append_debug_log(f"generate_resume_content OK: len_html={len(html) if html else 0}")
            except Exception as e_gen:
                append_debug_log("Error in generate_resume_content: " + safe_repr_response(e_gen))
                try:
                    import traceback
                    append_debug_log("Traceback: " + traceback.format_exc())
                except Exception:
                    pass
                raise

            # 2) Image Injection
            try:
                if base64_photo:
                    if '[[PROFILE_PHOTO]]' in html:
                        try:
                            app.logger.debug("Placeholder found. Replacing...")
                        except Exception:
                            pass
                        html = html.replace('[[PROFILE_PHOTO]]', base64_photo)
                    else:
                        try:
                            app.logger.debug("AI forgot placeholder. Force injecting...")
                        except Exception:
                            pass
                        img_tag = f'<img src="{base64_photo}" class="profile-pic" style="width:80px; height:80px; border-radius:6px; margin-right:15px;" /><br>'
                        html = html.replace('<h1>', f'{img_tag}<h1>')
                else:
                    html = html.replace('<img src="[[PROFILE_PHOTO]]" class="profile-pic" />', '')
                append_debug_log('Image injection complete')
            except Exception as e_img:
                append_debug_log('Image injection error: ' + safe_repr_response(e_img))
                try:
                    import traceback
                    append_debug_log('Traceback: ' + traceback.format_exc())
                except Exception:
                    pass
                raise

            # 3) Inject contact icons
            try:
                html = inject_contact_icons(html)
                append_debug_log('inject_contact_icons OK')
            except Exception as e_icons:
                append_debug_log('inject_contact_icons error: ' + safe_repr_response(e_icons))
                try:
                    import traceback
                    append_debug_log('Traceback: ' + traceback.format_exc())
                except Exception:
                    pass
                raise

            # 4) Create PDF
            try:
                filename = f"resume_{uuid.uuid4().hex}.pdf"
                path = os.path.join(PDF_FOLDER, filename)
                append_debug_log(f"Creating PDF at {path}")
                ok = create_pdf(html, path)
                append_debug_log(f"create_pdf returned {ok}")
                if not ok:
                    append_debug_log('pisa reported errors')
                    raise Exception('PDF generation failed (pisa error)')
            except Exception as e_pdf:
                append_debug_log('PDF creation error: ' + safe_repr_response(e_pdf))
                try:
                    import traceback
                    append_debug_log('Traceback: ' + traceback.format_exc())
                except Exception:
                    pass
                raise

            return render_template("index.html", filename=filename, saved_resume=saved_resume, saved_resume_name=saved_resume_name, user_id=user_id)

        except Exception as e:
            # Persist a short error record and full trace for debugging
            try:
                append_debug_log(f"Error during resume generation: {safe_repr_response(e)}; saved_resume={bool(saved_resume)}; user_pdf={getattr(user_pdf, 'filename', None)}; job_len={len(job_post) if isinstance(job_post,str) else 'N/A'}")
            except Exception:
                pass

            tb = None
            try:
                import traceback
                tb = traceback.format_exc()
            except Exception:
                tb = '<traceback unavailable>'
            ctx = {
                'final_data_len': len(final_data) if isinstance(final_data, str) else None,
                'job_post_len': len(job_post) if isinstance(job_post, str) else None,
                'saved_resume': bool(saved_resume),
                'user_pdf_filename': getattr(user_pdf, 'filename', None) if user_pdf is not None else None,
            }
            OpenAIAuth = getattr(openai, 'AuthenticationError', None)
            try:
                # If this was an auth error from the AI client, surface a helpful message
                if (OpenAIAuth and isinstance(e, OpenAIAuth)) or 'User not found' in safe_repr_response(e) or '401' in safe_repr_response(e):
                    flash('AI service unavailable or invalid credentials. Check API configuration (OPENROUTER_API_KEY / OPENAI keys).', 'error')
                else:
                    flash('System Error: An internal error occurred', 'error')
            except Exception:
                flash('System Error: An internal error occurred', 'error')

            try:
                app.logger.exception("Error during resume generation: %s; ctx=%s\n%s", safe_repr_response(e), safe_repr_response(ctx), tb)
            except Exception:
                try:
                    with open('supabase_debug.log', 'a', encoding='utf-8') as f:
                        f.write("Error during resume generation: " + safe_repr_response(e) + "\n")
                        f.write("Context: " + safe_repr_response(ctx) + "\n")
                        f.write("Traceback:\n" + (tb or '') + "\n")
                except Exception:
                    pass
            return render_template("index.html", error="System Error: An internal error occurred", saved_resume=saved_resume, saved_resume_name=saved_resume_name, user_id=user_id)

    return render_template("index.html", saved_resume=saved_resume, saved_resume_name=saved_resume_name, user_id=user_id)

@app.route('/_debug_logs')
def _debug_logs():
    """Return the end of supabase_debug.log for local debugging. Only enabled in debug mode."""
    if not app.debug:
        return "Not available", 404
    try:
        with open('supabase_debug.log', 'r', encoding='utf-8') as f:
            data = f.read()
            # Return the last ~10KB to keep response small
            return data[-10000:] if len(data) > 0 else "(no log entries yet)", 200
    except FileNotFoundError:
        return "(no log file found)", 200
    except Exception as e:
        try:
            app.logger.exception("Error reading debug log: %s", safe_repr_response(e))
        except Exception:
            pass
        return "(error reading log)", 500


if __name__ == "__main__":
    app.run(debug=True)