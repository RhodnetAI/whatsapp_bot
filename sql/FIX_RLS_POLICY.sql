-- CRITICAL FIX: Remove RLS policies blocking setup table access
-- Run this FIRST if you're getting "row-level security policy" errors
-- Then run 002_service_agent_setup.sql

-- Step 1: Drop existing RLS policies on setup table
DROP POLICY IF EXISTS "setup_read" ON public.service_agent_setup;
DROP POLICY IF EXISTS "setup_write" ON public.service_agent_setup;
DROP POLICY IF EXISTS "setup_delete" ON public.service_agent_setup;
DROP POLICY IF EXISTS "setup_all" ON public.service_agent_setup;

-- Step 2: Drop existing RLS policies on documents table
DROP POLICY IF EXISTS "documents_read" ON public.service_agent_documents;
DROP POLICY IF EXISTS "documents_write" ON public.service_agent_documents;
DROP POLICY IF EXISTS "documents_delete" ON public.service_agent_documents;
DROP POLICY IF EXISTS "documents_all" ON public.service_agent_documents;

-- Step 3: Disable RLS completely on both tables
ALTER TABLE public.service_agent_setup DISABLE ROW LEVEL SECURITY;
ALTER TABLE public.service_agent_documents DISABLE ROW LEVEL SECURITY;

-- Step 4: Verify RLS is disabled (check output - should show false for rowsecurity)
SELECT tablename, rowsecurity 
FROM pg_tables 
WHERE tablename IN ('service_agent_setup', 'service_agent_documents');
