import os
import json
import asyncio
from pathlib import Path
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse, HTMLResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from google import genai
from supabase import create_client, Client
import stripe

load_dotenv()

# ── env vars ───────────────────────────────────────────────────────────────────
GEMINI_API_KEY       = os.getenv("GEMINI_API_KEY")
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_ANON_KEY    = os.getenv("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
SITE_URL             = os.getenv("SITE_URL", "http://127.0.0.1:8000")
STRIPE_SECRET_KEY    = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRICE_ID      = os.getenv("STRIPE_PRICE_ID", "")  # monthly price ID

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

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
    email: str  # can be email or username
    password: str

class AnswerRequest(BaseModel):
    answer: str

class FollowRequest(BaseModel):
    username: str  # username to follow/unfollow

class SurveyRequest(BaseModel):
    taken_sat: str | None = None
    current_score: int | None = None
    target_score: int | None = None
    exam_date_range: str | None = None
    study_hours_per_week: str | None = None


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

@app.get("/login")
async def login_page():
    return FileResponse(BASE_DIR / "public" / "login.html")

@app.get("/history-page")
async def history_page():
    return FileResponse(BASE_DIR / "public" / "history.html")

@app.get("/friends")
async def friends_page():
    return FileResponse(BASE_DIR / "public" / "friends.html")


@app.get("/survey")
async def survey_page():
    return FileResponse(BASE_DIR / "public" / "survey.html")


# ── survey endpoint ────────────────────────────────────────────────────────────
@app.post("/survey/complete")
async def complete_survey(req: SurveyRequest, user_id: str = Depends(verify_token)):
    """Save onboarding survey answers to the user's profile."""
    update_data = {}
    if req.taken_sat is not None: update_data["taken_sat"] = req.taken_sat
    if req.current_score is not None: update_data["current_score"] = req.current_score
    if req.target_score is not None: update_data["target_score"] = req.target_score
    if req.exam_date_range is not None: update_data["exam_date_range"] = req.exam_date_range
    if req.study_hours_per_week is not None: update_data["study_hours_per_week"] = req.study_hours_per_week
    if update_data:
        supabase.table("profiles").update(update_data).eq("id", user_id).execute()
    return {"ok": True}

# ── auth endpoints ─────────────────────────────────────────────────────────────
@app.post("/auth/signup")
async def signup(req: SignUpRequest):
    try:
        # Check username taken
        existing_username = supabase.table("profiles").select("id").eq("username", req.username).execute()
        if existing_username.data:
            raise HTTPException(status_code=400, detail="Username already taken. Please choose another.")

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
            "email": req.email,
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
        # Catch Supabase duplicate email error
        err = str(e)
        if "already registered" in err or "already been registered" in err or "User already registered" in err:
            raise HTTPException(status_code=400, detail="Email already registered. Please sign in instead.")
        raise HTTPException(status_code=400, detail=err)


@app.post("/auth/signin")
async def signin(req: SignInRequest):
    try:
        email_to_use = req.email.strip()

        # If input doesn't look like an email, treat it as a username
        if "@" not in email_to_use:
            # Look up email from profiles + auth.users via admin API
            profile = supabase.table("profiles")\
                .select("id")\
                .eq("username", email_to_use)\
                .execute()
            if not profile.data:
                raise HTTPException(status_code=401, detail="Username not found.")
            user_id = profile.data[0]["id"]
            # Use admin client to get email
            # Get email directly from profiles table (no admin API needed)
            email_profile = supabase.table("profiles")\
                .select("email")\
                .eq("id", user_id)\
                .single()\
                .execute()
            if not email_profile.data or not email_profile.data.get("email"):
                raise HTTPException(status_code=401, detail="Could not resolve username. Please sign in with your email instead.")
            email_to_use = email_profile.data["email"]

        res = supabase.auth.sign_in_with_password({
            "email": email_to_use,
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


# ── subscription helpers ──────────────────────────────────────────────────────

FREE_PROBLEM_LIMIT = 5

def is_user_pro(user_id: str) -> bool:
    try:
        profile = supabase.table("profiles").select("is_pro").eq("id", user_id).single().execute()
        return bool(profile.data and profile.data.get("is_pro"))
    except Exception:
        return False

def get_total_attempted(user_id: str) -> int:
    try:
        profile = supabase.table("profiles").select("total_attempted").eq("id", user_id).single().execute()
        return int(profile.data.get("total_attempted", 0)) if profile.data else 0
    except Exception:
        return 0


@app.post("/stripe/create-checkout")
async def create_checkout(user_id: str = Depends(verify_token)):
    try:
        # Get user email
        user_resp = supabase.auth.admin.get_user_by_id(user_id)
        email = user_resp.user.email if user_resp.user else ""

        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            mode="subscription",
            line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
            customer_email=email,
            success_url=SITE_URL + "/app?subscribed=1",
            cancel_url=SITE_URL + "/app?cancelled=1",
            metadata={"user_id": user_id},
        )
        return {"url": session.url}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/stripe/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        user_id = session.get("metadata", {}).get("user_id")
        stripe_customer_id = session.get("customer")
        if user_id:
            supabase.table("profiles").update({
                "is_pro": True,
                "stripe_customer_id": stripe_customer_id,
            }).eq("id", user_id).execute()

    elif event["type"] in ("customer.subscription.deleted", "customer.subscription.paused"):
        customer_id = event["data"]["object"].get("customer")
        if customer_id:
            supabase.table("profiles").update({"is_pro": False})\
                .eq("stripe_customer_id", customer_id).execute()

    return {"ok": True}


@app.get("/subscription/status")
async def subscription_status(user_id: str = Depends(verify_token)):
    pro = is_user_pro(user_id)
    attempted = get_total_attempted(user_id)
    return {
        "is_pro": pro,
        "total_attempted": attempted,
        "free_limit": FREE_PROBLEM_LIMIT,
        "problems_remaining_free": max(0, FREE_PROBLEM_LIMIT - attempted) if not pro else None,
    }


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

Return ONLY a valid JSON array (no markdown, no extra text).
IMPORTANT: Format ALL math expressions using LaTeX inline notation with $ delimiters.
Examples: use $x^2$ not x^2, use $\\frac{{a}}{{b}}$ not a/b for fractions, use $\\sqrt{{x}}$ not sqrt(x), use $ax^2 + bx + c = 0$ for equations.

[
  {{
    "question": "...",
    "choices": {{"A":"$...$","B":"$...$","C":"$...$","D":"$...$"}},
    "correct_answer": "A",
    "explanation": "step-by-step explanation using $ for math"
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
@app.get("/active-session")
async def get_active_session(user_id: str = Depends(verify_token)):
    """Return the user's current unanswered question if one exists."""
    session = supabase.table("active_sessions")\
        .select("*")\
        .eq("user_id", user_id)\
        .execute()
    if not session.data:
        return {"question": None}
    data = session.data[0]
    choices = data["choices"]
    if isinstance(choices, str):
        choices = json.loads(choices)
    return {"question": data["question"], "choices": choices}


@app.post("/skip")
async def skip_question(user_id: str = Depends(verify_token)):
    """Delete the active session so user can get a fresh question."""
    supabase.table("active_sessions").delete().eq("user_id", user_id).execute()
    return {"ok": True}


@app.get("/problem")
async def get_problem(difficulty: str = "easy", user_id: str = Depends(verify_token)):
    try:
        existing = supabase.table("active_sessions")\
            .select("id,problem_id")\
            .eq("user_id", user_id)\
            .execute()
        if existing.data:
            raise HTTPException(status_code=400, detail="Answer the current question first.")

        # Paywall check
        if not is_user_pro(user_id):
            attempted = get_total_attempted(user_id)
            if attempted >= FREE_PROBLEM_LIMIT:
                raise HTTPException(status_code=402, detail="free_limit_reached")

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
        .limit(50)\
        .execute()
    return {"leaderboard": result.data or []}


# ── friends / follow endpoints ─────────────────────────────────────────────────

@app.post("/friends/follow")
async def follow_user(req: FollowRequest, user_id: str = Depends(verify_token)):
    """Follow another user by username."""
    # Look up the target user's id
    target = supabase.table("profiles")\
        .select("id,username")\
        .eq("username", req.username)\
        .execute()
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found")

    target_id = target.data[0]["id"]
    if target_id == user_id:
        raise HTTPException(status_code=400, detail="You cannot follow yourself")

    # Upsert — silently succeeds if already following
    supabase.table("follows").upsert({
        "follower_id": user_id,
        "following_id": target_id,
    }, on_conflict="follower_id,following_id").execute()

    return {"ok": True, "following": req.username}


@app.post("/friends/unfollow")
async def unfollow_user(req: FollowRequest, user_id: str = Depends(verify_token)):
    """Unfollow another user by username."""
    target = supabase.table("profiles")\
        .select("id")\
        .eq("username", req.username)\
        .execute()
    if not target.data:
        raise HTTPException(status_code=404, detail="User not found")

    target_id = target.data[0]["id"]

    supabase.table("follows")\
        .delete()\
        .eq("follower_id", user_id)\
        .eq("following_id", target_id)\
        .execute()

    return {"ok": True, "unfollowed": req.username}


@app.get("/friends/following")
async def get_following(user_id: str = Depends(verify_token)):
    """Return usernames + stats for everyone the current user follows."""
    result = supabase.table("follows")\
        .select("following_id, profiles!follows_following_id_fkey(username,total_correct,total_attempted)")\
        .eq("follower_id", user_id)\
        .execute()

    usernames = []
    details = []
    for row in (result.data or []):
        profile = row.get("profiles")
        if profile and profile.get("username"):
            usernames.append(profile["username"])
            details.append({
                "username": profile["username"],
                "total_correct": profile.get("total_correct", 0),
                "total_attempted": profile.get("total_attempted", 0),
            })

    return {"following": usernames, "following_details": details}


@app.get("/friends/followers")
async def get_followers(user_id: str = Depends(verify_token)):
    """Return users who follow the current user, with stats."""
    result = supabase.table("follows")\
        .select("follower_id, profiles!follows_follower_id_fkey(username,total_correct,total_attempted)")\
        .eq("following_id", user_id)\
        .execute()

    details = []
    for row in (result.data or []):
        profile = row.get("profiles")
        if profile and profile.get("username"):
            details.append({
                "username": profile["username"],
                "total_correct": profile.get("total_correct", 0),
                "total_attempted": profile.get("total_attempted", 0),
            })

    return {"followers": [d["username"] for d in details], "followers_details": details}


@app.get("/leaderboard/friends")
async def get_friends_leaderboard(user_id: str = Depends(verify_token)):
    """Return leaderboard data for the current user + people they follow."""
    # Get IDs of everyone the user follows
    follows_res = supabase.table("follows")\
        .select("following_id")\
        .eq("follower_id", user_id)\
        .execute()

    friend_ids = [row["following_id"] for row in (follows_res.data or [])]
    # Always include the current user
    all_ids = list(set(friend_ids + [user_id]))

    if not all_ids:
        return {"leaderboard": []}

    result = supabase.table("profiles")\
        .select("username,total_correct,total_attempted")\
        .in_("id", all_ids)\
        .order("total_correct", desc=True)\
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


class DeleteAccountRequest(BaseModel):
    password: str

@app.delete("/auth/account")
async def delete_account(req: DeleteAccountRequest, credentials: HTTPAuthorizationCredentials = Depends(security)):
    """Permanently delete the authenticated user's account and all associated data."""
    import httpx

    # Verify token and get user_id
    token = credentials.credentials
    try:
        response = supabase.auth.get_user(token)
        if response.user is None:
            raise HTTPException(status_code=401, detail="Invalid token")
        user_id = response.user.id
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")

    # Re-authenticate with password to confirm identity
    try:
        profile = supabase.table("profiles").select("email").eq("id", user_id).single().execute()
        if not profile.data or not profile.data.get("email"):
            raise HTTPException(status_code=400, detail="Could not retrieve account email.")
        email = profile.data["email"]

        auth_check = supabase.auth.sign_in_with_password({"email": email, "password": req.password})
        if auth_check.user is None:
            raise HTTPException(status_code=401, detail="Incorrect password.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=401, detail="Incorrect password.")

    # Delete all user data in dependency order
    try:
        supabase.table("active_sessions").delete().eq("user_id", user_id).execute()
        supabase.table("score_history").delete().eq("user_id", user_id).execute()
        supabase.table("user_problem_answers").delete().eq("user_id", user_id).execute()
        supabase.table("follows").delete().eq("follower_id", user_id).execute()
        supabase.table("follows").delete().eq("following_id", user_id).execute()
        supabase.table("profiles").delete().eq("id", user_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to delete user data: {str(e)}")

    # Delete the auth user via direct Supabase Admin REST API
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.delete(
                f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
                headers={
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                },
            )
        if resp.status_code not in (200, 204):
            raise HTTPException(status_code=500, detail=f"Auth deletion failed: {resp.text}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Auth deletion failed: {str(e)}")

    return {"ok": True}