# SAT Algebra Tutor v2

Multi-user SAT algebra practice with authentication, leaderboard, and score history.

## Stack
- **FastAPI** — Python backend
- **Supabase** — Auth (email + Google OAuth) + Postgres database
- **Gemini 2.5 Flash** — AI problem generation
- **Vercel** — Hosting

## Setup

### 1. Supabase Project
1. Go to [supabase.com](https://supabase.com) and create a free project
2. Go to **SQL Editor** → paste and run `supabase_schema.sql`
3. Go to **Project Settings → API** and copy:
   - `Project URL` → `SUPABASE_URL`
   - `anon / public` key → `SUPABASE_ANON_KEY`
   - `JWT Secret` (under JWT Settings) → `SUPABASE_JWT_SECRET`

### 2. Google OAuth (optional)
1. In Supabase: **Authentication → Providers → Google → Enable**
2. Follow the instructions to create a Google OAuth app and paste the credentials

### 3. Local Development
```bash
# Install dependencies
pip install -r requirements.txt

# Create .env file
cat > .env << EOF
GEMINI_API_KEY=your_gemini_key
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_ANON_KEY=your_anon_key
SUPABASE_JWT_SECRET=your_jwt_secret
EOF

# Run server
python -m uvicorn api.index:app --reload
```

Open http://localhost:8000

### 4. Deploy to Vercel
```bash
npm install -g vercel
vercel login
vercel
```

In Vercel Dashboard → Project → **Settings → Environment Variables**, add:
- `GEMINI_API_KEY`
- `SUPABASE_URL`
- `SUPABASE_ANON_KEY`
- `SUPABASE_JWT_SECRET`

Then redeploy:
```bash
vercel --prod
```

## File Structure
```
sat-tutor/
├── api/
│   └── index.py          # FastAPI backend
├── public/
│   ├── index.html        # Login / Sign up page
│   ├── app.html          # Practice page (auth required)
│   └── leaderboard.html  # Leaderboard (auth required)
├── supabase_schema.sql   # Run this once in Supabase SQL editor
├── requirements.txt
├── vercel.json
└── .env                  # Local only, never commit
```

## API Endpoints
| Method | Path | Auth | Description |
|--------|------|------|-------------|
| POST | /auth/signup | No | Create account |
| POST | /auth/signin | No | Sign in |
| GET | /auth/me | Yes | Get own profile |
| GET | /problem | Yes | Get new question |
| POST | /answer | Yes | Submit answer |
| GET | /leaderboard/data | Yes | Top 20 users |
| GET | /history | Yes | Own answer history |
