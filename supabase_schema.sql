-- Supabase schema for CV-Ai-Agent
-- Run these SQL statements in your Supabase SQL editor to create the required tables.
-- Notes:
-- - Use the SQL editor in Supabase dashboard to apply these statements
-- - Adjust types (uuid vs text) to match your existing projects if needed

-- Table: profiles
CREATE TABLE IF NOT EXISTS public.profiles (
  id TEXT PRIMARY KEY,
  full_name TEXT,
  current_resume UUID,
  created_at TIMESTAMPTZ DEFAULT now()
);

-- Table: resumes
CREATE TABLE IF NOT EXISTS public.resumes (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT REFERENCES public.profiles(id) ON DELETE CASCADE,
  original_filename TEXT,
  storage_path TEXT,
  mime_type TEXT,
  size INTEGER,
  resume_text TEXT,
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_resumes_user_id ON public.resumes (user_id);
CREATE INDEX IF NOT EXISTS idx_resumes_is_active ON public.resumes (is_active);

-- Table: reviews
CREATE TABLE IF NOT EXISTS public.reviews (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  user_id TEXT REFERENCES public.profiles(id), -- nullable for anonymous feedback
  filename TEXT,
  review TEXT,
  created_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_reviews_user_id ON public.reviews (user_id);

-- Optional: simple view for recent activity
CREATE OR REPLACE VIEW public.recent_reviews AS
SELECT r.id, r.user_id, r.filename, r.review, r.created_at
FROM public.reviews r
ORDER BY r.created_at DESC
LIMIT 100;

-- NOTE: Consider enabling Row Level Security and policies to control access.
-- Example policy to allow inserts via service key only (recommended for server-side inserts):
-- ALTER TABLE public.reviews ENABLE ROW LEVEL SECURITY;
-- CREATE POLICY "Insert via service" ON public.reviews
--   FOR INSERT
--   USING (auth.role() = 'service_role');
