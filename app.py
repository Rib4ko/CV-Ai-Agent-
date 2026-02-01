import os
import uuid
import re
import base64
from io import BytesIO
from flask import Flask, render_template, request, send_file, session, redirect, url_for, flash
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

USERS_FILE = 'users.json'
if not os.path.exists(USERS_FILE):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump({}, f)

def load_users():
    with open(USERS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def save_users(data):
    with open(USERS_FILE, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=2)

def get_user(user_id):
    users = load_users()
    return users.get(user_id)

def set_user_resume(user_id, filename):
    users = load_users()
    user = users.setdefault(user_id, {})
    user['resume'] = filename
    users[user_id] = user
    save_users(users)

def remove_user_resume(user_id):
    users = load_users()
    user = users.get(user_id, {})
    if 'resume' in user:
        del user['resume']
    users[user_id] = user
    save_users(users)

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
        print(f"Image processing error: {e}")
        return None


# --- HELPER: PDF TEXT ---
def extract_pdf_text(pdf_file):
    text = ""
    try:
        with pdfplumber.open(pdf_file) as pdf:
            for page in pdf.pages:
                text += (page.extract_text() or "") + "\n"
    except Exception as e:
        print(f"Error reading PDF: {e}")
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
        print(f"AI Error: {e}")
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
            res = supabase.auth.sign_in_with_password({ 'email': email, 'password': password })
            # API may return error in different shapes
            if getattr(res, 'error', None):
                flash(str(res.error), 'error')
                return redirect(url_for('login'))
            data = res.get('data') if isinstance(res, dict) else res
            user = None
            if isinstance(data, dict):
                user = data.get('user') or data.get('session', {}).get('user')
            if not user and isinstance(res, dict):
                user = res.get('user')
            if user:
                session['user_id'] = user.get('id')
                session['email'] = user.get('email')
                flash('Logged in.', 'success')
                return redirect(url_for('index'))
            else:
                flash('Login failed. Check credentials.', 'error')
                return redirect(url_for('login'))
        except Exception as e:
            flash(f'Login error: {e}', 'error')
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
                session['user_id'] = user.get('id')
                session['email'] = user.get('email')
                flash('Account created. Check your email for confirmation if required.', 'success')
                return redirect(url_for('index'))
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
    if user and user.get('resume'):
        path = os.path.join(USER_UPLOADS, user['resume'])
        if os.path.exists(path):
            os.remove(path)
        remove_user_resume(user_id)
        flash('Saved resume removed', 'success')
    return redirect(url_for('index'))


@app.route("/", methods=["GET", "POST"])
def index():
    user_id = session.get('user_id')
    saved_resume = None
    if user_id:
        user = get_user(user_id)
        if user and user.get('resume'):
            saved_path = os.path.join(USER_UPLOADS, user['resume'])
            if os.path.exists(saved_path):
                saved_resume = user['resume']

    if request.method == "POST":
        job_post = request.form.get("job-post")
        text_input = request.form.get("user-data")
        user_pdf = request.files.get('user-pdf')
        profile_pic = request.files.get('profile-pic')

        final_data = ""
        # If a new PDF was uploaded, save it (and remember it for logged-in users)
        if user_pdf and user_pdf.filename != '':
            save_name = f"{uuid.uuid4().hex}_{secure_filename(user_pdf.filename)}"
            save_path = os.path.join(USER_UPLOADS, save_name)
            user_pdf.save(save_path)
            extracted = extract_pdf_text(save_path)
            if extracted:
                final_data = extracted
            if user_id:
                # remember per-user
                set_user_resume(user_id, save_name)
                saved_resume = save_name
        elif user_id and saved_resume:
            # Use persisted resume if available
            saved_path = os.path.join(USER_UPLOADS, saved_resume)
            extracted = extract_pdf_text(saved_path)
            if extracted:
                final_data = extracted

        if not final_data:
            final_data = text_input

        base64_photo = ""
        if profile_pic and profile_pic.filename != '':
            print(f" Processing photo: {profile_pic.filename}")
            base64_photo = process_profile_photo(profile_pic)

        if not final_data or not job_post:
             return render_template("index.html", error="Missing data or job post.", saved_resume=saved_resume, user_id=user_id)

        try:
            print(" Generating HTML...")
            html = generate_resume_content(final_data, job_post)

            # Image Injection
            if base64_photo:
                if '[[PROFILE_PHOTO]]' in html:
                    print(" Placeholder found. Replacing...")
                    html = html.replace('[[PROFILE_PHOTO]]', base64_photo)
                else:
                    print(" AI forgot placeholder. Force injecting...")
                    img_tag = f'<img src="{base64_photo}" class="profile-pic" style="width:80px; height:80px; border-radius:6px; margin-right:15px;" /><br>'
                    html = html.replace('<h1>', f'{img_tag}<h1>')
            else:
                html = html.replace('<img src="[[PROFILE_PHOTO]]" class="profile-pic" />', '')

            # Inject contact icons into contact-info block
            html = inject_contact_icons(html)

            filename = f"resume_{uuid.uuid4().hex}.pdf"
            path = os.path.join(PDF_FOLDER, filename)
            create_pdf(html, path)

            return render_template("index.html", filename=filename, saved_resume=saved_resume, user_id=user_id)

        except Exception as e:
            print(f" Error: {e}")
            return render_template("index.html", error=f"System Error: {str(e)}", saved_resume=saved_resume, user_id=user_id)

    return render_template("index.html", saved_resume=saved_resume, user_id=user_id)

if __name__ == "__main__":
    app.run(debug=True)