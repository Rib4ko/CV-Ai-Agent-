import os
import uuid
import re
from flask import Flask, render_template, request, send_file
from dotenv import load_dotenv
import openai  # STANDARD LIBRARY
from xhtml2pdf import pisa
import pdfplumber

app = Flask(__name__)
load_dotenv()

# --- CONFIGURATION FOR OPENROUTER ---
client = openai.OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.getenv("OPENROUTER_API_KEY"),
    # Optional headers for OpenRouter rankings
    default_headers={
        "HTTP-Referer": "http://localhost:5000",
        "X-Title": "Resume Builder App",
    }
)

# --- FOLDER SETUP ---
PDF_FOLDER = 'static/resumes'
PHOTOS_FOLDER = 'static/photos'
os.makedirs(PDF_FOLDER, exist_ok=True)
os.makedirs(PHOTOS_FOLDER, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB Limit


# --- HELPER: READ PDF ---
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


# --- AI GENERATION (OPENROUTER) ---
def generate_resume_content(data_me, job):
    try:
        response = client.chat.completions.create(
            # You can change this to 'meta-llama/llama-3-70b-instruct' or 'deepseek/deepseek-chat' if you want
            model="google/gemini-2.0-flash-001",
            messages=[
                {
                    "role": "system",
                    "content": "You are a Resume Architect. Output ONLY valid HTML code. No markdown formatting."
                },
                {
                    "role": "user",
                    "content": f"""
                    INPUTS:
                    - Candidate Profile: {data_me}
                    - Target Job: {job}

                    INSTRUCTIONS:
                    1. Rewrite the resume to align with the job requirements.
                    2. Use the EXACT HTML structure below (do not change class names).
                    3. Do NOT use markdown (```html). Just output raw HTML.

                    REQUIRED HTML STRUCTURE:
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
                            <p>Summary text...</p>
                        </div>

                        <div class="section">
                            <h2>Experience</h2>
                            <div class="job-entry">
                                <table class="job-header">
                                    <tr>
                                        <td class="job-title"><strong>Job Title</strong> at <strong>Company</strong></td>
                                        <td class="job-date">Jan 2020 - Present</td>
                                    </tr>
                                </table>
                                <ul>
                                    <li>Action verb + context + result...</li>
                                    <li>Action verb + context + result...</li>
                                </ul>
                            </div>
                        </div>

                        <div class="section">
                            <h2>Education</h2>
                            <p>Degree, University, Year</p>
                        </div>

                        <div class="section">
                            <h2>Skills</h2>
                            <p><strong>Languages:</strong> Python, Java...</p>
                        </div>
                    </div>
                    """
                }
            ],
            temperature=0.5,  # Keeps the formatting stable
        )

        raw_content = response.choices[0].message.content
        # Clean up if the AI still adds markdown fences
        clean_content = re.sub(r'```html|```', '', raw_content).strip()
        return clean_content

    except Exception as e:
        print(f"OpenRouter Error: {e}")
        raise e


# --- PDF GENERATION (Keep this exactly the same) ---
def create_pdf(html_content, output_path):
    full_html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{ size: letter; margin: 0.6in; }}
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 10pt; line-height: 1.4; color: #333; }}

            .header-table {{ width: 100%; margin-bottom: 10px; }}
            .photo-cell {{ width: 120px; vertical-align: middle; }}
            .info-cell {{ vertical-align: middle; padding-left: 20px; }}

            .profile-pic {{
                width: 100px; height: 100px; object-fit: cover;
                border-radius: 12px; border: 2px solid #e0e0e0;
            }}

            h1 {{ font-size: 22pt; margin: 0; color: #1a365d; text-transform: uppercase; }}
            .contact-info {{ color: #666; font-size: 9pt; margin-top: 5px; }}
            .header-line {{ border: 0; border-bottom: 2px solid #2c3e50; margin: 10px 0 20px 0; }}

            h2 {{ font-size: 12pt; color: #2980b9; border-bottom: 1px solid #ddd; margin-top: 15px; text-transform: uppercase; }}

            .job-header {{ width: 100%; margin-bottom: 2px; }}
            .job-title {{ text-align: left; }}
            .job-date {{ text-align: right; font-weight: bold; color: #555; }}

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


# --- MAIN ROUTE ---
@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        job_post = request.form.get("job-post")
        text_input = request.form.get("user-data")
        user_pdf = request.files.get('user-pdf')
        profile_pic = request.files.get('profile-pic')

        # 1. Text Extraction
        final_candidate_data = ""
        if user_pdf and user_pdf.filename != '':
            extracted_text = extract_pdf_text(user_pdf)
            if extracted_text: final_candidate_data = extracted_text
        if not final_candidate_data: final_candidate_data = text_input

        # 2. Image Handling
        photo_path_html = ""
        if profile_pic and profile_pic.filename != '':
            filename = f"photo_{uuid.uuid4().hex}.jpg"
            abs_path = os.path.join(os.getcwd(), PHOTOS_FOLDER, filename)
            profile_pic.save(abs_path)
            photo_path_html = abs_path

        if not final_candidate_data or not job_post:
            return render_template("index.html", error="Please provide candidate data and a job post.")

        try:
            # 3. Generate Content (Using OpenRouter)
            resume_html = generate_resume_content(final_candidate_data, job_post)

            # 4. Inject Image
            if photo_path_html:
                resume_html = resume_html.replace('[[PROFILE_PHOTO]]', photo_path_html)
            else:
                # Remove the image tag if no photo uploaded
                resume_html = resume_html.replace('<img src="[[PROFILE_PHOTO]]" class="profile-pic" />', '')

            # 5. Create PDF
            unique_filename = f"resume_{uuid.uuid4().hex}.pdf"
            file_path = os.path.join(PDF_FOLDER, unique_filename)
            create_pdf(resume_html, file_path)

            return render_template("index.html", filename=unique_filename)

        except Exception as e:
            return render_template("index.html", error=f"Error: {str(e)}")

    return render_template("index.html")


if __name__ == "__main__":
    app.run(debug=True)