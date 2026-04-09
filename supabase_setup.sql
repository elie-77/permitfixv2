-- ============================================================
-- PermitFix AI — Supabase Setup
-- Run this in: Supabase Dashboard → SQL Editor → New Query
-- ============================================================

-- ── stripe_customers table ────────────────────────────────────────────────────
-- Lovable may have created this already; these statements are idempotent.

CREATE TABLE IF NOT EXISTS public.stripe_customers (
  id                      UUID          DEFAULT gen_random_uuid() PRIMARY KEY,
  user_id                 UUID          REFERENCES auth.users(id) ON DELETE CASCADE UNIQUE NOT NULL,
  stripe_customer_id      TEXT,
  stripe_subscription_id  TEXT,
  subscription_status     TEXT          DEFAULT 'inactive',  -- 'active' | 'inactive' | 'cancelled' | 'past_due'
  plan_type               TEXT          DEFAULT 'per_submission',  -- 'monthly' | 'per_submission'
  submissions_remaining   INTEGER       DEFAULT 0,
  created_at              TIMESTAMPTZ   DEFAULT NOW(),
  updated_at              TIMESTAMPTZ   DEFAULT NOW()
);

-- Add new columns in case Lovable already created the table without them
ALTER TABLE public.stripe_customers
  ADD COLUMN IF NOT EXISTS plan_type             TEXT    DEFAULT 'per_submission',
  ADD COLUMN IF NOT EXISTS submissions_remaining INTEGER DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_stripe_customers_user_id
  ON public.stripe_customers (user_id);

-- ── Row Level Security ────────────────────────────────────────────────────────
ALTER TABLE public.stripe_customers ENABLE ROW LEVEL SECURITY;

-- Users can read their own billing record
DROP POLICY IF EXISTS "Users can read own stripe_customers" ON public.stripe_customers;
CREATE POLICY "Users can read own stripe_customers"
  ON public.stripe_customers FOR SELECT
  TO authenticated
  USING (auth.uid() = user_id);

-- Users can update their own record (needed so Streamlit can deduct submission credits)
DROP POLICY IF EXISTS "Users can update own stripe_customers" ON public.stripe_customers;
CREATE POLICY "Users can update own stripe_customers"
  ON public.stripe_customers FOR UPDATE
  TO authenticated
  USING (auth.uid() = user_id)
  WITH CHECK (auth.uid() = user_id);

-- ── Stripe webhook helper: insert on payment ──────────────────────────────────
-- This lets Lovable's Edge Function insert a record for a new paying user.
DROP POLICY IF EXISTS "Service role can insert stripe_customers" ON public.stripe_customers;
CREATE POLICY "Service role can insert stripe_customers"
  ON public.stripe_customers FOR INSERT
  TO service_role
  WITH CHECK (true);

DROP POLICY IF EXISTS "Service role can update stripe_customers" ON public.stripe_customers;
CREATE POLICY "Service role can update stripe_customers"
  ON public.stripe_customers FOR UPDATE
  TO service_role
  USING (true);

-- ── Auto-updated timestamp ────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.handle_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at = NOW();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS stripe_customers_updated_at ON public.stripe_customers;
CREATE TRIGGER stripe_customers_updated_at
  BEFORE UPDATE ON public.stripe_customers
  FOR EACH ROW EXECUTE FUNCTION public.handle_updated_at();

-- ── Free trial tracking ───────────────────────────────────────────────────────
-- 3-day gated trial: one scan, blurred results, no export, no bulk upload.
ALTER TABLE public.stripe_customers
  ADD COLUMN IF NOT EXISTS trial_started_at      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trial_expires_at      TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS trial_scan_used       BOOLEAN     DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS trial_scan_project_id TEXT;

-- Service role can insert new rows (needed for /start-trial endpoint)
DROP POLICY IF EXISTS "Service role can insert stripe_customers" ON public.stripe_customers;
CREATE POLICY "Service role can insert stripe_customers"
  ON public.stripe_customers FOR INSERT
  TO service_role
  WITH CHECK (true);

-- ── Helper: activate trial for a new user ─────────────────────────────────────
-- Called by FastAPI /start-trial or Lovable edge function after signup.
CREATE OR REPLACE FUNCTION public.start_user_trial(p_user_id UUID)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
DECLARE
  v_now   TIMESTAMPTZ := NOW();
  v_exp   TIMESTAMPTZ := NOW() + INTERVAL '3 days';
BEGIN
  INSERT INTO public.stripe_customers
    (user_id, subscription_status, plan_type, submissions_remaining,
     trial_started_at, trial_expires_at, trial_scan_used)
  VALUES
    (p_user_id, 'inactive', 'per_submission', 0, v_now, v_exp, FALSE)
  ON CONFLICT (user_id) DO UPDATE
    SET trial_started_at  = EXCLUDED.trial_started_at,
        trial_expires_at  = EXCLUDED.trial_expires_at,
        trial_scan_used   = FALSE
  WHERE public.stripe_customers.trial_started_at IS NULL
    AND public.stripe_customers.subscription_status <> 'active';
END;
$$;

-- ── Helper: fast truncate for re-indexing ─────────────────────────────────────
-- Called by load_obc.py --reload to wipe obc_sections quickly.
CREATE OR REPLACE FUNCTION public.truncate_obc_sections()
RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$
BEGIN
  TRUNCATE TABLE public.obc_sections RESTART IDENTITY;
END;
$$;

-- ============================================================
-- Pricing reference (for Stripe webhook / Lovable integration):
--   $20 / submission  → plan_type = 'per_submission', increment submissions_remaining += 1
--   $200 / month      → plan_type = 'monthly', subscription_status = 'active'
--   free trial        → trial_started_at set, trial_expires_at = started + 3 days
--                       trial_scan_used tracks whether the one free scan was used
-- ============================================================
