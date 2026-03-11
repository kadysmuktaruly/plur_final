-- Run this in your Supabase SQL Editor (Dashboard → SQL Editor → New Query)

-- 1. Profiles table (extends Supabase auth.users)
create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  username text unique not null,
  total_correct integer default 0,
  total_attempted integer default 0,
  created_at timestamptz default now()
);

-- 2. Active sessions (one row per user while they have an unanswered question)
create table if not exists public.active_sessions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid unique references auth.users(id) on delete cascade,
  question text not null,
  choices jsonb not null,
  correct_answer text not null,
  explanation text not null,
  created_at timestamptz default now()
);

-- 3. Score history (full log of every answered question)
create table if not exists public.score_history (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  question text not null,
  user_answer text not null,
  correct_answer text not null,
  is_correct boolean not null,
  explanation text not null,
  created_at timestamptz default now()
);

-- 4. Row Level Security — users can only see/edit their own data
alter table public.profiles enable row level security;
alter table public.active_sessions enable row level security;
alter table public.score_history enable row level security;

-- Profiles: readable by anyone (for leaderboard), writable only by owner
create policy "profiles_public_read" on public.profiles for select using (true);
create policy "profiles_self_write" on public.profiles for all using (auth.uid() = id);

-- Active sessions: only owner
create policy "sessions_self" on public.active_sessions for all using (auth.uid() = user_id);

-- History: only owner
create policy "history_self" on public.score_history for all using (auth.uid() = user_id);

-- 5. Allow service role to bypass RLS (needed for backend inserts)
-- (This is automatic for the service role key — no action needed)
