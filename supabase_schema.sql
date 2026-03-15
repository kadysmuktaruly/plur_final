-- ═══════════════════════════════════════════════════════════
-- RUN THIS in Supabase → SQL Editor → New Query
-- ═══════════════════════════════════════════════════════════

-- 1. Shared problem pool
create table if not exists public.problems (
  id uuid primary key default gen_random_uuid(),
  difficulty text not null check (difficulty in ('easy','medium','hard')),
  question text not null,
  choices jsonb not null,
  correct_answer text not null,
  explanation text not null,
  created_at timestamptz default now()
);

-- 2. Track which user has answered which problem
create table if not exists public.user_problem_answers (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  problem_id uuid references public.problems(id) on delete cascade,
  user_answer text not null,
  is_correct boolean not null,
  answered_at timestamptz default now(),
  unique(user_id, problem_id)
);

-- 3. RLS
alter table public.problems enable row level security;
alter table public.user_problem_answers enable row level security;

-- Problems: everyone can read, only service role writes
create policy "problems_public_read" on public.problems for select using (true);

-- Answers: only owner can read/write their own
create policy "answers_self" on public.user_problem_answers for all using (auth.uid() = user_id);

-- 4. Useful indexes
create index if not exists problems_difficulty_idx on public.problems(difficulty);
create index if not exists answers_user_idx on public.user_problem_answers(user_id);
create index if not exists answers_problem_idx on public.user_problem_answers(problem_id);

-- ── Add problem_id to active_sessions (run if table already exists) ──
alter table public.active_sessions
  add column if not exists problem_id uuid references public.problems(id) on delete set null;

-- ── Add difficulty to score_history (run if table already exists) ──
alter table public.score_history
  add column if not exists difficulty text default '';

-- ── Add subscription fields to profiles ──
alter table public.profiles
  add column if not exists is_pro boolean default false,
  add column if not exists stripe_customer_id text default null;