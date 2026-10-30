-- kb_embed_runs holds error text from the Edge Function. Keep it private: the public view is the redacted
-- GET status of the kb-embed function. (Applied after an unredacted fetch error briefly exposed secret material.)
revoke select on public.kb_embed_runs from anon, authenticated;
comment on table public.kb_embed_runs is
  'kb-embed run log and lease. Service role only; public status via GET /functions/v1/kb-embed (redacted).';
