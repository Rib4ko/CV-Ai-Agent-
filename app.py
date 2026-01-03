import os
import uuid
import re
import base64
from io import BytesIO
from flask import Flask, render_template, request, send_file
from dotenv import load_dotenv
import openai
from xhtml2pdf import pisa
import pdfplumber
from PIL import Image

app = Flask(__name__)

# --- CONFIGURATION ---
load_dotenv()
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB Upload Limit

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

# --- FOLDERS (Only for PDFs now) ---
PDF_FOLDER = 'static/resumes'
os.makedirs(PDF_FOLDER, exist_ok=True)


# Note: We deleted PHOTOS_FOLDER because we don't need it anymore!

# --- HELPER: IMAGE TO BASE64 (The "Cloud Safe" Way) ---
def process_profile_photo(image_file):
    """
    Reads image from memory, crops to square, resizes to 300x300,
    and returns a base64 string that can be put directly into HTML.
    """
    try:
        # 1. Open image from memory
        img = Image.open(image_file)

        # 2. Convert to RGB
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # 3. Center Crop to Square
        width, height = img.size
        new_size = min(width, height)
        left = (width - new_size) / 2
        top = (height - new_size) / 2
        right = (width + new_size) / 2
        bottom = (height + new_size) / 2
        img = img.crop((left, top, right, bottom))

        # 4. Resize
        img = img.resize((300, 300), Image.Resampling.LANCZOS)

        # 5. Save to RAM (BytesIO) instead of Hard Drive
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)

        # 6. Convert to Base64 String
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


# --- AI GENERATION ---
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
                    2. Use EXACT HTML structure.
                    3. ALWAYS include the '[[PROFILE_PHOTO]]' placeholder in the header.

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
                            </div>

                        <div class="section">
                            <h2>Education</h2>
                            </div>

                        <div class="section">
                            <h2>Skills</h2>
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

# --- PDF GENERATION ---
def create_pdf(html_content, output_path):
    full_html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{ size: letter; margin: 0.6in; }}
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 10pt; line-height: 1.4; color: #333; }}

            .header-table {{ width: 100%; margin-bottom: 15px; }}
            .photo-cell {{ width: 110px; vertical-align: top; }}
            .info-cell {{ vertical-align: middle; padding-left: 15px; }}

            .profile-pic {{
                width: 100px; height: 100px; 
                object-fit: cover;
                border-radius: 6px;
                border: 1px solid #ccc;
            }}

            h1 {{ font-size: 20pt; margin: 0; color: #1a365d; text-transform: uppercase; }}
            .contact-info {{ color: #666; font-size: 9pt; margin-top: 5px; }}
            .header-line {{ border: 0; border-bottom: 2px solid #2c3e50; margin: 5px 0 15px 0; }}

            h2 {{ font-size: 11pt; color: #2980b9; border-bottom: 1px solid #ddd; margin-top: 15px; text-transform: uppercase; font-weight: bold; }}

            .job-header {{ width: 100%; margin-bottom: 2px; }}
            .job-title {{ text-align: left; }}
            .job-date {{ text-align: right; font-weight: bold; color: #555; font-size: 9pt; }}

            ul {{ padding-left: 15px; margin: 0; }}
            li {{ margin-bottom: 3px; }}
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


# --- ROUTE ---
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        job_post = request.form.get("job-post")
        text_input = request.form.get("user-data")
        user_pdf = request.files.get('user-pdf')
        profile_pic = request.files.get('profile-pic')

        # 1. Extract Text
        final_data = ""
        if user_pdf and user_pdf.filename != '':
            extracted = extract_pdf_text(user_pdf)
            if extracted: final_data = extracted
        if not final_data: final_data = text_input

        # 2. Process Image (Debug Logs Added)
        base64_photo = ""
        if profile_pic and profile_pic.filename != '':
            print(f"📸 Processing photo: {profile_pic.filename}")
            base64_photo = process_profile_photo(profile_pic)
            if base64_photo:
                print(f"✅ Photo converted to Base64 (Length: {len(base64_photo)})")
            else:
                print("❌ Photo processing failed.")

        if not final_data or not job_post:
             return render_template("index.html", error="Missing data or job post.")

        try:
            # 3. AI Generation
            print("🤖 Generating HTML...")
            html = generate_resume_content(final_data, job_post)

            # 4. Image Injection Logic (The Fix)
            if base64_photo:
                if '[[PROFILE_PHOTO]]' in html:
                    print("✅ Placeholder found. Replacing...")
                    html = html.replace('[[PROFILE_PHOTO]]', base64_photo)
                else:
                    print("⚠️ AI forgot the placeholder. Force injecting image...")
                    # Fallback: Find <h1> and inject image before it
                    # This is a bit of a "hack" but ensures the image appears
                    img_tag = f'<img src="{base64_photo}" class="profile-pic" style="width:100px; height:100px; border-radius:10px; margin-right:20px;" /><br>'
                    html = html.replace('<h1>', f'{img_tag}<h1>')
            else:
                # Cleanup if no photo
                html = html.replace('<img src="[[PROFILE_PHOTO]]" class="profile-pic" />', '')

            # 5. Create PDF
            filename = f"resume_{uuid.uuid4().hex}.pdf"
            path = os.path.join(PDF_FOLDER, filename)
            create_pdf(html, path)

            return render_template("index.html", filename=filename)

        except Exception as e:
            print(f"❌ Error: {e}")
            return render_template("index.html", error=f"System Error: {str(e)}")

    return render_template("index.html")

if __name__ == "__main__":
    app.run(debug=True)