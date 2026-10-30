# RAG, embeddings, graph, SQL — which tool for which job

`[HYPOTHESIS]` design guidance.

Not one big RAG over every PDF. A hybrid:
- **Embedding** answers: are these two descriptions similar?
- **Graph** answers: which relationships really link these entities?
- **Relational DB** answers: what are the exact values and identifiers?

| Task | Best fit |
|---|---|
| Find a similar tender | Embeddings |
| Compare technical descriptions | Embeddings + structured filters (CPV, country, value) |
| Find a company by capability | Graph + vector search |
| Find links between suppliers | Graph |
| History of cooperation | Graph |
| Forecast recurring purchases | SQL + time series / simple ML |
| Check certificates | Structured data + sources |
| Analyse tender documents | RAG |
| Explain why a firm fits | Agent + graph traversal + evidence retrieval |

## Practical choice for the hackathon `[OWN KNOWLEDGE]`
- One **Postgres (Supabase)** database:
  - relational tables for tenders, companies, awards, deals;
  - **pgvector** column for embeddings of tender and capability text;
  - graph as **edge/claim tables** (`claims(subject, predicate, object, …)`) queried with SQL /
    recursive CTEs.
- No separate graph DB or vector DB today — one store, fewer moving parts, easier demo.
- Embed multilingual text with a multilingual model, or translate to English first and keep
  the original.

## Two ways to use history — use both
| Historical RAG (Model A) | Pattern intelligence (Model B) |
|---|---|
| Agent gets a demand, retrieves similar past tenders, uses them as context | History is pre-processed: patterns detected, classified, stored as structured knowledge |
| Finds concrete cases | Finds repeating relationships |
| Works on documents | Works on structured knowledge |
| Gives context | Gives rules and patterns |
| Good for single queries | Good for scaling knowledge |

Pipeline: historical data → RAG retrieval → pattern detection → pattern classification →
ontology / graph → agentic matching → new deals feed back in.
Self-learning loop, deal cards and metrics: [23-self-learning-rag.md](23-self-learning-rag.md).
