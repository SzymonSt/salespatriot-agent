# agent_context evaluation

Checks whether the historical knowledge layer (`baseline_v1`, later `baseline_v2`) actually improves the context
pack that buyer and supplier agents get from `agent_context`. It scores the **pack**, not agent prose:
no agent conversation is run and no LLM or Gemini call is made. Everything is read-only SQL.

**Re-run it after `baseline_v2` becomes the active snapshot** (and after any change to `agent_ctx_*`, `kb_search`
or `kb_refresh`), then compare the new `results-<date>.md` with the previous one.

## Files

| File | What it is |
|---|---|
| `questions.json` | 25 questions over the 5 purposes, built on real open tenders, suppliers and buyers. Each has the `agent_context` call (`role`, `purpose`, `subject`, `query_text`, `viewer`), the ground-truth definition (`ground_truth.template` + `params`), the rendered `ground_truth_sql`, and the `expected` items scored in the pack |
| `build_eval.py` | Builds `ground_truth.sql` and `eval.sql` from `questions.json` (standard library only). `--check` fails if a question's `ground_truth_sql` is stale; `--write-questions` re-renders it |
| `ground_truth.sql` | One row per question: the answer computed from the source tables (`kb_baseline_procedures`, `ted_notices`, `ted_awards`, `supplier_contracts`) with CPV, buyer and title filters. Never uses `agent_context` or the classifier's family labels |
| `eval.sql` | Calls `agent_context` per question (text mode, plus a proxy-embedding mode for tender questions), builds the same pack without the baseline layer, and prints markdown: recall with vs without history, wrong numbers, warnings, size, latency |
| `retrieval_p5.sql` | Semantic retrieval without an API key: the stored embedding of 14 open tenders' cards as the query, precision@5 on family / class / calibre, cross-language hits, with and without the baseline |
| `unit_price_before_after.sql` | Unit-price priors for every open-tender scope: the pre-fix rule (replayed over `kb_baseline_stats`) vs the current `agent_ctx_priors` |
| `results-<date>.md` | Output of a run, with the findings |

## Run

```bash
python3 evals/agent_context/build_eval.py            # after editing questions.json
psql "$DATABASE_URL" -qAt -f evals/agent_context/eval.sql                    > /tmp/eval.md
psql "$DATABASE_URL" -qAt -f evals/agent_context/retrieval_p5.sql            > /tmp/p5.md
psql "$DATABASE_URL" -qAt -f evals/agent_context/unit_price_before_after.sql > /tmp/prices.md
psql "$DATABASE_URL" -qAt -f evals/agent_context/ground_truth.sql            # check that the ground truth still holds
```

Without `psql`, paste each file into the Supabase MCP `execute_sql` (project `aodxjmjnsqvadxcrepfr`). `eval.sql`
starts with `set statement_timeout = '15min'` because the first `agent_context` calls in a session can take 2-12 s.
Then write `results-<date>.md`: the three outputs plus the time, the active snapshot (printed in the header) and
what changed since the last run.

## How the comparison works

- **with history** = the pack exactly as `agent_context` returns it.
- **without history** = the same pack with `priors.baseline` (and a blend that only repeats it) set to null and the
  `evidence` list recomputed by an exact copy of `kb_search` + `agent_ctx_evidence` (or `agent_ctx_company_chunks`
  for `check_counterparty`) that skips `layer = 'baseline'`. `agent_context` has no layer switch, so this is an
  emulation; facts, candidates and price anchors do not read the baseline and stay as they are. The copy is
  verified on every run: run *with* the baseline it must return the same evidence refs as the real pack
  (column `fidelity`). If the deployed `kb_search` changes, the fidelity column shows `DIFF` and the copy in
  `build_eval.py` (`KB_SEARCH_REPLICA`) needs updating.
- **evaluate_quote** needs a conversation row, and the eval does not write. Q14 uses the one existing demo
  conversation. Q15-Q17 are emulated: they call `agent_ctx_priors` and `agent_ctx_anchors` (what `agent_context` calls
  in step 6) and apply the step-7 benchmark rule, copied in `build_eval.py` (`BENCHMARK`).
- **Recall** = share of the expected items found anywhere in the pack's facts, candidates, evidence, price anchors,
  priors, memory and warnings. Items are winner names (a distinctive lowercase token), TED publication numbers,
  numbers on a JSON path with a tolerance, or warning patterns. Winner items are the top winners of the ground-truth
  query by number of award notices. Recall measures whether the information reached the agent, not ranking quality.
- **Wrong numbers** = unit prices outside a per-family EUR band (our assumption: generous bands that only catch
  order-of-magnitude errors, rough fixed FX rates), p75/p25 > 20, non-positive values and `bids_received = 0` on
  awarded cards.
- **Modes**: `text` passes the question's `query_text` and no embedding (what an agent gets without a Google key).
  `embed` passes the stored embedding of the subject tender's own card as a stand-in for the query embedding
  (a proxy: a real agent would embed its question text).

## Adding a question

Add an object to `questions.json` with `ground_truth.template` = `procedures`, `supplier` or `search` and its params
(see the existing ones), run `build_eval.py --write-questions`, run `ground_truth.sql`, then fill `expected` from its
output. Keep expected items checkable by regex and keep the ground truth independent of `agent_context`.
