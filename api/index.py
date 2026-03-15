import os
import json
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from google import genai
from supabase import create_client, Client

load_dotenv()

# ── env vars ───────────────────────────────────────────────────────────────────
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY")
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SITE_URL             = os.getenv("SITE_URL", "http://127.0.0.1:8000")

for name, val in [
    ("GEMINI_API_KEY", GEMINI_API_KEY),
    ("SUPABASE_URL", SUPABASE_URL),
    ("SUPABASE_ANON_KEY", SUPABASE_ANON_KEY),
    ("SUPABASE_SERVICE_KEY", SUPABASE_SERVICE_KEY),
]:
    if not val:
        raise ValueError(f"Missing {name}")

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

app = FastAPI()
security = HTTPBearer()
BASE_DIR = Path(__file__).resolve().parent.parent

POOL_SIZE = 30  # problems per difficulty level


# ── auth helpers ───────────────────────────────────────────────────────────────
def verify_token(credentials: HTTPAuthorizationCredentials = Depends(security)) -> str:
    token = credentials.credentials
    try:
        response = supabase.auth.get_user(token)
        if response.user is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        return response.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


# ── request models ─────────────────────────────────────────────────────────────
class SignUpRequest(BaseModel):
    email: str
    password: str
    username: str

class SignInRequest(BaseModel):
    email: str
    password: str

class AnswerRequest(BaseModel):
    answer: str


# ── static pages ───────────────────────────────────────────────────────────────
@app.get("/")
async def home():
    return FileResponse(BASE_DIR / "public" / "index.html")

@app.get("/app")
async def tutor_app():
    return FileResponse(BASE_DIR / "public" / "app.html")

@app.get("/leaderboard")
async def leaderboard_page():
    return FileResponse(BASE_DIR / "public" / "leaderboard.html")
@app.get("/history-page")
async def history_page():
    return FileResponse(BASE_DIR / "public" / "history.html")


# ── auth endpoints ─────────────────────────────────────────────────────────────
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

        try:
            profile = supabase.table("profiles").select("username").eq("id", res.user.id).single().execute()
            username = profile.data.get("username", "") if profile.data else ""
        except Exception:
            username = res.user.email.split("@")[0]

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


@app.get("/auth/google")
async def google_auth():
    res = supabase.auth.sign_in_with_oauth({
        "provider": "google",
        "options": {
            "redirect_to": SITE_URL + "/auth/callback"
        }
    })
    return RedirectResponse(res.url)


@app.get("/auth/callback")
async def auth_callback(code: str = None):
    if not code:
        return RedirectResponse("/")

    try:
        # Exchange the code for a real session
        res = supabase.auth.exchange_code_for_session({"auth_code": code})
        access_token = res.session.access_token
        user = res.user

        # Ensure profile exists
        try:
            profile = supabase.table("profiles").select("username").eq("id", user.id).single().execute()
            username = profile.data.get("username", "") if profile.data else ""
        except Exception:
            username = ""

        if not username:
            raw = ""
            if user.user_metadata:
                raw = user.user_metadata.get("full_name") or user.user_metadata.get("name") or ""
            if not raw:
                raw = (user.email or "user").split("@")[0]
            base = "".join(c for c in raw.replace(" ", "_") if c.isalnum() or c == "_")[:20] or "user"
            username = base
            suffix = 1
            while True:
                existing = supabase.table("profiles").select("id").eq("username", username).execute()
                if not existing.data:
                    break
                username = f"{base}{suffix}"
                suffix += 1
            supabase.table("profiles").upsert({
                "id": user.id,
                "username": username,
                "total_correct": 0,
                "total_attempted": 0,
            }).execute()

        # Redirect to /app with token embedded so JS can store it
        return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head><title>Signing in…</title></head>
<body>
<script>
  localStorage.setItem('sat_token', '{access_token}');
  localStorage.setItem('sat_user', JSON.stringify({{
    id: '{user.id}',
    email: '{user.email}',
    username: '{username}'
  }}));
  window.location.href = '/app';
</script>
<p style="font-family:sans-serif;text-align:center;margin-top:3rem;color:#888">Signing you in…</p>
</body>
</html>""")
    except Exception as e:
        return RedirectResponse("/?error=oauth_failed")


@app.post("/auth/google-profile")
async def google_profile(user_id: str = Depends(verify_token)):
    """
    Called after Google OAuth to ensure a profile row exists.
    Creates one from the Google account data if missing.
    """
    user_resp = supabase.auth.admin.get_user_by_id(user_id)
    user = user_resp.user
    email = user.email or ""

    # Try fetching existing profile
    try:
        profile = supabase.table("profiles").select("*").eq("id", user_id).single().execute()
        if profile.data:
            return {
                "id": user_id,
                "email": email,
                "username": profile.data.get("username", email.split("@")[0])
            }
    except Exception:
        pass

    # No profile yet — create one using Google display name or email prefix
    raw_username = ""
    if user.user_metadata:
        raw_username = user.user_metadata.get("full_name") or user.user_metadata.get("name") or ""
    if not raw_username:
        raw_username = email.split("@")[0]

    # Make username URL-safe and unique
    base = "".join(c for c in raw_username.replace(" ", "_") if c.isalnum() or c == "_")[:20] or "user"
    username = base
    suffix = 1
    while True:
        existing = supabase.table("profiles").select("id").eq("username", username).execute()
        if not existing.data:
            break
        username = f"{base}{suffix}"
        suffix += 1

    supabase.table("profiles").insert({
        "id": user_id,
        "username": username,
        "total_correct": 0,
        "total_attempted": 0,
    }).execute()

    return {"id": user_id, "email": email, "username": username}


# ── problem pool logic ─────────────────────────────────────────────────────────

async def generate_problems_batch(difficulty: str, count: int) -> list:
    """Ask Gemini to generate `count` problems at once."""
    difficulty_guide = {
        "easy": "basic algebra: solving simple linear equations, evaluating expressions, basic substitution. Suitable for 6th-8th grade.",
        "medium": "intermediate algebra: systems of equations, quadratics, inequalities, word problems. Suitable for SAT Math section.",
        "hard": "advanced algebra: complex quadratics, polynomial manipulation, function composition, challenging word problems. Hardest SAT Math level.",
    }
    guide = difficulty_guide.get(difficulty, difficulty_guide["easy"])
    prompt = f"""
Generate exactly {count} unique SAT-style algebra problems at {difficulty.upper()} difficulty.
Difficulty guide: {guide}

Return ONLY a valid JSON array (no markdown, no extra text):

[
  {{
    "question": "...",
    "choices": {{"A":"...","B":"...","C":"...","D":"..."}},
    "correct_answer": "A",
    "explanation": "step-by-step explanation"
  }},
  ...
]
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
    problems = json.loads(resp.text)
    if not isinstance(problems, list):
        raise ValueError("Gemini did not return a list")
    return problems


async def ensure_pool_for_user(user_id: str, difficulty: str):
    """
    If this user has no unsolved problems left at this difficulty,
    generate POOL_SIZE more and add them to the shared pool.
    """
    problems_res = supabase.table("problems")\
        .select("id")\
        .eq("difficulty", difficulty)\
        .execute()
    all_ids = [p["id"] for p in (problems_res.data or [])]

    answered_res = supabase.table("user_problem_answers")\
        .select("problem_id")\
        .eq("user_id", user_id)\
        .execute()
    answered_ids = {a["problem_id"] for a in (answered_res.data or [])}

    unsolved = [pid for pid in all_ids if pid not in answered_ids]

    if unsolved:
        return  # user still has problems to work through

    # User is caught up — generate a fresh batch
    problems = await generate_problems_batch(difficulty, POOL_SIZE)
    rows = [
        {
            "difficulty": difficulty,
            "question": p["question"],
            "choices": p["choices"],
            "correct_answer": p["correct_answer"],
            "explanation": p["explanation"],
        }
        for p in problems
    ]
    supabase.table("problems").insert(rows).execute()


def get_next_problem_for_user(user_id: str, difficulty: str):
    answered_res = supabase.table("user_problem_answers")\
        .select("problem_id")\
        .eq("user_id", user_id)\
        .execute()
    answered_ids = [a["problem_id"] for a in (answered_res.data or [])]

    query = supabase.table("problems")\
        .select("*")\
        .eq("difficulty", difficulty)

    if answered_ids:
        query = query.not_.in_("id", answered_ids)

    result = query.limit(1).execute()
    return result.data[0] if result.data else None


# ── problem endpoints ──────────────────────────────────────────────────────────
@app.get("/problem")
async def get_problem(difficulty: str = "easy", user_id: str = Depends(verify_token)):
    try:
        existing = supabase.table("active_sessions")\
            .select("id,problem_id")\
            .eq("user_id", user_id)\
            .execute()
        if existing.data:
            raise HTTPException(status_code=400, detail="Answer the current question first.")

        await ensure_pool_for_user(user_id, difficulty)

        problem = get_next_problem_for_user(user_id, difficulty)

        if problem is None:
            return {
                "question": None,
                "choices": None,
                "pool_exhausted": True,
                "message": "You've solved all available problems at this difficulty! Check back soon for more."
            }

        supabase.table("active_sessions").upsert({
            "user_id": user_id,
            "problem_id": problem["id"],
            "question": problem["question"],
            "choices": json.dumps(problem["choices"]) if isinstance(problem["choices"], dict) else problem["choices"],
            "correct_answer": problem["correct_answer"],
            "explanation": problem["explanation"],
        }).execute()

        return {
            "question": problem["question"],
            "choices": problem["choices"],
            "pool_exhausted": False,
            "pool_info": {"difficulty": difficulty}
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/answer")
async def check_answer(req: AnswerRequest, user_id: str = Depends(verify_token)):
    answer = req.answer.upper()

    session = supabase.table("active_sessions")\
        .select("*")\
        .eq("user_id", user_id)\
        .single()\
        .execute()
    if not session.data:
        raise HTTPException(status_code=400, detail="No active question")

    data = session.data
    correct = data["correct_answer"]
    is_correct = answer == correct
    problem_id = data.get("problem_id")

    if problem_id:
        supabase.table("user_problem_answers").upsert({
            "user_id": user_id,
            "problem_id": problem_id,
            "user_answer": answer,
            "is_correct": is_correct,
        }).execute()

    # Get difficulty from the problem record
    problem_difficulty = ""
    if problem_id:
        try:
            prob = supabase.table("problems").select("difficulty,choices").eq("id", problem_id).single().execute()
            if prob.data:
                problem_difficulty = prob.data.get("difficulty", "")
        except Exception:
            pass

    supabase.table("score_history").insert({
        "user_id": user_id,
        "question": data["question"],
        "user_answer": answer,
        "correct_answer": correct,
        "is_correct": is_correct,
        "explanation": data["explanation"],
        "difficulty": problem_difficulty,
    }).execute()

    profile = supabase.table("profiles")\
        .select("total_correct,total_attempted")\
        .eq("id", user_id).single().execute()
    current = profile.data or {"total_correct": 0, "total_attempted": 0}

    supabase.table("profiles").update({
        "total_correct": current["total_correct"] + (1 if is_correct else 0),
        "total_attempted": current["total_attempted"] + 1,
    }).eq("id", user_id).execute()

    supabase.table("active_sessions").delete().eq("user_id", user_id).execute()

    return {
        "result": "correct" if is_correct else "incorrect",
        "correct_answer": correct,
        "explanation": data["explanation"],
        "score": {
            "correct": current["total_correct"] + (1 if is_correct else 0),
            "total": current["total_attempted"] + 1,
        },
    }


@app.delete("/session/clear")
async def clear_session(user_id: str = Depends(verify_token)):
    supabase.table("active_sessions").delete().eq("user_id", user_id).execute()
    return {"cleared": True}


@app.get("/pool/status")
async def pool_status(user_id: str = Depends(verify_token)):
    result = {}
    for diff in ["easy", "medium", "hard"]:
        total_res = supabase.table("problems")\
            .select("id", count="exact")\
            .eq("difficulty", diff).execute()
        total = total_res.count or 0

        answered_res = supabase.table("user_problem_answers")\
            .select("problem_id")\
            .eq("user_id", user_id)\
            .execute()
        answered_ids = {a["problem_id"] for a in (answered_res.data or [])}

        problems_res = supabase.table("problems")\
            .select("id")\
            .eq("difficulty", diff).execute()
        diff_ids = {p["id"] for p in (problems_res.data or [])}

        solved_in_diff = len(answered_ids & diff_ids)
        result[diff] = {
            "total": total,
            "solved_by_you": solved_in_diff,
            "remaining": max(0, total - solved_in_diff),
        }
    return result


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