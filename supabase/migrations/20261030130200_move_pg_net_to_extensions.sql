-- Security advisor 0014: pg_net was registered in public. Its functions live in schema "net" either way,
-- so ted_sync_trigger (net.http_post) keeps working after the move.
drop extension if exists pg_net;
create extension if not exists pg_net with schema extensions;
