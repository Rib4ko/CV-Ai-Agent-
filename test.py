import os
import uuid
import re
from flask import Flask, render_template, request, send_file
from dotenv import load_dotenv
import markdown
from xhtml2pdf import pisa
import openai  # Changed from anthropic

app = Flask(__name__)
load_dotenv()

# --- CONFIG ---
# Using the MiniMax credentials we set up earlier
client = openai.OpenAI(
    api_key=os.getenv("MINIMAX_API_KEY"),
    base_url=os.getenv("MINIMAX_BASE_URL")
)

# Ensure folders exist
PDF_FOLDER = 'static/resumes'
os.makedirs(PDF_FOLDER, exist_ok=True)


def generate_resume_content(data_me, job , language_choice):
    response = client.chat.completions.create(
        model="MiniMax-M2",  # Ensure this matches your provider's model string
        messages=[
            {
                "role": "system",
                "content": "You are a Senior Technical Recruiter. Output ONLY clean Markdown. No preamble."
            },
            {
                "role": "user",
                "content": f"""
                INPUTS:
                - Candidate Profile: {data_me}
                - Target Job: {job}
                -language: {language_choice}

                INSTRUCTIONS:
                1. Write a Professional Summary mirroring the job requirements.
                2. Re-order Experience bullet points to prioritize relevant skills.
                3. Use clean Markdown (## for Headers, * for bullets, ** for bold).

                CRITICAL: Start immediately with '# Name'.
                """
            }
        ],
        temperature=0.7
    )

    raw_content = response.choices[0].message.content

    # --- STRIP THINKING TAGS ---
    # This prevents the AI's internal thoughts from being printed in the PDF
    clean_content = re.sub(r'<think>.*?</think>', '', raw_content, flags=re.DOTALL).strip()

    return clean_content


def create_pdf(markdown_content, output_path):
    # Convert Markdown to HTML
    html_body = markdown.markdown(markdown_content)

    # Add Professional CSS for PDF
    full_html = f"""
    <html>
    <head>
        <meta charset="UTF-8">
        <style>
            @page {{ margin: 1in; }}
            body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11pt; line-height: 1.5; color: #333; }}
            h1 {{ font-size: 24pt; color: #1a365d; text-align: center; text-transform: uppercase; margin-bottom: 0; }}
            h2 {{ font-size: 14pt; color: #2d3748; border-bottom: 1px solid #cbd5e0; margin-top: 20px; padding-bottom: 2px; }}
            ul {{ padding-left: 18px; }}
            li {{ margin-bottom: 5px; }}
            strong {{ color: #2d3748; }}
        </style>
    </head>
    <body>
        {html_body}
    </body>
    </html>
    """

    with open(output_path, "wb") as result_file:
        pisa_status = pisa.CreatePDF(src=full_html, dest=result_file)

    return not pisa_status.err


@app.route("/", methods=["GET", "POST"])
def index():
    if request.method == "POST":
        data_me = request.form.get("user-data")
        job_post = request.form.get("job-post")
        language_choice = request.form.get("language")
        if not data_me or not job_post:
            return render_template("index.html", error="Please fill in both fields.")

        try:
            # 1. AI Generation
            resume_md = generate_resume_content(data_me, job_post ,language_choice )

            # 2. PDF Creation
            unique_filename = f"resume_{uuid.uuid4().hex}.pdf"
            file_path = os.path.join(PDF_FOLDER, unique_filename)

            success = create_pdf(resume_md, file_path)

            if success:
                return render_template("index.html", filename=unique_filename)
            else:
                return render_template("index.html", error="PDF Generation failed.")
        except Exception as e:
            return render_template("index.html", error=f"AI Error: {str(e)}")

    return render_template("index.html")


@app.route("/download/<filename>")
def download_pdf(filename):
    path = os.path.join(PDF_FOLDER, filename)
    return send_file(path, as_attachment=True)


if __name__ == "__main__":
    app.run(debug=True)