from io import BytesIO #to save files in ram rather than disk
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
# 1.this function writes to memory instead of a file
def create_pdf(markdown_content):
    html_body = markdown.markdown(markdown_content)
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

    # Create an in-memory "file"
    pdf_file = BytesIO()

    # Write PDF to that memory location
    pisa_status = pisa.CreatePDF(src=full_html, dest=pdf_file)

    if pisa_status.err:
        return None

    # Rewind the "cursor" to the beginning of the file so it can be read
    pdf_file.seek(0)
    return pdf_file

# --- ROUTES ---

@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        # ... (keep your existing input/file handling logic here) ...

        # --- CHANGED LOGIC START ---
        try:
            resume_md = generate_resume_content(data_me, job_post)

            # Generate PDF in memory
            pdf_file = create_pdf(resume_md)

            if pdf_file:
                # Send directly to user as a download
                return send_file(
                    pdf_file,
                    as_attachment=True,
                    download_name=f"resume_{uuid.uuid4().hex}.pdf",
                    mimetype='application/pdf'
                )
            else:
                return render_template("index.html", error="Error generating PDF")

        except Exception as e:
            return render_template("index.html", error=f"An error occurred: {str(e)}")
        # --- CHANGED LOGIC END ---

    return render_template("index.html")


@app.route("/download/<filename>")
def download_pdf(filename):
    # This allows the user to download the specific file
    path = os.path.join(PDF_FOLDER, filename)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)