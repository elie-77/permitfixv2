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

-- ============================================================
-- Pricing reference (for Stripe webhook / Lovable integration):
--   $20 / submission  → plan_type = 'per_submission', increment submissions_remaining += 1
--   $77 / month       → plan_type = 'monthly', subscription_status = 'active'
-- ============================================================
