import os
import json
import math
import random
import re
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from pathlib import Path
from dotenv import load_dotenv
from groq import Groq

import pdfplumber
import plotly.express as px
import streamlit as st
from docx import Document

load_dotenv()

# Initialize Groq client
client = Groq(api_key=os.environ.get("GROQ_API_KEY"))
try:
    import pytesseract
except ImportError:
    pytesseract = None
try:
    from pdf2image import convert_from_bytes
except ImportError:
    convert_from_bytes = None

from utils.storage import (
    authenticate_user,
    init_db,
    load_attempts,
    load_questions,
    register_user,
    save_attempt,
    save_questions,
)
from analytics import render_dashboard

APP_TITLE = "SmartQuizzer Pro"
STOPWORDS = {
    "the", "is", "are", "was", "were", "this", "that", "these", "those", "from", "into",
    "with", "for", "and", "but", "about", "over", "under", "between", "during", "through",
    "have", "has", "had", "can", "could", "will", "would", "should", "may", "might", "must",
    "a", "an", "of", "to", "in", "on", "at", "by", "as", "it", "its", "be", "or", "if",
    "than", "then", "there", "their", "them", "they", "you", "your", "we", "our", "he", "she"
}

HAS_OCR = pytesseract is not None and convert_from_bytes is not None


def play_sound(sound_type):
    """Injects HTML to play sounds based on event."""
    sounds = {
        "correct": "https://www.soundjay.com/buttons/sounds/button-3.mp3",
        "wrong": "https://www.soundjay.com/buttons/sounds/button-10.mp3",
        "finish": "https://www.soundjay.com/misc/sounds/bell-ringing-05.mp3",
    }
    url = sounds.get(sound_type)
    if url:
        st.markdown(
            f'<audio autoplay><source src="{url}" type="audio/mpeg"></audio>',
            unsafe_allow_html=True
        )


def show_confetti():
    """Triggers confetti animation using JavaScript."""
    st.markdown(
        """
        <script src="https://cdn.jsdelivr.net/npm/canvas-confetti@1.5.1/dist/confetti.browser.min.js"></script>
        <script>
            confetti({
                particleCount: 150,
                spread: 70,
                origin: { y: 0.6 },
                colors: ['#006d77', '#0a9396', '#94d2bd', '#e9d8a6']
            });
        </script>
        """,
        unsafe_allow_html=True
    )


def normalize_text(text):
    return re.sub(r"\s+", " ", text).strip()


def split_sentences(text):
    cleaned = normalize_text(text)
    if not cleaned:
        return []
    parts = re.split(r"(?<=[.!?])\s+", cleaned)
    return [p.strip() for p in parts if len(p.split()) >= 6]


def extract_keywords(text, top_k=80):
    words = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text.lower())
    words = [word for word in words if word not in STOPWORDS]
    return [item[0] for item in Counter(words).most_common(top_k)]





def text_from_pdf(file_bytes):
    result = []
    
    with pdfplumber.open(BytesIO(file_bytes)) as pdf:
        for page in pdf.pages:
            # 1. Extract standard selectable text
            page_text = page.extract_text() or ""
            
            # 2. If OCR is available, hunt for images embedded on this specific page
            if HAS_OCR:
                for img in page.images:
                    try:
                        # Get the bounding box coordinates of the embedded image
                        bbox = (img["x0"], img["top"], img["x1"], img["bottom"])
                        
                        # Crop the page to just that image and convert it to a PIL image
                        image_crop = page.crop(bbox).to_image(resolution=200).original
                        
                        # Run OCR specifically on that cropped image
                        ocr_text = pytesseract.image_to_string(image_crop)
                        
                        if ocr_text.strip():
                            # Append it cleanly so the AI knows it came from a graphic
                            page_text += f"\n[Text from Graphic/Image]: {ocr_text.strip()}\n"
                    except Exception as e:
                        # Silently skip if the image is corrupted or coordinates are off-page
                        continue

            # 3. Handle the edge case where the WHOLE page is a single scanned image
            # (If standard text extraction found nothing, OCR the full page)
            if not page_text.strip() and HAS_OCR:
                try:
                    full_page_img = page.to_image(resolution=200).original
                    page_text = pytesseract.image_to_string(full_page_img)
                except Exception:
                    pass

            if page_text.strip():
                result.append(page_text)
                
    return "\n".join(result).strip()


def text_from_docx(file_bytes):
    doc = Document(BytesIO(file_bytes))
    lines = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    return "\n".join(lines)





def pick_answer_token(sentence):
    tokens = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", sentence)
    filtered = [t for t in tokens if t.lower() not in STOPWORDS]
    if not filtered:
        return None
    filtered.sort(key=len, reverse=True)
    return filtered[0]


def sentence_pool(sentences, difficulty):
    if difficulty == "Easy":
        return sentences[:]
    if difficulty == "Medium":
        return [s for s in sentences if 10 <= len(s.split()) <= 26] or sentences
    return [s for s in sentences if len(s.split()) >= 14] or sentences


def build_mcq(sentence, keyword_bank, difficulty):
    answer = pick_answer_token(sentence)
    if not answer:
        return None

    prompt = re.sub(
        rf"\b{re.escape(answer)}\b",
        "_____",
        sentence,
        count=1,
        flags=re.IGNORECASE
    )

    distractors = [w.title() for w in keyword_bank if w.lower() != answer.lower()]
    random.shuffle(distractors)

    options = [answer] + distractors[:3]

    while len(options) < 4:
        options.append(f"Option {len(options) + 1}")

    random.shuffle(options)

    return {
        "question": f"Fill in the blank: {prompt}",
        "options": options[:4],
        "answer": answer,
        "type": "MCQ",
        "difficulty": difficulty.lower(),
    }

def build_true_false(sentence, keyword_bank, difficulty):
    answer = "True"
    statement = sentence
    flip = random.choice([True, False])
    if flip:
        token = pick_answer_token(sentence)
        replacement = next((w for w in keyword_bank if w.lower() != (token or "").lower()), None)
        if token and replacement:
            statement = re.sub(rf"\b{re.escape(token)}\b", replacement, sentence, count=1, flags=re.IGNORECASE)
            answer = "False"
    return {
        "question": f"True or False: {statement}",
        "options": ["True", "False"],
        "answer": answer,
        "type": "True/False",
        "difficulty": difficulty.lower(),
    }


    return {
        "question": f"Explain briefly: {sentence}",
        "options": [],
        "answer": sentence,
        "type": "Short Answer",
        "difficulty": difficulty.lower(),
    }

def chunk_text(text, max_chars=12000):
    """
    Splits text into chunks, respecting paragraph breaks where possible,
    to ensure we don't exceed API token limits.
    """
    paragraphs = text.split('\n')
    chunks = []
    current_chunk = ""
    
    for p in paragraphs:
        # If adding the next paragraph keeps us under the limit, add it
        if len(current_chunk) + len(p) < max_chars:
            current_chunk += p + "\n"
        else:
            # Otherwise, save the current chunk and start a new one
            if current_chunk:
                chunks.append(current_chunk.strip())
            
            # Edge case: What if a single paragraph is massive? Force split it.
            if len(p) > max_chars:
                for i in range(0, len(p), max_chars):
                    chunks.append(p[i:i+max_chars])
                current_chunk = ""
            else:
                current_chunk = p + "\n"
                
    if current_chunk:
        chunks.append(current_chunk.strip())
        
    return chunks


def generate_quiz(text, question_count, difficulty, question_type):
    """
    Generates quiz questions using the Groq API with chunking and rate-limit handling.
    """
    chunks = chunk_text(text, max_chars=12000) # Safe limit for ~6000 TPM
    all_questions = []
    
    # Figure out roughly how many questions we need per chunk to hit the user's total
    questions_per_chunk = math.ceil(question_count / len(chunks))
    
    for i, chunk in enumerate(chunks):
        # Stop if we already have enough questions
        if len(all_questions) >= question_count:
            break
            
        # Determine how many questions to ask for in this specific request
        requested_count = min(questions_per_chunk, question_count - len(all_questions))
        
        prompt = f"""
        You are an expert educator. Create a {requested_count}-question {question_type} quiz based on the text below.
        The difficulty level should be {difficulty}.

        Text:
        \"\"\"{chunk}\"\"\"

        Output the result strictly in the following JSON format. Ensure the key is "questions" and the value is a list of objects.
        {{
            "questions": [
                {{
                    "question": "The question text",
                    "options": ["Option 1", "Option 2", "Option 3", "Option 4"],
                    "answer": "The correct answer",
                    "type": "{question_type}",
                    "difficulty": "{difficulty.lower()}"
                }}
            ]
        }}
        """

        max_retries = 3
        for attempt in range(max_retries):
            try:
                response = client.chat.completions.create(
                    model="llama-3.1-8b-instant",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant designed to output strict JSON."},
                        {"role": "user", "content": prompt}
                    ],
                    response_format={"type": "json_object"},
                    temperature=0.3 
                )
                
                result_json = json.loads(response.choices[0].message.content)
                chunk_questions = result_json.get("questions", [])
                all_questions.extend(chunk_questions)
                
                # Success! Break out of the retry loop and move to the next chunk
                break 
                
            except Exception as e:
                error_msg = str(e).lower()
                # If we hit a rate limit (413 or 429), pause for 60 seconds
                if "rate_limit" in error_msg or "429" in error_msg or "413" in error_msg or "too large" in error_msg:
                    st.warning(f"⏳ API rate limit reached on chunk {i+1}/{len(chunks)}. Waiting 60 seconds to cool down... (Attempt {attempt+1}/{max_retries})")
                    time.sleep(60) 
                else:
                    st.error(f"Error generating quiz from chunk {i+1}: {e}")
                    break

    return all_questions[:question_count]


def evaluate_short_answer(question, correct_answer, user_answer):
    """
    Uses Groq to evaluate subjective short answers semantically.
    """
    prompt = f"""
    You are an educator grading a short answer question.
    Question: "{question}"
    Correct Answer/Rubric: "{correct_answer}"
    Student's Answer: "{user_answer}"
    
    Assess if the student's answer is correct based on the core meaning, even if phrased differently.
    Return JSON only: {{"is_correct": true/false, "feedback": "Short explanation of why"}}
    """
    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant", # Smaller, faster model is fine for grading
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        return {"is_correct": False, "feedback": f"Evaluation error: {str(e)}"}
    

def evaluate_answer(question_data, user_answer):
    """
    Evaluates a single user's answer against the question data.
    Returns True if correct, False otherwise.
    """
    # Handle cases where the user left the answer blank
    if user_answer is None:
        user_answer = ""
        
    user_ans = str(user_answer).strip()
    correct_ans = str(question_data.get("answer", ""))
    q_type = question_data.get("type", "")

    # Exact match logic for Objective questions
    if q_type in ["Multiple Choice Questions (MCQ)", "True/False", "MCQ"]:
        return user_ans.lower() == correct_ans.lower()
    
    # LLM Evaluation logic for Subjective questions
    elif q_type == "Short Answer":
        if not user_ans: # If blank, it's automatically wrong
            return False
            
        eval_result = evaluate_short_answer(
            question=question_data["question"],
            correct_answer=correct_ans,
            user_answer=user_ans
        )
        return eval_result.get("is_correct", False)
        
    return False


def extract_input_text(input_mode, typed_text, uploaded_file):
    if input_mode == "Paste Text":
        return normalize_text(typed_text), "Pasted Text"

    if uploaded_file is None:
        return "", ""

    file_bytes = uploaded_file.read()
    suffix = Path(uploaded_file.name).suffix.lower()
    source_name = uploaded_file.name

    if suffix == ".pdf":
        return normalize_text(text_from_pdf(file_bytes)), source_name
    if suffix == ".docx":
        return normalize_text(text_from_docx(file_bytes)), source_name
    return "", source_name


st.set_page_config(page_title=APP_TITLE, page_icon="🧠", layout="wide")
init_db()

if "quiz_submitted" not in st.session_state:
    st.session_state.quiz_submitted = False

if "answers" not in st.session_state:
    st.session_state.answers = {}

if "current_q" not in st.session_state:
    st.session_state.current_q = 0

if "auth_user" not in st.session_state:
    st.session_state.auth_user = None

if "landing_done" not in st.session_state:
    st.session_state.landing_done = False

if "auth_mode_choice" not in st.session_state:
    st.session_state.auth_mode_choice = "Login"

st.markdown(
    """
    <style>
        @import url('https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700;800&family=Fraunces:opsz,wght@9..144,600;9..144,700&display=swap');

        :root {
            --bg-a: #f4f6f8;
            --bg-b: #e8eef3;
            --ink: #10202a;
            --text: #1a2b35;
            --muted: #4a5f6b;
            --panel: #ffffff;
            --panel-soft: #f9fbfc;
            --line: #d7e0e6;
            --brand: #006d77;
            --brand-2: #0a9396;
            --brand-soft: rgba(0, 109, 119, 0.14);
            --success: #157347;
            --warn: #9a6700;
            --danger: #b42318;
            --radius-xl: 20px;
            --radius-lg: 14px;
            --radius-md: 10px;
            --shadow-sm: 0 8px 24px rgba(9, 30, 66, 0.08);
            --shadow-lg: 0 20px 45px rgba(9, 30, 66, 0.14);
        }

        html, body, [class*="css"] {
            font-family: 'Plus Jakarta Sans', sans-serif;
            color: var(--text);
        }

        .stApp {
            background: 
                linear-gradient(120deg, rgba(0, 109, 119, 0.05) 0%, rgba(10, 147, 150, 0.05) 100%),
                radial-gradient(at 0% 0%, rgba(0, 109, 119, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 0%, rgba(10, 147, 150, 0.1) 0px, transparent 50%),
                radial-gradient(at 100% 100%, rgba(0, 109, 119, 0.1) 0px, transparent 50%),
                radial-gradient(at 0% 100%, rgba(10, 147, 150, 0.1) 0px, transparent 50%),
                var(--bg-a);
            background-attachment: fixed;
            min-height: 100vh;
        }

        .main .block-container {
            max-width: 1200px;
            background: rgba(255, 255, 255, 0.65);
            backdrop-filter: blur(25px) saturate(180%);
            -webkit-backdrop-filter: blur(25px) saturate(180%);
            border: 1px solid rgba(255, 255, 255, 0.5);
            border-radius: var(--radius-xl);
            padding: clamp(1.2rem, 2.5vw, 2.5rem) clamp(1rem, 2.5vw, 2.8rem) !important;
            margin-top: 1.5rem;
            margin-bottom: 2rem;
            animation: fadeInScale 0.8s cubic-bezier(0.16, 1, 0.3, 1);
        }

        h1, h2, h3 { animation: fadeInDown 0.6s ease-out; }
        
        .card { 
            animation: simpleFade 1s ease-out;
            transition: all 0.3s cubic-bezier(0.175, 0.885, 0.32, 1.275) !important;
        }

        .stButton>button {
            transition: all 0.2s ease !important;
            border-radius: 12px !important;
        }
        .stButton>button:hover {
            transform: scale(1.02);
            box-shadow: 0 5px 15px rgba(0, 109, 119, 0.2);
        }

        @keyframes fadeInDown {
            from { opacity: 0; transform: translateY(-15px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes simpleFade {
            from { opacity: 0; }
            to { opacity: 1; }
        }
        @keyframes fadeInScale {
            from { opacity: 0; transform: scale(0.98) translateY(10px); }
            to { opacity: 1; transform: scale(1) translateY(0); }
        }

        .bounce-title {
            animation: bounce 2s infinite;
        }

        @keyframes bounce {
            0%, 20%, 50%, 80%, 100% {transform: translateY(0);}
            40% {transform: translateY(-10px);}
            60% {transform: translateY(-5px);}
        }

        .card {
            background: rgba(255, 255, 255, 0.75);
            backdrop-filter: blur(12px);
            border: 1px solid rgba(255, 255, 255, 0.8);
            border-radius: var(--radius-lg);
            padding: 1.8rem;
            box-shadow: 0 8px 30px rgba(0, 0, 0, 0.04);
            margin-bottom: 1.5rem;
            transition: all 0.4s cubic-bezier(0.23, 1, 0.32, 1);
            position: relative;
            overflow: hidden;
        }

        .card::before {
            content: '';
            position: absolute;
            top: 0; left: 0; width: 100%; height: 4px;
            background: linear-gradient(90deg, var(--brand), var(--brand-2));
            opacity: 0;
            transition: opacity 0.3s ease;
        }

        .card:hover {
            transform: translateY(-8px);
            box-shadow: 0 20px 40px rgba(0, 109, 119, 0.12);
            border-color: var(--brand-soft);
            background: rgba(255, 255, 255, 0.95);
        }

        .card:hover::before {
            opacity: 1;
        }

        .feature-card {
            border-left: 5px solid var(--brand);
            padding: 1.25rem !important;
            margin-bottom: 20px;
        }

        .feature-card h4 {
            margin: 0 0 8px 0 !important;
            color: var(--brand) !important;
            font-size: 1.15rem;
        }

        .feature-card p {
            margin: 0 !important;
            font-size: 0.92rem;
            color: var(--muted) !important;
        }

        h1, h2, h3, h4, h5, h6,
        [data-testid="stHeading"] {
            font-family: 'Fraunces', serif;
            color: var(--ink) !important;
            letter-spacing: 0.1px;
            line-height: 1.2;
        }

        h1 { font-size: clamp(1.85rem, 3vw, 2.7rem); }
        h2 { font-size: clamp(1.4rem, 2.25vw, 2.05rem); }
        h3 { font-size: clamp(1.2rem, 1.75vw, 1.55rem); }

        p, li, label, .stCaption, .stMarkdown, .stText {
            color: var(--text) !important;
            line-height: 1.55;
        }

        .stDivider {
            border-top: 1px solid var(--line);
            margin: 1rem 0 1.2rem;
        }

        div.stButton > button,
        div.stDownloadButton > button,
        div[data-testid="stFormSubmitButton"] > button {
            width: 100%;
            min-height: 46px;
            border-radius: 12px;
            border: 1px solid rgba(255, 255, 255, 0.18);
            background: linear-gradient(135deg, var(--brand), var(--brand-2));
            color: #f5ffff !important;
            font-size: 0.98rem;
            font-weight: 700;
            letter-spacing: 0.2px;
            box-shadow: 0 10px 22px rgba(0, 109, 119, 0.22);
            transition: transform .16s ease, box-shadow .16s ease, filter .16s ease;
        }

        div.stButton > button:hover,
        div.stDownloadButton > button:hover,
        div[data-testid="stFormSubmitButton"] > button:hover {
            transform: translateY(-1px);
            box-shadow: 0 13px 24px rgba(0, 109, 119, 0.28);
            filter: saturate(110%);
        }

        div.stButton > button:focus-visible,
        div.stDownloadButton > button:focus-visible,
        div[data-testid="stFormSubmitButton"] > button:focus-visible,
        .stTextInput > div > div > input:focus-visible,
        .stTextArea textarea:focus-visible {
            outline: 3px solid var(--brand-soft);
            outline-offset: 2px;
        }

        .stTextInput > div > div > input,
        .stTextArea textarea,
        .stNumberInput input,
        .stSelectbox > div > div,
        .stMultiSelect > div > div,
        [data-baseweb="select"] > div {
            background: #ffffff !important;
            border: 1px solid #c7d4dd !important;
            border-radius: var(--radius-md) !important;
            min-height: 44px;
            color: var(--ink) !important;
            -webkit-text-fill-color: var(--ink) !important;
        }

        .stTextArea textarea {
            min-height: 130px;
            background: #fcfeff !important;
        }

        .stTextInput input::placeholder,
        .stTextArea textarea::placeholder {
            color: #6f7f8a !important;
            opacity: 1;
        }

        .stTextInput > div > div > input:focus,
        .stTextArea textarea:focus,
        .stNumberInput input:focus,
        [data-baseweb="select"] > div:focus-within {
            border-color: #14919b !important;
            box-shadow: 0 0 0 4px var(--brand-soft) !important;
        }

        [data-testid="stWidgetLabel"],
        [data-testid="stRadio"] p,
        [role="radiogroup"] label,
        .stSlider label,
        .stFileUploader label,
        .stSelectbox label,
        .stTextInput label,
        .stTextArea label {
            color: var(--ink) !important;
            font-weight: 650;
        }

        [data-baseweb="select"] *,
        [data-baseweb="tag"] * {
            color: var(--ink) !important;
        }

        [data-baseweb="select"] [data-baseweb="menu"] li {
            color: var(--ink) !important;
            background: #ffffff !important;
        }

        [data-baseweb="select"] [data-baseweb="menu"] li:hover {
            background: #f0f4f7 !important;
        }

        [data-baseweb="select"] [data-baseweb="menu"] [data-baseweb="option"] {
            color: var(--ink) !important;
            background: #ffffff !important;
        }

        [data-baseweb="select"] [data-baseweb="menu"] [data-baseweb="option"]:hover {
            background: #f0f4f7 !important;
        }

        [data-baseweb="select"] [data-baseweb="menu"] {
            background: #ffffff !important;
            border: 1px solid #c7d4dd !important;
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1) !important;
        }

        [data-testid="stSidebar"] {
            background: #ffffff;
            border-right: 1px solid var(--line);
            box-shadow: 10px 0 30px rgba(0, 0, 0, 0.02);
        }

        [data-testid="stSidebar"] .block-container {
            background: transparent;
            border: none;
            box-shadow: none;
            padding: 2rem 1.2rem !important;
        }

        [data-testid="stSidebar"] * {
            color: var(--text) !important;
        }

        .user-pill {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 0.5rem 1rem;
            border-radius: 12px;
            background: linear-gradient(135deg, #006d77, #0a9396);
            color: white !important;
            font-weight: 700;
            font-size: 0.9rem;
            box-shadow: 0 4px 12px rgba(0, 109, 119, 0.2);
            margin: 1rem 0;
        }

        [data-testid="stMetric"] {
            background: var(--panel-soft);
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            padding: 0.65rem 0.8rem;
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, 0.8);
        }

        [data-testid="stMetricLabel"],
        [data-testid="stMetricValue"] {
            color: var(--ink) !important;
        }

        [data-testid="stExpander"] {
            background: #ffffff;
            border: 1px solid var(--line);
            border-radius: var(--radius-lg);
            overflow: hidden;
        }

        .stProgress > div > div > div > div {
            background: linear-gradient(90deg, #36b3a8, var(--brand));
        }


        .leaderboard-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 0.4rem 0.6rem;
            border-bottom: 1px solid var(--line);
        }

        .leaderboard-name {
            font-weight: 600;
            color: var(--ink);
        }

        .leaderboard-score {
            font-weight: 700;
            color: var(--brand);
        }

        .score-card {
            background: linear-gradient(135deg, #006d77 0%, #0a9396 100%);
            color: white !important;
            padding: 2.5rem;
            border-radius: 20px;
            text-align: center;
            box-shadow: 0 15px 35px rgba(0, 109, 119, 0.3);
            margin-bottom: 2rem;
            animation: slideUp 0.6s ease-out;
        }

        @keyframes slideUp {
            from { opacity: 0; transform: translateY(30px); }
            to { opacity: 1; transform: translateY(0); }
        }

        .score-card h1 { color: white !important; font-size: 3.5rem; margin: 0; }
        .score-card p { color: rgba(255,255,255,0.9) !important; font-size: 1.2rem; margin-top: 0.5rem; }
        .score-card .stars { font-size: 2rem; margin-bottom: 1rem; color: #ffca28; }

        .stAlert {
            border-radius: var(--radius-md);
            border: 1px solid var(--line);
        }

        .stSuccess { color: var(--success) !important; }
        .stWarning { color: var(--warn) !important; }
        .stError { color: var(--danger) !important; }

        [data-testid="stHorizontalBlock"] {
            gap: clamp(0.6rem, 1.4vw, 1rem);
        }

        [data-testid="stRadio"] [role="radiogroup"] {
            display: flex;
            gap: 0.45rem;
            flex-wrap: wrap;
        }

        [data-testid="stRadio"] [role="radiogroup"] > label {
            background: #f6fafb;
            border: 1px solid #d1dde4;
            border-radius: 999px;
            padding: 0.2rem 0.65rem;
        }

        @media (max-width: 1080px) {
            .main .block-container {
                border-radius: 14px;
                padding: 1rem 1rem 1.25rem !important;
            }
        }

        @media (max-width: 860px) {
            .main .block-container {
                max-width: 100%;
                border-radius: 12px;
            }

            [data-testid="stHorizontalBlock"] {
                display: flex;
                flex-direction: column;
                gap: 0.7rem;
            }

            [data-testid="column"] {
                width: 100% !important;
                flex: 1 1 100% !important;
                min-width: 100% !important;
            }

            div.stButton > button,
            div.stDownloadButton > button,
            div[data-testid="stFormSubmitButton"] > button {
                min-height: 44px;
                font-size: 0.95rem;
            }
        }

        @media (max-width: 640px) {
            .main .block-container {
                padding: 0.85rem 0.7rem 1rem !important;
                border-left: none;
                border-right: none;
                box-shadow: none;
            }

            h1 { font-size: 1.55rem; }
            h2 { font-size: 1.25rem; }
            h3 { font-size: 1.08rem; }
        }

        /* Interactive Sidebar Nav */
        div[data-testid="stSidebar"] [data-testid="stButton"] button {
            background: transparent !important;
            color: var(--text) !important;
            border: 1px solid transparent !important;
            box-shadow: none !important;
            text-align: left !important;
            justify-content: flex-start !important;
            padding: 10px 15px !important;
            font-weight: 600 !important;
            border-radius: 12px !important;
            transition: all 0.3s ease !important;
            width: 100% !important;
        }

        div[data-testid="stSidebar"] [data-testid="stButton"] button:hover {
            background: var(--brand-soft) !important;
            color: var(--brand) !important;
            transform: translateX(5px) !important;
        }

        /* We will inject a special class or use a data attribute if possible, 
           but since streamlit doesn't allow it, we style the "Active" one 
           differently by checking session state and conditional rendering 
           is hard with CSS alone. So we use a divider or highlight in the button label. */

    </style>
    """,
    unsafe_allow_html=True,
)



if st.session_state.auth_user is None:
    if not st.session_state.landing_done:
        # Immersive Hero Section
        st.markdown(
            """
            <div class="hero-container">
                <div class="hero-content">
                    <div class="hero-badge">✨ VERSION 2.0 IS LIVE</div>
                    <h1 class="hero-title">SmartQuizzer <span class="hero-accent">Pro</span></h1>
                    <p class="hero-subtitle">The Intelligent Learning Platform for Masterful Knowledge Retention.</p>
                    <div class="hero-stats">
                        <div class="stat-item"><b>AI</b> Powered</div>
                        <div class="stat-separator">/</div>
                        <div class="stat-item"><b>Pro</b> Analytics</div>
                        <div class="stat-separator">/</div>
                        <div class="stat-item"><b>Secure</b> Access</div>
                    </div>
                    <div class="scroll-indicator">
                        <p>EXPLORE PLATFORM</p>
                        <div class="chevron">↓</div>
                    </div>
                </div>
            </div>
            
            <style>
                .hero-container {
                    height: 90vh;
                    display: flex;
                    flex-direction: column;
                    align-items: center;
                    justify-content: center;
                    text-align: center;
                    position: relative;
                    margin: -1.5rem 0 2rem 0;
                    border-radius: 40px;
                    overflow: hidden;
                    background: #ffffff;
                }

                /* Animated Mesh Background */
                .hero-container::before {
                    content: '';
                    position: absolute;
                    top: -50%; left: -50%;
                    width: 200%; height: 200%;
                    background: 
                        radial-gradient(circle at 75% 25%, rgba(0, 109, 119, 0.1) 0%, transparent 40%),
                        radial-gradient(circle at 25% 75%, rgba(10, 147, 150, 0.08) 0%, transparent 40%),
                        radial-gradient(circle at 50% 50%, rgba(148, 210, 189, 0.1) 0%, transparent 50%);
                    animation: meshMove 20s infinite alternate linear;
                    z-index: 1;
                }

                @keyframes meshMove {
                    0% { transform: translate(0, 0) rotate(0deg); }
                    100% { transform: translate(5%, 5%) rotate(5deg); }
                }

                .hero-content {
                    position: relative;
                    z-index: 2;
                    padding: 3rem;
                    max-width: 900px;
                }

                .hero-badge {
                    display: inline-block;
                    padding: 6px 16px;
                    background: var(--brand-soft);
                    color: var(--brand);
                    font-size: 0.75rem;
                    font-weight: 800;
                    border-radius: 99px;
                    letter-spacing: 1.5px;
                    margin-bottom: 2rem;
                    animation: fadeInUp 0.8s ease-out;
                }

                .hero-title {
                    font-size: clamp(3.5rem, 8vw, 5.8rem) !important;
                    margin: 0 !important;
                    font-weight: 800 !important;
                    letter-spacing: -2px !important;
                    line-height: 1 !important;
                    animation: fadeInUp 1s cubic-bezier(0.16, 1, 0.3, 1) both;
                }

                .hero-accent {
                    background: linear-gradient(135deg, var(--brand), var(--brand-2));
                    -webkit-background-clip: text;
                    -webkit-text-fill-color: transparent;
                    font-weight: 300;
                }

                .hero-subtitle {
                    font-size: clamp(1.1rem, 2vw, 1.45rem) !important;
                    color: var(--muted) !important;
                    font-weight: 500 !important;
                    margin-top: 1.5rem !important;
                    animation: fadeInUp 1s 0.2s cubic-bezier(0.16, 1, 0.3, 1) both;
                }

                .hero-stats {
                    display: flex;
                    justify-content: center;
                    gap: 15px;
                    margin-top: 3rem;
                    color: var(--text);
                    font-size: 0.9rem;
                    animation: fadeInUp 1s 0.4s cubic-bezier(0.16, 1, 0.3, 1) both;
                }

                .stat-separator { opacity: 0.3; }

                .scroll-indicator {
                    margin-top: 6rem;
                    opacity: 0.6;
                    animation: bounce 2s infinite;
                }

                .scroll-indicator p {
                    font-size: 0.75rem;
                    font-weight: 800;
                    color: var(--brand);
                    letter-spacing: 3px;
                }

                .chevron {
                    font-size: 2rem;
                    color: var(--brand-2);
                    margin-top: -10px;
                }

                @keyframes fadeInUp {
                    from { opacity: 0; transform: translateY(30px); }
                    to { opacity: 1; transform: translateY(0); }
                }

                .main .block-container {
                    background: transparent !important;
                    box-shadow: none !important;
                    border: none !important;
                    padding-top: 0 !important;
                }
            </style>
            """,
            unsafe_allow_html=True
        )
        
        st.divider()
        
        # Login/Register Selection Area
        # Dashboard Options with Visual Impact
        st.markdown("<div style='height: 5vh;'></div>", unsafe_allow_html=True)
        
        btn_col1, btn_col2 = st.columns(2)
        with btn_col1:
            st.markdown("""
                <div style='text-align: center; margin-bottom: 1.5rem;'>
                    <h3 style='margin-bottom: 0.5rem;'>Returning Master?</h3>
                    <p style='color: var(--muted); font-size: 0.9rem;'>Pick up where you left off</p>
                </div>
            """, unsafe_allow_html=True)
            if st.button("🚀 Access Your Dashboard", type="primary", use_container_width=True):
                st.session_state.auth_mode_choice = "Login"
                st.session_state.landing_done = True
                st.rerun()
        with btn_col2:
            st.markdown("""
                <div style='text-align: center; margin-bottom: 1.5rem;'>
                    <h3 style='margin-bottom: 0.5rem;'>New Scholar?</h3>
                    <p style='color: var(--muted); font-size: 0.9rem;'>Start your journey today</p>
                </div>
            """, unsafe_allow_html=True)
            if st.button("✨ Initialize Account", use_container_width=True):
                st.session_state.auth_mode_choice = "Register"
                st.session_state.landing_done = True
                st.rerun()

        # Premium Footer
        st.markdown("""
            <div style='margin-top: 10rem; text-align: center; padding: 4rem 2rem; border-top: 1px solid var(--line);'>
                <p style='font-weight: 800; color: var(--brand); letter-spacing: 2px; font-size: 0.75rem; margin-bottom: 1rem;'>JOIN THE FUTURE OF LEARNING</p>
                <div style='display: flex; justify-content: center; gap: 20px; font-size: 1.5rem; opacity: 0.5; margin-bottom: 2rem;'>
                    <span>🧪</span><span>🎯</span><span>📈</span><span>🔐</span>
                </div>
                <p style='color: var(--muted); font-size: 0.85rem;'>© 2024 SmartQuizzer Pro. All rights reserved.</p>
                <p style='color: var(--muted); font-size: 0.75rem; margin-top: 0.5rem;'>Empowering students through AI-driven education.</p>
            </div>
        """, unsafe_allow_html=True)

        # Feature Showcase integrated into landing page
        st.markdown("<div style='height: 15vh;'></div>", unsafe_allow_html=True)
        st.markdown("<h3 style='text-align: center; margin-bottom: 2rem;'>🚀 Core Platform Features</h3>", unsafe_allow_html=True)
        feat_col1, feat_col2 = st.columns(2)
        with feat_col1:
            st.markdown("""
                <div class='card feature-card'>
                    <h4>🧠 Smart Extraction</h4>
                    <p>Upload PDF, DOCX, or even Scanned Images. Our AI handles the heavy lifting of reading and understanding your material.</p>
                </div>
                <div class='card feature-card'>
                    <h4>📊 Interactive Analytics</h4>
                    <p>Track your accuracy, speed, and topic-wise strengths with our pro-level dashboard.</p>
                </div>
            """, unsafe_allow_html=True)
        with feat_col2:
            st.markdown("""
                <div class='card feature-card'>
                    <h4>👨‍🏫 Teacher Mode</h4>
                    <p>Effortlessly create assessment materials and interactive quizzes for students based on lesson plans.</p>
                </div>
                <div class='card feature-card'>
                    <h4>⚡ Instant Feedback</h4>
                    <p>Receive immediate scoring and explanations. Identify your weak points instantly.</p>
                </div>
            """, unsafe_allow_html=True)
        
        st.stop()

    # If landing is done, show the actual Login/Register form
    if st.button("← Back to Home", use_container_width=False):
        st.session_state.landing_done = False
        st.rerun()
    # Re-using the existing logic but respecting the choice
    auth_mode = st.radio("Access Mode", ["Login", "Register"], 
                         index=0 if st.session_state.auth_mode_choice == "Login" else 1,
                         horizontal=True, label_visibility="collapsed")
    
    if auth_mode == "Login":
        with st.form("login_form", clear_on_submit=False):
            login_username = st.text_input("Username", key="login_username")
            login_password = st.text_input("Password", type="password", key="login_password")
            login_submit = st.form_submit_button("Login", use_container_width=True)
            if login_submit:
                ok, db_username = authenticate_user(login_username, login_password)
                if ok:
                    st.session_state.auth_user = db_username
                    st.success("Login successful.")
                    st.rerun()
                else:
                    st.error("Invalid username or password.")
    else:
        with st.form("register_form", clear_on_submit=False):
            st.subheader("Create New Account")
            reg_username = st.text_input("Username", key="register_username")
            reg_password = st.text_input("Password", type="password", key="register_password")
            reg_confirm = st.text_input("Confirm Password", type="password", key="register_confirm")
            
            register_submit = st.form_submit_button("Register", use_container_width=True)
            
            if register_submit:
                if reg_password != reg_confirm:
                    st.error("Passwords do not match.")
                elif not reg_username.strip():
                    st.error("Username cannot be empty.")
                else:
                    ok, message = register_user(reg_username, reg_password, None)
                    if ok:
                        st.success("Registration successful! You can now log in.")
                        st.session_state.auth_mode_choice = "Login"
                        st.rerun()
                    else:
                        st.error(message)

    # 🚀 Interactive Feature Showcase
    st.divider()
    st.markdown("<h3 style='text-align: center; margin-bottom: 2rem;'>🚀 Core Platform Features</h3>", unsafe_allow_html=True)
    
    feat_col1, feat_col2 = st.columns(2)
    with feat_col1:
        st.markdown("""
            <div class='card feature-card'>
                <h4>🧠 Smart Extraction</h4>
                <p>Upload PDF, DOCX, or even Scanned Images. Our AI handles the heavy lifting of reading and understanding your material.</p>
            </div>
            <div class='card feature-card'>
                <h4>📊 Interactive Analytics</h4>
                <p>Track your accuracy, speed, and topic-wise strengths with our pro-level dashboard visualizing every attempt.</p>
            </div>
        """, unsafe_allow_html=True)
    
    with feat_col2:
        st.markdown("""
            <div class='card feature-card'>
                <h4>👨‍🏫 Teacher Mode</h4>
                <p>Effortlessly create assessment materials and interactive quizzes for students based on lesson plans.</p>
            </div>
            <div class='card feature-card'>
                <h4>⚡ Instant Feedback</h4>
                <p>Receive immediate scoring and explanations. Identify your weak points instantly and refine your knowledge.</p>
            </div>
        """, unsafe_allow_html=True)

    st.stop()

if "menu_selection" not in st.session_state:
    st.session_state.menu_selection = "Home"

menu_opts = {
    "Home": "🏠",
    "Generate Quiz": "📄",
    "Take Quiz": "🧠",
    "Analytics Dashboard": "📊"
}

st.sidebar.markdown("### 🧭 Navigation")
for opt, icon in menu_opts.items():
    is_active = st.session_state.menu_selection == opt
    label = f"{icon} {opt}"
    
    # Active item styling logic
    if is_active:
        st.sidebar.markdown(f"""
            <style>
                div[data-testid="stSidebar"] [data-testid="stButton"] button:has(div:contains('{label}')) {{
                    background: linear-gradient(135deg, var(--brand), var(--brand-2)) !important;
                    color: white !important;
                    box-shadow: 0 4px 15px rgba(0, 109, 119, 0.2) !important;
                }}
            </style>
        """, unsafe_allow_html=True)
    
    if st.sidebar.button(label, key=f"nav_{opt}", use_container_width=True):
        st.session_state.menu_selection = opt
        st.rerun()

menu = st.session_state.menu_selection
candidate = st.session_state.auth_user
history = load_attempts(limit=200, user_name=candidate)

with st.sidebar:
    st.markdown(f"**Logged in as:** <span class='user-pill'>{candidate}</span>", unsafe_allow_html=True)
    if st.button("Logout", use_container_width=True):
        st.session_state.auth_user = None
        st.session_state.answers = {}
        st.session_state.landing_done = False
        st.rerun()
    st.markdown("### Performance Snapshot")
    tests_taken = history.get("tests_taken", 0)
    if history.get("percentages"):
        p_list = history["percentages"]
        avg_v = float(sum(p_list)) / len(p_list)
        avg_accuracy = f"{avg_v:.1f}"
    else:
        avg_accuracy = "0.0"
    st.metric("Attempts", tests_taken)
    st.metric("Average Accuracy", f"{avg_accuracy}%")

if menu == "Home":
    st.markdown(f"""
        <div style='display: flex; align-items: center; gap: 20px; margin-bottom: 2rem;'>
            <div style='font-size: 3rem;'>👋</div>
            <div>
                <h1 style='margin:0;'>Welcome, {candidate}!</h1>
                <p style='color: var(--muted); font-size: 1.1rem;'>Ready for today's learning challenge?</p>
            </div>
        </div>
    """, unsafe_allow_html=True)
    
    # Hero Summary Card
    st.markdown(f"""
        <div class="score-card" style="padding: 2rem; background: linear-gradient(135deg, #006d77, #0a9396); border-radius: 30px;">
            <div style="display: flex; justify-content: space-between; align-items: center;">
                <div style="text-align: left;">
                    <p style="text-transform: uppercase; letter-spacing: 2px; font-size: 0.9rem; opacity: 0.9; color: white !important;">Overall Performance</p>
                    <h1 style="font-size: 4rem; color: white !important;">{avg_accuracy}%</h1>
                    <p style="color: rgba(255,255,255,0.8) !important;">Across total {tests_taken} attempts</p>
                </div>
                <div style="font-size: 5rem; opacity: 0.2; color: white !important;">🎯</div>
            </div>
        </div>
    """, unsafe_allow_html=True)

    st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)
    st.markdown("### 🚀 Quick Actions")
    
    qa_col1, qa_col2, qa_col3 = st.columns(3)
    
    with qa_col1:
        st.markdown("""
            <div class='card' style='height: 100%;'>
                <div style='font-size: 2.5rem; margin-bottom: 1rem;'>📄</div>
                <h4 style='color: var(--brand) !important;'>New Quiz</h4>
                <p style='font-size: 0.9rem; color: var(--muted) !important;'>Upload notes and generate AI questions instantly.</p>
            </div>
        """, unsafe_allow_html=True)
        if st.button("Start Extraction"):
            st.session_state.menu_selection = "Generate Quiz"
            st.rerun()

    with qa_col2:
        st.markdown("""
            <div class='card' style='height: 100%;'>
                <div style='font-size: 2.5rem; margin-bottom: 1rem;'>🧠</div>
                <h4 style='color: var(--brand-2) !important;'>Resume Test</h4>
                <p style='font-size: 0.9rem; color: var(--muted) !important;'>Take your most recently generated quiz again.</p>
            </div>
        """, unsafe_allow_html=True)
        if st.button("Jump to Quiz"):
            st.session_state.menu_selection = "Take Quiz"
            st.rerun()

    with qa_col3:
        st.markdown("""
            <div class='card' style='height: 100%;'>
                <div style='font-size: 2.5rem; margin-bottom: 1rem;'>📈</div>
                <h4 style='color: #0b7285 !important;'>Insights</h4>
                <p style='font-size: 0.9rem; color: var(--muted) !important;'>Detailed breakdown of your strengths and weaknesses.</p>
            </div>
        """, unsafe_allow_html=True)
        if st.button("View Analytics"):
            st.session_state.menu_selection = "Analytics Dashboard"
            st.rerun()

    st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)

elif menu == "Generate Quiz":
    st.markdown(f"""
        <div style='display: flex; align-items: center; gap: 15px; margin-bottom: 0.5rem;'>
            <div style='font-size: 2.2rem;'>📄</div>
            <h1 style='margin:0;'>Prepare Your Material</h1>
        </div>
        <p style='color: var(--muted); font-size: 1rem; margin-bottom: 2rem;'>Transform your study notes, documents, or media into a custom-tailored quiz.</p>
    """, unsafe_allow_html=True)
    
    st.markdown("### 📥 Choose Input Mode")
    
    # Styled Input Selection
    input_mode_col1, input_mode_col2 = st.columns(2)
    
    # We use session state to track the mode because we want custom buttons
    if "input_mode_choice" not in st.session_state:
        st.session_state.input_mode_choice = "Upload File"

    with input_mode_col1:
        is_pasted = st.session_state.input_mode_choice == "Paste Text"
        active_style = "border: 2px solid var(--brand); background: var(--brand-soft); transform: translateY(-3px);" if is_pasted else ""
        st.markdown(f"""
            <div class='card' style='padding: 1.5rem; text-align: center; {active_style}'>
                <div style='font-size: 2.5rem; margin-bottom: 0.8rem;'>✍️</div>
                <h4 style='margin:0;'>Paste Text</h4>
                <p style='font-size: 0.85rem; color: var(--muted); margin-top: 0.5rem;'>Manually write or paste content</p>
            </div>
        """, unsafe_allow_html=True)
        if st.button("Use Text Editor", key="mode_pasted", use_container_width=True):
            st.session_state.input_mode_choice = "Paste Text"
            st.rerun()

    with input_mode_col2:
        is_uploaded = st.session_state.input_mode_choice == "Upload File"
        active_style = "border: 2px solid var(--brand); background: var(--brand-soft); transform: translateY(-3px);" if is_uploaded else ""
        st.markdown(f"""
            <div class='card' style='padding: 1.5rem; text-align: center; {active_style}'>
                <div style='font-size: 2.5rem; margin-bottom: 0.8rem;'>📎</div>
                <h4 style='margin:0;'>Upload File</h4>
                <p style='font-size: 0.85rem; color: var(--muted); margin-top: 0.5rem;'>PDF or DOCX documents</p>
            </div>
        """, unsafe_allow_html=True)
        if st.button("Use File Uploader", key="mode_uploaded", use_container_width=True):
            st.session_state.input_mode_choice = "Upload File"
            st.rerun()

    st.divider()
    typed_text = ""
    uploaded = None

    if st.session_state.input_mode_choice == "Paste Text":
        with st.container():
            st.markdown("#### 📝 Edit Your Content")
            typed_text = st.text_area(
                "hidden_label",
                height=300,
                placeholder="Paste chapters, notes, or transcript text here to begin...",
                label_visibility="collapsed",
            )
    else:
        with st.container():
            st.markdown("#### 📁 Select Your Document")
            upload_types = ["pdf", "docx"]

            uploaded = st.file_uploader(
                "hidden_label",
                type=upload_types,
                help=f"Supported: {', '.join(ext.upper() for ext in upload_types)}",
                label_visibility="collapsed",
            )
            
            # Formats visual summary
            st.markdown(f"""
                <div style='display: flex; gap: 8px; flex-wrap: wrap; margin-top: 1rem;'>
                    {" ".join([f"<span style='background: #eef2f5; padding: 4px 10px; border-radius: 6px; font-size: 0.75rem; color: #4a5f6b; font-weight: 700;'>{ext.upper()}</span>" for ext in upload_types])}
                </div>
            """, unsafe_allow_html=True)
            if not HAS_OCR:
                st.caption("ℹ️ Scanned PDF OCR is disabled. Install `pytesseract` and `pdf2image` to enable it.")

    st.markdown("<div style='height: 2rem;'></div>", unsafe_allow_html=True)
    st.markdown("### ⚙️ Finalize Configuration")
    
    with st.container():
        st.markdown("<div class='card' style='padding: 2rem; border-color: rgba(0, 109, 119, 0.1);'>", unsafe_allow_html=True)
        ctrl_col1, ctrl_col2, ctrl_col3 = st.columns(3)
        with ctrl_col1:
            st.markdown("**🔢 Total Questions**")
            question_count = st.slider("questions_slider", min_value=3, max_value=25, value=8, label_visibility="collapsed")
            st.caption(f"Target: {question_count} items")
        with ctrl_col2:
            st.markdown("**🎯 Complexity**")
            difficulty = st.selectbox("diff_select", ["Easy", "Medium", "Hard"], label_visibility="collapsed")
        with ctrl_col3:
            st.markdown("**📋 Question Mode**")
            question_type = st.selectbox(
                "type_select",
                ["Multiple Choice Questions (MCQ)", "True/False", "Short Answer"],
                label_visibility="collapsed",
            )
        st.markdown("</div>", unsafe_allow_html=True)
    
    input_mode = st.session_state.input_mode_choice

    if st.button("Generate Quiz", type="primary", use_container_width=True):
        with st.status("Building your quiz...", expanded=True) as status_box:
            status_box.write("Reading content source...")
            processing_failed = False
            try:
                extracted_text, source_name = extract_input_text(input_mode, typed_text, uploaded)
                if extracted_text:
                    status_box.write(f"Extracted text from {source_name or 'input'}")
                else:
                    status_box.error("Content extraction failed.")
            except Exception as exc:
                detail = str(exc).strip() or f"{exc.__class__.__name__} occurred while processing the input."
                status_box.error(f"Input processing failed: {detail}")
                extracted_text, source_name = "", ""
                processing_failed = True

            if processing_failed:
                st.stop()
            if not extracted_text:
                if input_mode == "Paste Text":
                    status_box.warning("No text found. Paste some content and retry.")
                else:
                    file_name = uploaded.name if uploaded else "the selected file"
                    status_box.warning(
                        f"No valid text could be extracted from {file_name}. "
                        "For PDFs, ensure the file contains selectable text (not scanned images)."
                    )
            else:
                progress = st.progress(0)
                status_box.write("AI is generating questions...")
                progress.progress(45)
                questions = generate_quiz(extracted_text, question_count, difficulty, question_type)
                progress.progress(85)
                
                if not questions:
                    status_box.warning("Not enough content to build a quiz. Provide richer material.")
                else:
                    status_box.write("Saving your interactive quiz...")
                    quiz_id = save_questions(
                        questions=questions,
                        source_name=source_name or "Typed Text",
                        metadata={
                            "difficulty": difficulty,
                            "question_type": question_type,
                            "question_count": len(questions),
                            "generated_at": datetime.utcnow().isoformat(),
                        },
                    )
                    st.session_state.answers = {}
                    st.session_state.quiz_submitted = False
                    st.session_state.current_q = 0
                    progress.progress(100)
                    status_box.update(label="Quiz Ready!", state="complete", expanded=False)
                    st.success(f"Quiz generated successfully. Heading to 'Take Quiz'!")
                    st.session_state.menu_selection = "Take Quiz"
                    st.rerun()

elif menu == "Take Quiz":
    st.header("Take Your Quiz")
    st.write("Answer step-by-step and submit at the end.")

    quiz = load_questions()
    questions = quiz["questions"] if isinstance(quiz, dict) else quiz
    quiz_meta = quiz.get("metadata", {}) if isinstance(quiz, dict) else {}

    if not questions:
        st.info("No quiz available. Generate one first.")
    else:
        st.caption(
            f"Questions: {len(questions)} | Type: {quiz_meta.get('question_type', 'N/A')} | Difficulty: {quiz_meta.get('difficulty', 'N/A')}"
        )

        # Ensure state exists
        if "current_q" not in st.session_state:
            st.session_state.current_q = 0

        if "answers" not in st.session_state:
            st.session_state.answers = {}

        idx = st.session_state.current_q
        question = questions[idx]


        # Progress
        st.markdown(f"### 📝 Question {idx + 1} / {len(questions)}")
        st.progress((idx + 1) / len(questions))

        st.divider()
        with st.container():
            st.markdown('<div class="card quiz-question-card">', unsafe_allow_html=True)
            st.markdown(f"<div style='font-size:1.25rem; font-weight:700; margin-bottom:1.5rem; color:var(--ink);'>{question['question']}</div>", unsafe_allow_html=True)
            
            # Question Input logic based on type
            q_type = question["type"]
            if q_type in ["Multiple Choice Questions (MCQ)", "MCQ"]:
                current_answer = st.session_state.answers.get(idx)
                try:
                    index = question["options"].index(current_answer) if current_answer is not None else None
                except ValueError:
                    index = None
                
                choice = st.radio(
                    "Select the correct option:",
                    question["options"],
                    index=index,
                    key=f"q_radio_{idx}",
                    label_visibility="collapsed"
                )
                st.session_state.answers[idx] = choice
            
            elif q_type == "True/False":
                current_answer = st.session_state.answers.get(idx)
                index = None
                if current_answer == "True":
                    index = 0
                elif current_answer == "False":
                    index = 1
                
                choice = st.radio(
                    "True or False?",
                    ["True", "False"],
                    index=index,
                    key=f"q_tf_{idx}",
                    label_visibility="collapsed"
                )
                st.session_state.answers[idx] = choice
            
            elif q_type == "Short Answer":
                ans = st.text_area(
                    "Your Answer:",
                    value=st.session_state.answers.get(idx, ""),
                    key=f"q_sa_{idx}",
                    placeholder="Type your explanation here..."
                )
                st.session_state.answers[idx] = ans
            st.markdown('</div>', unsafe_allow_html=True)

        # Navigation
        col1, col2 = st.columns(2)

        with col1:
            if st.button("⬅️ Previous"):
                if idx > 0:
                    st.session_state.current_q -= 1
                    st.rerun()

        with col2:
            if idx < len(questions) - 1:
                if st.button("Next ➡️"):
                    st.session_state.current_q += 1
                    st.rerun()
            else:
                submit_button = st.button("🚀 Submit Quiz", type="primary")

                score = 0
                details = []
                weak_areas = []
                difficulty_totals = defaultdict(lambda: {"correct": 0, "total": 0})

                for i, q in enumerate(questions):
                    user_answer = st.session_state.answers.get(i)
                    correct = evaluate_answer(q, user_answer)
                    score += int(correct)

                    diff = q.get("difficulty", "unknown")
                    difficulty_totals[diff]["total"] += 1
                    difficulty_totals[diff]["correct"] += int(correct)

                    details.append({
                        "index": i + 1,
                        "question": q["question"],
                        "user_answer": user_answer,
                        "correct_answer": q["answer"],
                        "is_correct": correct,
                    })

                percent = round((score / len(questions)) * 100, 2)

                save_attempt(
                    score=score,
                    total=len(questions),
                    user_name=candidate.strip() or "Guest",
                    details=details,
                    difficulty_breakdown=dict(difficulty_totals),
                )

                # Reset
                st.session_state.current_q = 0

                # Styled Score Card
                stars = "⭐" * (int(percent // 20))
                st.markdown(f"""
                    <div class="score-card">
                        <div class="stars">{stars}</div>
                        <p>YOUR FINAL SCORE</p>
                        <h1>{percent}%</h1>
                        <p>{score} out of {len(questions)} correct</p>
                    </div>
                """, unsafe_allow_html=True)

                # Action Buttons
                btn_col1, btn_col2 = st.columns(2)
                with btn_col1:
                    df_results = pd.DataFrame(details)
                    csv = df_results.to_csv(index=False).encode('utf-8')
                    st.download_button(
                        "📥 Download Result (CSV)",
                        csv,
                        f"quiz_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                        "text/csv",
                        use_container_width=True
                    )
                with btn_col2:
                    if st.button("🔄 Retake Quiz", use_container_width=True):
                        st.session_state.answers = {}
                        st.session_state.current_q = 0
                        st.rerun()

                # Feedback
                if percent >= 80:
                    show_confetti()
                    play_sound("finish")
                    st.success("🔥 Excellent performance! You're a pro!")
                elif percent >= 50:
                    st.info("👍 Good job! With a bit more practice, you'll be perfect.")
                else:
                    st.warning("⚠️ Keep pushing! Review the material and try again.")

                # Weak Areas
                for diff, stats in difficulty_totals.items():
                    if stats["correct"] / stats["total"] < 0.5:
                        weak_areas.append(diff.title())

                if weak_areas:
                    st.warning(f"⚠️ Weak in: {', '.join(weak_areas)}")

                # Review Answers
                with st.expander("📊 Review Answers", expanded=True):
                    for item in details:
                        if item["is_correct"]:
                            st.success(f"Q{item['index']} - Correct ✅")
                        else:
                            st.error(f"Q{item['index']} - Incorrect ❌")

                        st.write(f"Your answer: {item['user_answer']}")
                        if not item["is_correct"]:
                            st.write(f"Correct answer: {item['correct_answer']}")

                        st.divider()

elif menu == "Analytics Dashboard":
    st.subheader("Quiz Analytics Dashboard")
    dataset = load_attempts(limit=200, user_name=candidate)
    render_dashboard(dataset)
