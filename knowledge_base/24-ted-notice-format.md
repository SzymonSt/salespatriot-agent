# TED notice format — real samples

Real notices from our scope ([21](21-focus-scope.md)), saved in full so everyone can see the
exact format. `[VERIFIED 2026-10-30]` (fetched that day).

## Samples in `../data/samples/`
Each sample has two files: `<number>.json` (every Search API field that has a value) and `<number>.xml` (the eForms XML).

| Notice | Type | Country | Legal basis | Why it is here |
|---|---|---|---|---|
| `749708-2026` | `cn-standard`, open tender | PL, Agencja Uzbrojenia | 32009L0081 (defence) | Restricted procedure: request-to-participate deadline only; naval ammunition |
| `750233-2026` | `cn-standard`, open tender | BG | 32014L0024 (classic) | Open procedure under the classic directive; cartridges |
| `720852-2026` | `can-standard`, award | PL, army unit | 32009L0081 | One winner (CENZIN, `NIP 5260251196`), value 16,974,000 PLN, 5 bids |
| `692794-2026` | `can-standard`, award | LT | 32009L0081 | **Consortium**: 2 winners from LT and EE, one tendering party |
| `754712-2026` | `can-standard`, award | CZ | 32014L0024 | Repeat winner (Sellier & Bellot), classic directive |

Fetch more: `python3 ../ted_notice_sample.py <publication-number> [...]`. It requests all 1,830
fields from `../data/ted_search_fields.txt` (extracted from the TED OpenAPI spec) and drops the empty ones.
On macOS with python.org Python, set `SSL_CERT_FILE` to certifi's bundle if you get `CERTIFICATE_VERIFY_FAILED`.

## Search API JSON — format rules
- A flat object. Only fields with a value come back: ~230–275 per notice in these samples.
- **Three value shapes:**
  - plain string: `"publication-number": "720852-2026"`, `"result-value-notice": "16974000"`;
  - list of strings, repeated per lot or organisation, so **dedupe** it: `"classification-cpv": ["35331000", "35331000"]`;
  - dict keyed by language: `"buyer-name": {"pol": ["Agencja Uzbrojenia"]}`.
- **Titles:**
  - `notice-title` comes in 24 languages, in the pattern "Country – CPV label – original title". Use `eng`.
  - `title-proc` and `description-proc` exist only in the original language (e.g. `pol`), so translate them or embed them with a multilingual model.
- **Numbers are strings** (`"126040.60"`). **Dates carry a timezone offset** (`"2026-10-19+02:00"`).
- **Aliases:** the same data appears under several names. Old TED codes and eForms BT codes
  duplicate the readable names: `TI` = `notice-title`, `AU` = `buyer-name`, `BT-24-Procedure` =
  `description-proc`, `BT-21-Lot` = `title-lot`, `OPT-211-Tenderer` = `tendering-party-name`. **Request the readable names.**
- `FT` (full text) is the biggest field (~28 KB). Skip it unless you need the full text for RAG.
- **Identifiers are free text**: `"NIP:7010269523"`, `"NIP 9521818707"`, `"300005349"`, `"RO 13554423"`. Normalise them ([19](19-ted-epo-ontology.md)).
- **Useful values seen:**
  - `BT-03-notice`: `competition` or `result`;
  - `buyer-legal-type`: e.g. `body-pl`;
  - `main-activity`: e.g. `defence`;
  - `procedure-type`: e.g. `restricted`;
  - `winner-size`: `small`, `medium` or `large`;
  - `subcontracting`: `no`;
  - `place-of-performance` mixes NUTS and country codes;
  - **`received-submissions-type-val`** is the number of bids received (5 in 720852), a direct measure of competition.
- **Restricted procedures** have no `deadline-receipt-tender-date-lot`, only `deadline-receipt-request`.
  For open procedures TED fills both with the same date. Generic `DT` repeats the request deadline.
- **Estimated value** is optional and often missing, especially in defence tenders. Fall back to `estimated-value-lot`
  (one entry per lot, so sum them) and `framework-maximum-value-lot`. Coverage: [25](25-supabase-ted-sync.md).
- **Consortia:** `winner-name` lists each member and `tendering-party-name` holds the joint name (692794).

## Caveat: notices before eForms (pre late 2023)
In the Search API, older notices (back to 2016 in our scope) have no `winner-*`, `title-proc` or
`description-proc` fields, only legacy ones such as `total-value`, `TI` and `FT`. Winners for that period
exist only in the full text or the legacy TED XML. Example: `6027-2018` has 62 fields and no winner.

## Caveat: lots
In the JSON every per-lot field is a flat list. **On multi-lot notices you cannot reliably pair
winner ↔ lot ↔ value from the JSON.** For lot-level truth, read the XML `NoticeResult` (as
`../ted_supplier_contracts.py` does).

## eForms XML — where things are
| Block | Contents |
|---|---|
| Root | `ContractNotice` (tender), `ContractAwardNotice` (result), `PriorInformationNotice` |
| `ContractingParty` | Buyer reference |
| `ProcurementProject` | Whole-procedure scope: `MainCommodityClassification/ItemClassificationCode` = CPV |
| `ProcurementProjectLot` | One per lot: lot scope, quantity, duration |
| `TenderingTerms` / `TenderingProcess` | Selection criteria, deadlines (`TenderSubmissionDeadlinePeriod`, `ParticipationRequestReceptionPeriod`) |
| `UBLExtensions/…/efac:Organizations` | **All organisations** (buyer, winners, review body…): name, `PartyIdentification`/`CompanyID`, address |
| `UBLExtensions/…/efac:NoticeResult` | `LotResult`, `LotTender` (`PayableAmount`), `TenderingParty → Tenderer`, `SettledContract`, `ReceivedSubmissionsStatistics` |

## JSON → our tables ([13](13-architecture-pipeline.md), PLAN schema)
| Our column | Search API field |
|---|---|
| `notices.publication_number` | `publication-number` |
| `notices.notice_type` / `legal_basis` / `procedure_type` | `notice-type` / `legal-basis[0]` / `procedure-type` |
| `notices.publication_date` | `publication-date` (drop the offset) |
| `notices.title_en` / `description` | `notice-title.eng` / `description-proc.<lang>` |
| `notices.cpv_codes` | `classification-cpv` (dedupe) |
| `notices.estimated_value`, `currency` | `estimated-value-proc`, `estimated-value-cur-proc` |
| `notices.deadline_tender` / `deadline_request` | `deadline-receipt-tender-date-lot` / `deadline-receipt-request` |
| buyer → `organisations` + `organisation_identifiers` | `buyer-name`, `buyer-identifier`, `buyer-country`, `buyer-legal-type`, `main-activity` |
| `awards.winner_id` (+ identifiers) | `winner-name`, `winner-identifier`, `winner-country`, `winner-size`, `winner-owner-nationality` |
| `awards.value`, `currency`, `decision_date` | `result-value-notice` (or per lot from XML), `result-value-cur-notice`, `winner-decision-date` |
| bids (competition signal) | `received-submissions-type-val` |
