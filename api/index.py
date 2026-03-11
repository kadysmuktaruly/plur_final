import os
import json
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from google import genai
from supabase import create_client, Client

load_dotenv()

# ── env vars ──────────────────────────────────────────────────────────────────
GEMINI_API_KEY    = os.getenv("GEMINI_API_KEY")
SUPABASE_URL      = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

for name, val in [
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("SUPABASE_URL", SUPABASE_URL),
    ("SUPABASE_ANON_KEY", SUPABASE_ANON_KEY),
]:
    if not val:
        raise ValueError(f"Missing {name}")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI()
security = HTTPBearer()
BASE_DIR = Path(__file__).resolve().parent.parent


# ── auth helpers ──────────────────────────────────────────────────────────────
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    """Verify token using Supabase's get_user — no JWT secret needed."""
    token = credentials.credentials
    try:
        response = supabase.auth.get_user(token)
        if response.user is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return response.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def get_user_id(user_id: str) -> str:
    return user_id


# ── request models ────────────────────────────────────────────────────────────
class SignUpRequest(BaseModel):
    email: str
    password: str
    username: str

class SignInRequest(BaseModel):
    email: str
    password: str

class AnswerRequest(BaseModel):
    answer: str


# ── static pages ──────────────────────────────────────────────────────────────
@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "public" / "index.html")

@app.get("/app")
async def tutor_app():
    return FileResponse(BASE_DIR / "public" / "app.html")

@app.get("/leaderboard")
async def leaderboard_page():
    return FileResponse(BASE_DIR / "public" / "leaderboard.html")


# ── auth endpoints ────────────────────────────────────────────────────────────
@app.post("/auth/signup")
async def signup(req: SignUpRequest):
    try:
        res = supabase.auth.sign_up({
            "email": req.email,
            "password": req.password,
            "options": {"data": {"username": req.username}}
        })
        if res.user is None:
            raise HTTPException(status_code=400, detail="Signup failed")

        # Create profile row
        supabase.table("profiles").upsert({
            "id": res.user.id,
            "username": req.username,
            "total_correct": 0,
            "total_attempted": 0,
        }).execute()

        return {
            "access_token": res.session.access_token if res.session else None,
            "user": {"id": res.user.id, "email": res.user.email, "username": req.username}
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/auth/signin")
async def signin(req: SignInRequest):
    try:
        res = supabase.auth.sign_in_with_password({
            "email": req.email,
            "password": req.password
        })
        if res.user is None:
            raise HTTPException(status_code=401, detail="Invalid credentials")

        profile = supabase.table("profiles").select("username").eq("id", res.user.id).single().execute()
        username = profile.data.get("username", "") if profile.data else ""

        return {
            "access_token": res.session.access_token,
            "user": {"id": res.user.id, "email": res.user.email, "username": username}
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=401, detail=str(e))


@app.get("/auth/me")
async def get_me(user_id: str = Depends(verify_token)):
    profile = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
    if not profile.data:
        raise HTTPException(status_code=404, detail="Profile not found")
    return profile.data


# ── problem generation ────────────────────────────────────────────────────────
async def generate_problem():
    prompt = """
Generate one SAT-style algebra problem.

Return ONLY valid JSON:

{
  "question": "...",
  "choices": {"A":"...","B":"...","C":"...","D":"..."},
  "correct_answer": "A",
  "explanation": "step-by-step explanation"
}
"""
    loop = asyncio.get_running_loop()
    resp = await loop.run_in_executor(
        None,
        lambda: gemini_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
            config={"response_mime_type": "application/json"},
        ),
    )
    if resp.text is None:
        raise ValueError("Gemini returned empty response")
    return json.loads(resp.text)


@app.get("/problem")
async def get_problem(user_id: str = Depends(verify_token)):
    try:
        # Check for existing active session
        existing = supabase.table("active_sessions").select("id").eq("user_id", user_id).execute()
        if existing.data:
            raise HTTPException(status_code=400, detail="Answer the current question first.")

        data = await generate_problem()

        # Store in Supabase
        supabase.table("active_sessions").upsert({
            "user_id": user_id,
            "question": data["question"],
            "choices": json.dumps(data["choices"]),
            "correct_answer": data["correct_answer"],
            "explanation": data["explanation"],
        }).execute()

        return {"question": data["question"], "choices": data["choices"]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/answer")
async def check_answer(req: AnswerRequest, user_id: str = Depends(verify_token)):
    answer = req.answer.upper()

    session = supabase.table("active_sessions").select("*").eq("user_id", user_id).single().execute()
    if not session.data:
        raise HTTPException(status_code=400, detail="No active question")

    data = session.data
    correct = data["correct_answer"]
    is_correct = answer == correct

    # Update profile stats
    profile = supabase.table("profiles").select("total_correct,total_attempted").eq("id", user_id).single().execute()
    current = profile.data or {"total_correct": 0, "total_attempted": 0}

    new_correct   = current["total_correct"] + (1 if is_correct else 0)
    new_attempted = current["total_attempted"] + 1

    supabase.table("profiles").update({
        "total_correct": new_correct,
        "total_attempted": new_attempted,
    }).eq("id", user_id).execute()

    # Save to history
    supabase.table("score_history").insert({
        "user_id": user_id,
        "question": data["question"],
        "user_answer": answer,
        "correct_answer": correct,
        "is_correct": is_correct,
        "explanation": data["explanation"],
    }).execute()

    # Clear active session
    supabase.table("active_sessions").delete().eq("user_id", user_id).execute()

    return {
        "result": "correct" if is_correct else "incorrect",
        "correct_answer": correct,
        "explanation": data["explanation"],
        "score": {"correct": new_correct, "total": new_attempted},
    }


@app.get("/leaderboard/data")
async def get_leaderboard(user_id: str = Depends(verify_token)):
    result = supabase.table("profiles")\
        .select("username,total_correct,total_attempted")\
        .order("total_correct", desc=True)\
        .limit(20)\
        .execute()
    return {"leaderboard": result.data or []}


@app.get("/history")
async def get_history(user_id: str = Depends(verify_token)):
    result = supabase.table("score_history")\
        .select("*")\
        .eq("user_id", user_id)\
        .order("created_at", desc=True)\
        .limit(50)\
        .execute()
    return {"history": result.data or []}
