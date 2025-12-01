import os
import uuid  # For generating unique filenames
from flask import Flask, render_template, request, send_file
from dotenv import load_dotenv
import google.generativeai as genai
import markdown
from xhtml2pdf import pisa

app = Flask(__name__)

# Ensure a folder exists to store the PDFs
PDF_FOLDER = 'static/resumes'
os.makedirs(PDF_FOLDER, exist_ok=True)

# --- CONFIG ---
load_dotenv()
api_key = os.getenv("GOOGLE_API_KEY")
if not api_key:
    raise ValueError("Error: GOOGLE_API_KEY environment variable not set.")
genai.configure(api_key=api_key)


# --- AI GENERATION ---
def generate_resume_content(data_me, job):
    template = f"""
    Act as a Senior Technical Recruiter optimizing a resume for a specific job opening.

    INPUTS:
    - Candidate Profile: {data_me}
    - Target Job: {job}

    INSTRUCTIONS:
    1. Write a Professional Summary mirroring the job requirements.
    2. Re-order Experience bullet points t  o prioritize relevant skills.
    3. Ensure the Skills section is ordered by relevance.
    4. Use clean Markdown (## for Headers, * for bullets, ** for bold).
    5. Do not use LaTeX formatting (like $text$) for standard business terms like A/B testing.

    CRITICAL OUTPUT RULES:
    - Output **ONLY** the resume content.
    - Start immediately with the Candidate's Name as a top-level header (# Name).
    """
    model = genai.GenerativeModel('gemini-2.0-flash')
    response = model.generate_content(template)
    return response.text


# --- PDF GENERATION ---
def create_pdf(markdown_content, output_path):
    # Convert Markdown to HTML
    html_body = markdown.markdown(markdown_content)

    # Add CSS Styles
    full_html = f"""
    <html>
    <head>
        <style>
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 12px; line-height: 1.4; color: #333; }}
            h1 {{ font-size: 22px; color: #2c3e50; border-bottom: 2px solid #2c3e50; padding-bottom: 5px; }}
            h2 {{ font-size: 16px; color: #2980b9; margin-top: 15px; border-bottom: 1px solid #ddd; }}
            ul {{ padding-left: 20px; }}
            li {{ margin-bottom: 4px; }}
        </style>
    </head>
    <body>
        {html_body}
    </body>
    </html>
    """

    # Generate PDF
    with open(output_path, "wb") as result_file:
        pisa_status = pisa.CreatePDF(src=full_html, dest=result_file)

    return not pisa_status.err


# --- ROUTES ---

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # 1. Get Data from Form
        data_me = request.form.get("user-data")
        job_post = request.form.get("job-post")

        if not data_me or not job_post:
            return render_template("index.html", error="Please fill in both fields.")

        # 2. Generate AI Content
        print("Generating Resume content...")
        resume_md = generate_resume_content(data_me, job_post)

        # 3. Create Unique Filename
        unique_filename = f"resume_{uuid.uuid4().hex}.pdf"
        file_path = os.path.join(PDF_FOLDER, unique_filename)

        # 4. Save PDF
        print(f"Saving PDF to {file_path}...")
        create_pdf(resume_md, file_path)

        # 5. Render the page again, but pass the filename so we can show the download button
        return render_template("index.html", filename=unique_filename)

    # GET Request (First visit)
    return render_template("index.html")


@app.route("/download/<filename>")
def download_pdf(filename):
    # This allows the user to download the specific file
    path = os.path.join(PDF_FOLDER, filename)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)