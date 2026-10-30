# How ePO / TED model "what is being bought" — research for our product-family taxonomy

Checked on **2026-10-30**. Every fact is tagged:
- **VERIFIED [Sx]**: I read it today in the source behind tag `Sx` (URLs in the Sources list at the end).
- **INFERENCE**: my own reading or proposal. Not stated by a source.

Versions read: ePO **v5.2.0** (latest release) and ePO **v4.2.0** (latest 4.x), plus v4.0.0 where TED's mapping uses it.
eForms SDK **1.15.1** (latest stable) and **2.0.0-alpha.4** (latest pre-release).

---

## 0. Summary

1. The subject of a procedure, lot or PIN part is an `epo:Purpose`. It holds `epo:hasMainClassification` (1..*), `epo:hasAdditionalClassification` (0..*) and `epo:hasTotalQuantity` (0..1). VERIFIED [S1][S2]
2. These two classification properties are **CPV-only**. ePO 4.0.0 and 4.2.0 SHACL require the CPV scheme. ePO 5.2.0 dropped the SHACL check but defines both properties as "the main/additional Common Procurement Vocabulary code". VERIFIED [S1][S3][S3b]
3. eForms allows only CPV. BT-26 "Classification Type" has a one-value code list: `cpv`. BT-262 and BT-263 are bound to the `cpv` code list. VERIFIED [S13][S14]
4. ePO has a full item model in **eCatalogue**: `epo:Item` / `epo:AbstractItem`, `epo:hasItemClassification` (any scheme: "CPV, UNSPC, eCl@ss, GPC, ...") and `epo:ItemProperty` for attribute–value pairs. VERIFIED [S5][S6]
5. `epo:ItemProperty` is a name/value construct. The attribute is a **string** (`epo:hasAttributeType`, plus `epo:hasClassificationScheme`). The value is either a **string** (`epo:hasLiteralValue`) or an `epo:Quantity` with a unit (`epo:hasQualifiedValue`, and in 5.x also `epo:hasMinimumValue` / `epo:hasMaximumValue`). There is no per-attribute datatype. VERIFIED [S5][S6][S7]
6. ePO has **no link from Lot/Procedure/Purpose to Item**. Items appear in catalogue, order, request-for-offer and contract lines (`epo:Line` → `epo:specifiesItem`) and as `epo:Deliverable` of a `epo:Contract`. VERIFIED [S6][S8]
7. TED Open Data never carries CPV supplementary codes. OP-TED dropped them "forever" because ePO cannot pair a supplementary code with its main code. VERIFIED [S20][S21]
8. The CPV supplementary vocabulary (`cpvsuppl`, 965 concepts, 19 sections) has useful **tags** for us (FB01 "For military use", FB02 "For weapons and ammunition", FB13 "With military specifications", AA44 "Brass", CA03 "Bullet-proof"). It has **no calibre codes**. VERIFIED [S22]
9. Recommendation, in short (INFERENCE, details in §7): keep CPV in `Purpose`. Put our family code on an `epo:Item` through `epo:hasItemClassification`, in our own SKOS scheme. Store attributes as `epo:ItemProperty` rows. Take units from the EU `measurement-unit` authority table.

---

## 1. Subject of the procurement: `epo:Purpose` and CPV

### 1.1 Classes and properties (exact names)

| Term | v4.2.0 | v5.2.0 | Source |
|---|---|---|---|
| `epo:Purpose` | Class. "The description of the objectives related to a Procurement. … includes the Subject Matter and Quantities." | Same text | VERIFIED [S1] (L602), [S28] `classes/Purpose.md` |
| `epo:hasPurpose` | Domain `epo:ProcurementObject` (subclasses `epo:Lot`, `epo:Procedure`), max 1 | SHACL on `epo:ProcurementElement` (max 1) **and** on `epo:Contract` (max 1) | VERIFIED [S28] `ProcurementObject.md`; [S2] L4601, L2586 |
| `epo:hasMainClassification` | Domain `epo:Purpose`, range `skos:Concept`. No definition text | Definition: "Relation indicating the main Common Procurement Vocabulary code." SHACL: `sh:class skos:Concept`, IRI, **minCount 1** | VERIFIED [S4] L2230; [S1] L2431; [S2] L4839 |
| `epo:hasAdditionalClassification` | Domain `epo:Purpose`, range `skos:Concept`. No definition text | Definition: "Relation indicating the additional Common Procurement Vocabulary code." SHACL: `sh:class skos:Concept`, IRI, 0..* | VERIFIED [S1] L1412; [S2] L4831 |
| `epo:hasTotalQuantity` | `epo:Purpose` → `epo:Quantity`, max 1. "The number of units required. … The quantity needs to go along with the unit." | Same, 0..1 | VERIFIED [S28] `hasTotalQuantity.md`; [S2] L4848 |
| `epo:Quantity` | "A counted number of non-monetary units possibly including fractions." `epo:hasQuantityValue` (xsd:decimal, exactly 1), `epo:hasUnitCode` (1..1), `epo:hasUnitDescription` (0..*) | Same, but `epo:hasUnitCode` is 0..1 | VERIFIED [S28] `Quantity.md`; [S3] L5072; [S2] L4944 |
| `epo:hasContractNatureType` / `epo:hasAdditionalContractNature` | On `epo:ContractTerm`, code list `contract-nature` (works/supplies/services) | Same | VERIFIED [S28]; [S3] L857 |

### 1.2 Which classification schemes are allowed?

| ePO version | Main / additional classification restricted to CPV? | Source |
|---|---|---|
| 4.0.0 (the version in TED's mapping metadata) | Yes. SHACL `sh:class at-voc:cpv` on both. `hasUnitCode` uses `sh:class at-voc:measurement-unit` | VERIFIED [S3b] L3952–3961, L3992 |
| 4.2.0 | Yes. SHACL `sh:node [ sh:path skos:inScheme ; sh:hasValue <http://data.europa.eu/cpv/cpv> ]` on both. `hasUnitCode` requires `skos:inScheme` measurement-unit | VERIFIED [S3] L5928–5953, L5072 |
| 5.2.0 | No SHACL scheme check (0 `skos:inScheme` constraints in core shapes). The **definitions** still say "Common Procurement Vocabulary code" | VERIFIED [S2][S1] |

Conclusion: a custom scheme on `hasMainClassification` / `hasAdditionalClassification` fails ePO 4.x validation. It also goes against the 5.2.0 definition. INFERENCE (follows directly from the rows above).

The only open, multi-scheme classification property in ePO is `epo:hasItemClassification` on items (see §2). VERIFIED [S5] L397

### 1.3 How TED attaches CPV at each level (eForms → ePO RDF)

| Level | eForms XML | ePO in TED RDF | Source |
|---|---|---|---|
| Procedure | `cac:ProcurementProject/cac:MainCommodityClassification` (BT-262-Procedure), `…/AdditionalCommodityClassification` (BT-263-Procedure) | `Procedure hasPurpose ProcedurePurpose`; `hasMainClassification` / `hasAdditionalClassification` | VERIFIED [S19] Procedure.rml.ttl L992–1036 |
| Lot | `cac:ProcurementProjectLot[…'Lot']/…/MainCommodityClassification` (BT-262-Lot), `…/AdditionalCommodityClassification` (BT-263-Lot) | `Lot hasPurpose LotPurpose`; same two properties | VERIFIED [S16] L3916–3971 |
| PIN part | `…[…'Part']/…/MainCommodityClassification` (BT-262-Part) | `PlannedProcurementPart foreseesProcurementObject ProcurementObject hasPurpose Purpose` | VERIFIED [S19] PlannedProcurementPart-pin.rml.ttl L433–481 |
| Lot quantity | `cbc:EstimatedOverallContractQuantity` (BT-25-Lot) and its `@unitCode` (BT-625-Lot) | `LotPurpose hasTotalQuantity Quantity`; `hasQuantityValue`, `hasUnitCode` → measurement-unit URI | VERIFIED [S16] L3085–3148 |

- The mapping keeps only codes with `@listName='cpv'`. Anything else becomes `null` (not output). VERIFIED [S16] L3940, L3971
- CPV object URIs look like `http://data.europa.eu/cpv/cpv/35331500`. Each CPV concept has `skos:inScheme <http://data.europa.eu/cpv/cpv>`, `skos:notation`, `dc:identifier` and `skos:broader` (35331500 → 35331000). VERIFIED [S22] (query Q13)
- TED's eForms mapping packages state `"ontology_version": "4.0.0"`. VERIFIED [S17]

### 1.4 What the live TED data looks like for our classes (VERIFIED [S22], queries in Appendix A)

| Example | What is in RDF |
|---|---|
| Notice `00725086-2025`, Lot `LOT-0001`, `dct:title` "L1 Cartucho 9 x 19 mm Ordinario" | `LotPurpose hasMainClassification cpv/35331500`. No additional classification. No quantity. Calibre and type exist **only in free text**. |
| Notice `00415461-2025`, lot "Dostawa 5,56 mm naboi ślepych" (5.56 mm blank cartridges) | `hasTotalQuantity [ hasQuantityValue 40000000 ; hasUnitCode measurement-unit/H87 ]` (piece) |
| Additional classifications used with main 35330000 / 35331000 / 35320000 / 35340000 / 35310000 | All are CPV: e.g. 35330000 + 35331500 (185×), + 35331000 (94×), + 35300000 (48×), + 35331100 (46×). No non-CPV codes appear. |

---

## 2. Item, catalogue, ordering and fulfilment modules

### 2.1 Modules

ePO 5.2.0 has these modules: ePO_core, eNotice, eAccess, eSubmission, eEvaluation, eAwarding, eQualification, eContract, eCatalogue, eOrdering, eFulfilment, eInvoicing, eRequest, ePayment. VERIFIED [S8] (`implementation/README.md`)
ePO 4.2.0 has: ePO_core, eNotice, eAccess, eSubmission, eContract, eCatalogue, eOrdering, eFulfilment, eInvoicing. VERIFIED [S7] (folder list of tag v4.2.0)
The 5.2.0 release notes list only metadata changes for eCatalogue, eOrdering, eFulfilment, eContract and eRequest. VERIFIED [S10]

### 2.2 Item-related classes (v5.2.0, exact names and definitions)

| Class | Module | Superclass | Definition (shortened, exact words) | Source |
|---|---|---|---|---|
| `epo:AbstractItem` | eCatalogue | – | "Gathering class for the Items, Item Requests and Deliverables. … related to a procurement need." | VERIFIED [S5] L760 |
| `epo:Item` | eCatalogue | `epo:AbstractItem` | "A product, work, service or any combination of them. … An Item is used to fulfill a need which is described in an Item Request." | VERIFIED [S5] L71 |
| `epo:Deliverable` | eContract | `epo:AbstractItem` | "Item that the supplier is obligated to provide under a contract." | VERIFIED [S8] eContract.ttl L23 |
| `epo:ItemProperty` | eCatalogue | – | "Construct meant to represent the association between an attribute defined in an external classification scheme and the value ascribed to it." | VERIFIED [S5] L105 |
| `epo:DimensionInformation` | eCatalogue | – | "Information about the physical or measurable attributes of the instance of the concept described." | VERIFIED [S5] |
| `epo:ItemModel`, `epo:Brand`, `epo:Batch`, `epo:ProductSpecification` (⊂ `epo:Document`) | eCatalogue | – | model name, brand, production lot, document about an item | VERIFIED [S5] |
| `epo:ItemRelation` + `epo:ItemComposition`, `epo:ItemAccessoryRelation`, `epo:ItemComplementaryRelation`, `epo:ItemReplacement`, `epo:ItemRequirement` | eCatalogue | – | relations between items (parts, accessories, replacements) | VERIFIED [S5] |
| `epo:Catalogue` (⊂ `epo:Document`), `epo:PostAwardCatalogue`, `epo:CatalogueLine` (⊂ `epo:Line`), `epo:Price` | eCatalogue | – | offered items with price and order limits | VERIFIED [S5] |
| `epo:Line` | eCatalogue | – | "Component of a document providing specific information." | VERIFIED [S5] |
| `epo:Order`, `epo:OrderLine` (⊂ `epo:Line`), `epo:OrderResponse(Line)`, `epo:OrderChange(Line)`, `epo:OrderAgreement(Line)`, `epo:Seller` | eOrdering | – | buyer's order and its lines | VERIFIED [S8] |
| `epo:DespatchAdvice`, `epo:DespatchLine`, `epo:ReceiptAdvice(Line)`, `epo:GoodsItem`, `epo:Package`, `epo:Consignment`, `epo:ShipmentInformation` … | eFulfilment | – | delivery documents | VERIFIED [S8] |
| `epo:RequestForOffer`, `epo:RequestForOfferLine` (⊂ `epo:Line`) | eRequest | – | buyer asks a supplier for an offer | VERIFIED [S8] |

Not found: there is **no `epo:ItemRequest` class** in any 5.2.0 module, although the `AbstractItem` and `Item` definitions mention "Item Requests". VERIFIED [S8] (grep over all module TTL files)

### 2.3 `epo:ItemProperty` — exact properties and types

| Property | v4.2.0 SHACL | v5.2.0 SHACL | Meaning (definition) | Source |
|---|---|---|---|---|
| `epo:describesItem` | → `epo:Item`, 1..1 | → `epo:AbstractItem`, 1..1 | "Characterises Item." Direction: property → item | VERIFIED [S7] L168; [S6] L1066 |
| `epo:hasAttributeType` | string/langString, 0..1 | string/langString, 0..1 | "The property defined in a classification scheme." Example: ECLASS `(0173-1#02-AAA026#007) drilling diameter` | VERIFIED [S5] L234; [S6] L1077 |
| `epo:hasClassificationScheme` | string/langString, 0..1 | string/langString, 0..1 | "The classification scheme where a property is defined." (ECLASS example) | VERIFIED [S5] L299; [S6] L1093 |
| `epo:hasClassificationCode` | – (absent) | string/langString, 0..1 | "The categorization of the instance of the concept expressed as a code." | VERIFIED [S5]; [S6] L1085 |
| `epo:hasLiteralValue` | string/langString, **0..1** | string/langString, **0..*** | "Simple value of the property … e.g. coating … has value of "silver". … for the cases when the property value does not have units, in which case the qualifiedValue shall be used." | VERIFIED [S7] L206; [S5] L420 |
| `epo:hasQualifiedValue` | → `epo:Quantity`, 0..1 | → `epo:Quantity`, 0..1 | "Qualified value of the property … drilling diameter … has value of 12 inches." | VERIFIED [S5] L559; [S6] L1136 |
| `epo:hasMinimumValue` / `epo:hasMaximumValue` | – (absent) | → `epo:Quantity`, 0..1 each | "lower / upper limit of the numerical quantity" | VERIFIED [S6] |
| `epo:hasImportanceCode` | – (absent) | xsd:integer, 0..1 | "The significance of the instance of the concept expressed as a code." | VERIFIED [S6] L1101 |
| `dcterms:title` | 0..1 | not in 5.2.0 shapes | name of the property | VERIFIED [S7]; [S6] (not found) |

So the answer to "name/value or typed?":
- It is a **name/value** pattern. The attribute is identified by strings (`hasAttributeType`, `hasClassificationScheme`, 5.x `hasClassificationCode`). It is not a URI. VERIFIED [S6]
- Values have **two kinds only**: text (`hasLiteralValue`) or quantity-with-unit (`hasQualifiedValue`, min/max). No boolean, date or coded-value (URI) type. VERIFIED [S6]
- The design follows the property systems of ECLASS and GS1 GPC and the CEN BII3 model. VERIFIED [S12] issue #341
- The 5.x `hasClassificationCode` came from a CEN/TC 440 request for an "Item property code" (UBL `cbc:NameCode`). VERIFIED [S12] issue #773

### 2.4 Other item properties that matter to us (v5.2.0, on `epo:AbstractItem`)

| Property | Range, cardinality | Note | Source |
|---|---|---|---|
| `epo:hasItemClassification` | `skos:Concept`, IRI, 0..* | "A classification of this item according to a specific system … Examples of classification schemes are: CPV, UNSPC, eCl@ss, GPC, ..." No scheme restriction in 4.2.0 or 5.2.0 | VERIFIED [S5] L397; [S6] L375; [S7] |
| `epo:hasItemClassificationIdentifier` | `adms:Identifier`, 0..* | identifier form of the classification | VERIFIED [S6] |
| `epo:hasNetQuantity` | `epo:Quantity`, 0..1 | "net quantity … in each consumable unit, excluding any packaging" | VERIFIED [S5] |
| `epo:hasKeyword` | literal, 0..* | "List of words (e.g, synonyms) to make the item searchable." | VERIFIED [S5] |
| `epo:hasCountryOfOrigin` | IRI, 0..1 | buyer requirement or tenderer info | VERIFIED [S5] |
| `epo:hasManufacturer` → `epo:Manufacturer`; `epo:hasBrand`; `epo:hasModel` → `epo:ItemModel` | 0..1 each | | VERIFIED [S6] |
| `epo:hasItemStandardID`, `epo:hasManufacturerItemID`, `epo:hasSellerItemID`, `epo:hasBuyerItemID`, `epo:hasSerialID` | `adms:Identifier` | e.g. GTIN for `hasItemStandardID` | VERIFIED [S6] |
| `epo:hasCertification` → `epo:Certificate` | 0..* | | VERIFIED [S6] |
| `epo:hasSpecification` → `epo:ProductSpecification` | 0..* | | VERIFIED [S6] |
| `epo:hasHazardousItemUNDGCode` (skos:Concept), `epo:hasHazardousClassID` (adms:Identifier) | 0..* | UN dangerous-goods classification (relevant for ammunition transport) | VERIFIED [S6] |
| `epo:hasQualifiedItemRelation` → `epo:ItemRelation` | 0..* | parts/accessories/replacements | VERIFIED [S6] |
| `epo:DimensionInformation`: `epo:concernsItem` → AbstractItem; `epo:hasMeasuredAttributeCode` (skos:Concept, **1..1**); `epo:hasMeasure` / `epo:hasMinimumMeasure` / `epo:hasMaximumMeasure` → `epo:Quantity` | | Second, coded way to state physical dimensions | VERIFIED [S6] L948 |

### 2.5 Where quantities sit outside the item

| Context | Property | Source |
|---|---|---|
| Notice lot / procedure | `epo:Purpose epo:hasTotalQuantity epo:Quantity` | VERIFIED [S2] |
| Any line (`epo:CatalogueLine`, `epo:OrderLine`, `epo:RequestForOfferLine` …) | `epo:Line epo:specifiesItem epo:AbstractItem` (1..1) | VERIFIED [S6] |
| Order line | `epo:OrderLine epo:hasQuantity epo:Quantity` (**1..1**), `epo:hasPrice`, `epo:refersToCatalogueLine` | VERIFIED [S8] eOrdering shapes |
| Catalogue line | `epo:hasMaximumOrderQuantity`, `epo:hasMinimumQuantityGuaranteedForDelivery` (→ Quantity), `epo:hasOrderableUnit` (skos:Concept), `epo:hasPrice` → `epo:Price` | VERIFIED [S6] |
| Request-for-offer line | `epo:isOptional` (boolean) | VERIFIED [S8] eRequest shapes |
| Contract | `epo:Contract epo:specifiesDeliverable epo:Deliverable`; Deliverable `epo:hasQuantity` (0..1), `epo:hasTotalValue` | VERIFIED [S8] eContract shapes L43 |

No property connects `epo:Lot`, `epo:Procedure` or `epo:Purpose` to `epo:Item` / `epo:AbstractItem`. I scanned every SHACL property shape in 5.2.0 whose `sh:class` is an item or catalogue class. The only owners are AbstractItem, Batch, Catalogue, CatalogueResponseLine, DimensionInformation, EnvironmentalEmissionInformation, ItemProperty, ItemRelation, Line, OrderLine, RequestForOffer and Contract (→ Deliverable). VERIFIED [S6][S8]

---

## 3. eForms SDK: fields that describe the item

### 3.1 Fields (SDK 1.15.1; same IDs and code lists in 2.0.0-alpha.4)

| Field ID | Name (exact) | Type | Code list | XPath (Lot variant) | Source |
|---|---|---|---|---|---|
| BT-262-Procedure / -Lot / -Part | Main Classification Code | code | `cpv` (hierarchical) | `…/cac:ProcurementProject/cac:MainCommodityClassification/cbc:ItemClassificationCode` | VERIFIED [S13][S15] |
| BT-263-Procedure / -Lot / -Part | Additional Classification Code | code | `cpv` (hierarchical) | `…/cac:AdditionalCommodityClassification/cbc:ItemClassificationCode` | VERIFIED [S13][S15] |
| BT-26(m)-… / BT-26(a)-… | Classification Type (e.g. CPV) | code | `classification-type` | `…/cbc:ItemClassificationCode/@listName` | VERIFIED [S13] |
| BT-25-Lot | Quantity | number | – | `…/cac:ProcurementProject/cbc:EstimatedOverallContractQuantity` | VERIFIED [S13] |
| BT-625-Lot | Unit | code | `measurement-unit` (flat) | `…/cbc:EstimatedOverallContractQuantity/@unitCode` | VERIFIED [S13] |
| BT-23-… | Main Nature | code | `contract-nature` | `…/cbc:ProcurementTypeCode[@listName='contract-nature']` | VERIFIED [S13] |
| BT-21-… / BT-24-… / BT-300-… | Title / Description / Additional Information | text-multilingual | – | `cbc:Name` / `cbc:Description` / `cbc:Note` | VERIFIED [S13] |

- BT-262-Lot has a rule: if BT-23 = `supplies`, the CPV must start with 0–3, 40–44 or 48. VERIFIED [S13] (rule BR-BT-00262-0203)
- BT-25 and BT-625 exist only at Lot level, not at Procedure or Part level. VERIFIED [S13]
- There is **no eForms field for item attributes** (calibre, material, standard). I searched field names and XPaths for "Item" and "Quantity". The only other hits are BT-811(b) "EED List (Item)" and BT-815 "Energy Efficiency Quantity" (energy-efficiency statistics). VERIFIED [S13]
- Item details therefore live only in free text: BT-21 title, BT-24 description, BT-300 note. INFERENCE (follows from the field list; matches §1.4)

### 3.2 Code lists

| Code list | Content | Source |
|---|---|---|
| `classification-type` (v1.0) | **One code only: `cpv`** "Common Procurement Vocabulary". Empty canonical URI | VERIFIED [S14] |
| `cpv` | `cpv_2008_core`, listId `http://data.europa.eu/cpv/Cpv2008/Code`, 9,454 rows. Our classes 35310000, 35320000, 35330000, 35340000 are present with their children (e.g. 35331500 Cartridges, 35321200 Rifles, 35341000 Parts of light firearms) | VERIFIED [S14] |
| `measurement-unit` (version 20250924-0) | listId `http://publications.europa.eu/resource/authority/measurement-unit`. **Only 12 codes**: GRM, H87, HUR, KGM, KMK, KTM, LTR, MGM, MLT, MTK, MTR, TNE | VERIFIED [S14] |
| `cpvsuppl` | **Not in the SDK.** Code lists with "cpv" in the name are `cpv.gc` and `cpv_social-service-cpv.gc` only, in both 1.15.1 and 2.0.0-alpha.4 | VERIFIED [S14][S15] |

---

## 4. CPV supplementary vocabulary (`cpvsuppl`)

### 4.1 Structure

- Concept scheme URI: `http://data.europa.eu/cpv/cpvsuppl`. Concept URIs: `http://data.europa.eu/cpv/cpvsuppl/AA26`. Each concept has `skos:notation` (e.g. `AA26`), `dc:identifier`, `skos:broader` (AA26 → AA), multilingual `skos:prefLabel`, `owl:versionInfo "2008"`. VERIFIED [S22] (Q1)
- It is **not** under `http://publications.europa.eu/resource/authority/`; that URI has 0 concepts. VERIFIED [S22] (Q14)
- Code format: two letters (section, group), two digits (attribute), and a check digit, e.g. `MB02-8`. VERIFIED [S23] §5.2. The SPARQL notations have no check digit (`FB01`). VERIFIED [S22]
- 19 sections. The explanatory notes say 42 groups; SPARQL returns 43 two-letter group concepts. 965 concepts with English labels in total. VERIFIED [S24][S22]
- "The supplementary codes are generic and are not designed to fit a specific code in the Main Vocabulary." VERIFIED [S24] p.3
- Usage: each main CPV code "can be further detailed … by using several codes chosen among those contained in the Supplementary Vocabulary" (example: 34110000-1 Passenger cars + MB02-8 Right-hand-drive + CA36-8 Euro 5). VERIFIED [S23] §6.2

| Section | Label | Groups |
|---|---|---|
| A | Materials | AA Metal and alloy, AB Non-Metal |
| B | Form, shape, packaging and conditioning | BA Form, BB Shape, BC Packaging and conditioning |
| C | Materials/products with special qualities and operation mode | CA special qualities, CB Mode of operation |
| D | General, Administration | DA |
| E | Users/beneficiaries | EA |
| F | Designated use | FA Educational, **FB Security use**, FC Waste, FD Season, FE Postal, FF Cleaning, FG Other |
| G | Scale and dimension | GA Dimension and power indication, GB Frequency, GC Other indication |
| H–L | Residual attributes: food; construction/works; computing/IT; energy & water; medical & laboratory | HA, IA, JA, KA, LA |
| M | Residual attributes for transport | MA, MB, MD, ME, MF |
| P–U | Renting; advertising & legal; research; financial; printing; retail | PA–PB, QA–QB, RA–RD, SA–SC, TA, UA–UB |

VERIFIED [S22] (Q2), [S23] Annex 2

### 4.2 Codes relevant to weapons and ammunition (English labels, VERIFIED [S22] Q3)

| Code | Label | Use for us (INFERENCE) |
|---|---|---|
| FB01 | For military use | tag: military end use |
| FB02 | For weapons and ammunition | tag |
| FB03 | For the police force | tag: police end use |
| FB13 | With military specifications | tag: built to MIL spec |
| FB09 / FB12 | For security system / For terrorist prevention | rarely |
| AA04 Copper, AA06 Lead, AA11 Steel, AA12 Carbon-steel, AA27 Tungsten, AA44 Brass | materials | case or core material |
| AB06 Plastic, AB37 Carbon | materials | polymer cases, composites |
| CA03 | Bullet-proof | protective items (outside our 4 classes) |
| CA68 | Armoured | |
| DA44 | Spare parts | parts family (35340000) |
| GA09 | Specific diameter required | generic placeholder, carries no value |
| GC04 / GC27 / GC31 | A minimum of / Number of items: / Number of units: | generic qualifiers, carry no value |

- There is **no calibre, cartridge-type, bullet-type or NATO/STANAG code**. A scan of all 965 English labels for "calib", "ammun", "cartridge", "ballist" found nothing apart from FB02 and BC22 "In cartridges" (a packaging code). VERIFIED [S22]
- OP-TED does not map supplementary codes to ePO. The standard-forms mapping says: "Not mapping supplementary CPV codes, as EPO does not provide a way to group them with 'regular' CPV codes. See issues #119 and #205." VERIFIED [S20] L224, L748
- Issue #205: "This association is not supported in the EPO, and it is not foreseen to be supported in the future. Therefore, the mapping to supplementary CPV is dropped forever." VERIFIED [S21]
- The ePO issue on pairing CPV and supplementary codes (#410) was closed with "no mapping is required". VERIFIED [S12]
- The eForms mapping packages ship a `cpvsuppl.json` resource, but no mapping file uses it (0 references in `Lot.rml.ttl`, `Procedure.rml.ttl`, `controlled-lists.rml.ttl`). VERIFIED [S18][S16]

---

## 5. `epo-knowledge` repo: files relevant to items, classification and quantities

The repo has only `v4.2.0/` today. Its `manifest.json` lists modules **`ePO_core` and `eNotice` only**. So eCatalogue classes are missing. VERIFIED [S28]

Files that exist (paths relative to `v4.2.0/`). VERIFIED [S28]

| Kind | Files |
|---|---|
| classes | `classes/Purpose.md`, `classes/Quantity.md`, `classes/ProcurementObject.md`, `classes/ProcurementElement.md`, `classes/Lot.md`, `classes/LotGroup.md`, `classes/Procedure.md`, `classes/PlannedProcurementPart.md`, `classes/TechnicalSpecification.md`, `classes/Contract.md`, `classes/ContractTerm.md`, `classes/Estimate.md`, `classes/MonetaryValue.md`, `classes/CatalogueProvider.md`, `classes/CatalogueReceiver.md` |
| object properties | `object-properties/hasPurpose.md`, `hasMainClassification.md`, `hasAdditionalClassification.md`, `hasContractNatureType.md`, `hasAdditionalContractNature.md`, `hasTotalQuantity.md`, `hasQuantity.md`, `hasUnitCode.md`, `hasNetQuantity.md`, `hasBaseQuantity.md`, `hasMaximumOrderQuantity.md`, `hasMinimumQuantityGuaranteedForDelivery.md`, `hasQualifiedValue.md`, `hasItemClassificationIdentifier.md`, `hasItemStandardID.md`, `hasManufacturerItemID.md`, `hasSellerItemID.md`, `hasBuyerItemID.md`, `hasCountryOfOrigin.md`, `hasProcurementScopeDividedIntoLot.md`, `foreseesProcurementObject.md`, `hasEstimatedValue.md` (all under `object-properties/`) |
| datatype properties | `datatype-properties/hasQuantityValue.md`, `datatype-properties/hasUnitDescription.md` |

Missing from `epo-knowledge` (exist in the ePO TTL): classes `Item`, `AbstractItem`, `ItemProperty`, `Catalogue`, `CatalogueLine`, `DimensionInformation`; properties `hasItemClassification`, `hasAttributeType`, `hasLiteralValue`, `hasClassificationScheme`, `describesItem`, `hasMeasure`. VERIFIED [S28]
Some property files point to these missing classes, e.g. `hasQualifiedValue.md` has "Domain: epo:ItemProperty" and `hasItemClassificationIdentifier.md` has "Domain: epo:Item". VERIFIED [S28]
`hasMainClassification.md` has no definition line (v4.2.0 has none). VERIFIED [S28]

For our RAG we must add the eCatalogue terms ourselves, from [S5][S6]. INFERENCE

---

## 6. Answers to the six questions in one place

| # | Question | Answer |
|---|---|---|
| 1 | How is the subject of a procurement modelled? | `Procedure`/`Lot` (and PIN `PlannedProcurementPart` → `ProcurementObject`) `epo:hasPurpose` → `epo:Purpose` with `hasMainClassification` (1..*), `hasAdditionalClassification` (0..*), `hasTotalQuantity` → `Quantity`. VERIFIED §1 |
| 1b | Other schemes than CPV? | Not on `Purpose`: CPV-only by SHACL in 4.x, by definition in 5.2.0, by code list in eForms, and by filter in TED's mapping. Custom schemes are allowed only on items via `epo:hasItemClassification`. VERIFIED §1.2, §2.4 |
| 2 | Item / catalogue / ordering modules? | Yes: eCatalogue, eOrdering, eFulfilment, eContract, eRequest. `epo:Item` ⊂ `epo:AbstractItem`; attributes via `epo:ItemProperty` (string name + string or Quantity value). VERIFIED §2 |
| 3 | eForms fields and code lists? | BT-262/263 (`cpv`), BT-26 (`classification-type` = only `cpv`), BT-25 quantity, BT-625 unit (`measurement-unit`, 12 codes). No item-attribute fields. No `cpvsuppl` list. VERIFIED §3 |
| 4 | cpvsuppl? | 19 sections, 965 concepts at `http://data.europa.eu/cpv/cpvsuppl`. FB01/FB02/FB03/FB13 and metal codes apply. No calibre codes. Not present in TED RDF or eForms. VERIFIED §4 |
| 5 | epo-knowledge files? | See §5. eCatalogue terms are missing. VERIFIED |
| 6 | Recommendation | §7 (INFERENCE, built on the facts above) |

---

## 7. Recommendation: ePO-compatible product families (INFERENCE unless tagged)

### 7.1 Principles

1. **Do not touch `Purpose` semantics.** Store TED's CPV codes exactly as published: one main and many additional CPV URIs per procedure/lot purpose. Never write our family code into `hasMainClassification` or `hasAdditionalClassification`. Reason: it would fail ePO 4.x SHACL and contradict the 5.2.0 definition (VERIFIED §1.2).
2. **Level 1 = CPV class** (35310000, 35320000, 35330000, 35340000). Compute it from the lot's main CPV by walking `skos:broader` (e.g. 35331500 → 35331000 → 35330000). `skos:broader` exists in the CPV data (VERIFIED §1.3). Keep a small lookup table instead of live SPARQL.
3. **Level 2 = product family = an `epo:Item` classification in our own SKOS scheme.** `epo:hasItemClassification` is the one ePO property made for any scheme ("CPV, UNSPC, eCl@ss, GPC, ...", VERIFIED §2.4).
4. **Family attributes = `epo:ItemProperty` rows.** This is the ePO pattern for "attribute from a classification scheme + value" (VERIFIED §2.3). We play the role ECLASS plays in the ePO examples: our family scheme defines the attributes.
5. **Units = `measurement-unit` authority table URIs** (`http://publications.europa.eu/resource/authority/measurement-unit/<CODE>`). This matches `epo:hasUnitCode` in 4.x SHACL and TED data (VERIFIED §1.2, §1.4).

### 7.2 Family concept scheme

- One `skos:ConceptScheme`, e.g. `<SP_BASE>/scheme/product-family` (`SP_BASE` = a base URI we control; not chosen here).
- One `skos:Concept` per family, e.g. `<SP_BASE>/product-family/AMMO-SC` with `skos:notation "AMMO-SC"`, `skos:prefLabel "Small-calibre cartridges"@en`, `skos:inScheme`.
- Link to CPV with **`skos:broadMatch`** to the level-1 CPV concept (`http://data.europa.eu/cpv/cpv/35330000`). Optionally add `skos:closeMatch` / `skos:narrowMatch` to finer CPV codes (35331500 Cartridges, 35331100 Bullets). SKOS says mapping properties are the convention for links between concepts in different schemes (VERIFIED [S30]).
- Keep family codes stable and independent of CPV notation, so a family can move if we re-cut level 1.

### 7.3 Where the family code is attached

| Our object | ePO pattern to mirror | Family goes in |
|---|---|---|
| TED lot (demand from a notice) | `Lot hasPurpose Purpose` (CPV) **plus** a derived `epo:Item` per lot. ePO has no Lot→Item link (VERIFIED §2.5), so the link is ours: `sp:derivedFromLot` | `item.hasItemClassification` = {family concept, the lot's CPV concepts} |
| `sp:Demand` line (prime asks to buy) | `epo:RequestForOfferLine` → `epo:specifiesItem` → `epo:Item`; `epo:isOptional` | same |
| `sp:Offer` line (supplier offers) | `epo:CatalogueLine` → `specifiesItem`; `hasPrice`, `hasMaximumOrderQuantity`, `hasMinimumQuantityGuaranteedForDelivery` | same |
| `sp:Deal` line | `epo:OrderLine` → `specifiesItem`; `hasQuantity` (1..1), `hasPrice`; or `epo:Contract specifiesDeliverable epo:Deliverable` | same |
| `sp:Capability` (what a company can make) | No ePO class. Point the capability at a family concept (and optionally an `epo:Item` template with properties) | `capability.family` |

Optional: add cpvsuppl tags (FB01, FB02, FB13, AA44 …) to `hasItemClassification` too. The property has no scheme limit. Do not expect TED to supply them (VERIFIED §4.2).

### 7.4 Attribute storage (Postgres)

Two tables define the schema. Two hold the values. Column comments give the ePO term each column stands for.

```sql
-- Level 2 definition (our SKOS scheme)
create table product_family (
  code            text primary key,          -- skos:notation, e.g. 'AMMO-SC'
  uri             text unique not null,      -- skos:Concept URI in our scheme
  cpv_class       text not null,             -- level 1: CPV URI, e.g. 'http://data.europa.eu/cpv/cpv/35330000' (skos:broadMatch)
  cpv_close       text[] default '{}',       -- finer CPV URIs (skos:closeMatch/narrowMatch)
  label_en        text not null
);

-- Fixed attribute schema per family (our "attribute dictionary"; ePO has no class for this)
create table family_attribute (
  family_code     text references product_family(code),
  attr_code       text,                      -- -> epo:hasAttributeType (and 5.x epo:hasClassificationCode)
  label_en        text not null,
  value_kind      text check (value_kind in ('text','code','quantity','range')),
  unit_code       text,                      -- measurement-unit code, e.g. 'MMT', for quantity/range
  allowed_values  text[],                    -- for value_kind = 'code' (stored as literal strings)
  required        boolean default false,     -- could map to 5.x epo:hasImportanceCode (meaning not defined by ePO)
  primary key (family_code, attr_code)
);

-- epo:Item / epo:AbstractItem
create table item (
  id              uuid primary key,
  family_code     text references product_family(code),  -- epo:hasItemClassification (our scheme)
  classification  text[] default '{}',       -- epo:hasItemClassification: CPV and cpvsuppl URIs
  title           text,                      -- free text from BT-21/BT-24 or the offer
  source_lot_uri  text                       -- sp:derivedFromLot (TED lot URI), null for marketplace items
);

-- epo:ItemProperty
create table item_property (
  item_id         uuid references item(id),  -- epo:describesItem
  scheme_uri      text not null,             -- epo:hasClassificationScheme (our family scheme URI, as a string)
  attr_code       text not null,             -- epo:hasAttributeType
  literal_value   text,                      -- epo:hasLiteralValue
  qty_value       numeric,                   -- epo:hasQualifiedValue -> epo:Quantity/epo:hasQuantityValue
  min_value       numeric,                   -- 5.x epo:hasMinimumValue -> Quantity
  max_value       numeric,                   -- 5.x epo:hasMaximumValue -> Quantity
  unit_code       text,                      -- epo:hasUnitCode (measurement-unit code)
  evidence        jsonb,                     -- sp: provenance (source text span, confidence) - our extension
  check (literal_value is not null or qty_value is not null or min_value is not null or max_value is not null)
);
```

Rules that follow from ePO (VERIFIED basis in §2.3):
- Text and coded values go to `literal_value`. ePO has no coded (URI) value type, so codes like `FMJ` are plain strings. The allowed list lives in `family_attribute.allowed_values`.
- Numbers with units go to `qty_value` + `unit_code`. ePO says: use the qualified value when the property has units.
- Ranges use `min_value` / `max_value` (ePO 5.x only). For a 4.2.0 export, write two properties or the midpoint. Not covered by ePO 4.2.0.
- **Quantity ordered is not an item attribute.** It belongs on `Purpose.hasTotalQuantity` (TED), `OrderLine.hasQuantity` (deal) or `Deliverable.hasQuantity` (contract). Keep it on the demand/offer/deal line, not in `item_property`.

### 7.5 Worked example: family "Small-calibre cartridges" (level 1 = 35330000)

| attr_code | value_kind | unit (measurement-unit) | Example value | Note |
|---|---|---|---|---|
| `calibre_designation` | code | – | `5.56x45mm NATO` | the trade name of the calibre; a designation, not a number |
| `bore_diameter` | quantity | `MMT` millimetre | 5.56 | MMT is in the authority table (VERIFIED [S22] Q6) |
| `cartridge_type` | code | – | `ball`, `tracer`, `AP`, `blank`, `training` | from lot text, e.g. "naboi ślepych" = blank |
| `bullet_construction` | code | – | `FMJ`, `JSP`, `steel-core` | |
| `bullet_mass` | quantity | `GRM` gram | 4.0 | |
| `muzzle_velocity` | quantity | `MTS` metre per second | 940 | MTS is in the table, not in the eForms 12 |
| `case_material` | code | – | `brass` | also tag item with cpvsuppl `AA44` |
| `standard` | text | – | `STANAG 4172` | |
| `packaging` | text | – | `linked M27`, `boxed 20` | |
| `end_use` | code | – | `military` | also cpvsuppl `FB01` |

Line quantity (not an attribute): count in `H87` piece, as TED does (VERIFIED §1.4). The table has no "round" or "C62 one" unit (VERIFIED [S22] Q6), so H87 is the only count unit.

Same lot as RDF, if we ever export (ePO terms VERIFIED; `sp:` terms and URIs are ours):

```turtle
<lot/LOT-0001>  epo:hasPurpose [ a epo:Purpose ;
                  epo:hasMainClassification <http://data.europa.eu/cpv/cpv/35331500> ;
                  epo:hasTotalQuantity [ a epo:Quantity ; epo:hasQuantityValue 40000000 ;
                     epo:hasUnitCode <http://publications.europa.eu/resource/authority/measurement-unit/H87> ] ] .
<item/1> a epo:Item ; sp:derivedFromLot <lot/LOT-0001> ;
   epo:hasItemClassification <SP_BASE/product-family/AMMO-SC> , <http://data.europa.eu/cpv/cpv/35331500> ,
                             <http://data.europa.eu/cpv/cpvsuppl/FB01> .
[] a epo:ItemProperty ; epo:describesItem <item/1> ;
   epo:hasClassificationScheme "SP_BASE/scheme/product-family" ; epo:hasAttributeType "bore_diameter" ;
   epo:hasQualifiedValue [ a epo:Quantity ; epo:hasQuantityValue 5.56 ;
      epo:hasUnitCode <http://publications.europa.eu/resource/authority/measurement-unit/MMT> ] .
[] a epo:ItemProperty ; epo:describesItem <item/1> ;
   epo:hasClassificationScheme "SP_BASE/scheme/product-family" ; epo:hasAttributeType "cartridge_type" ;
   epo:hasLiteralValue "blank" .
```

### 7.6 Choices the sources leave open (INFERENCE)

- **`hasAttributeType` vs `hasClassificationCode`.** Both are strings. The ePO examples put an ECLASS IRDI plus name in `hasAttributeType`. I suggest our `attr_code` in `hasAttributeType`, and the same code in `hasClassificationCode` when exporting to 5.x.
- **`DimensionInformation` vs `ItemProperty`.** ePO also has `DimensionInformation` with a required `hasMeasuredAttributeCode` (skos:Concept). There is no published code list for it (no `measured-attribute` authority table found, Q14). For one uniform schema, use `ItemProperty` for all attributes.
- **ePO 4.2.0 vs 5.2.0.** TED data follows 4.x. The item model is in both, but min/max values, `hasClassificationCode` and `hasImportanceCode` exist only in 5.x. Our columns cover both. Write 5.x terms only when exporting to 5.x.
- **Extraction.** TED gives only CPV plus free text for these classes (VERIFIED §1.4, §3.1). So every TED-side `item_property` is an extracted claim. Keep `evidence` (source span, confidence) on each row, in line with our `sp:Claim` design.

---

## 8. Not confirmed / caveats

- I found **no `epo:ItemRequest` class**, although two definitions mention it. VERIFIED absent [S8].
- I could not find a name/title property for `epo:Item` in 5.2.0 shapes. ePO 4.2.0 shapes use `dcterms:title` (1..1 on Item, 0..1 on ItemProperty). [S6][S7]
- The meaning of `epo:hasImportanceCode` is only "the significance … expressed as a code". No code list is named. [S6]
- The ePO 4.2.0 definition of `hasItemClassification` names the range `at-voc-new:item-classification`. No such authority table exists at `http://publications.europa.eu/resource/authority/item-classification` (0 concepts). [S7][S12 #612][S22 Q14]
- I did not open the HTML conceptual-model reports (eCatalogue, eOrdering …) linked from [S11]. All facts come from the TTL/SHACL files.
- EUR-Lex blocked automated access to Regulation (EC) 213/2008. CPV structure facts come from the TED-hosted CPV guide and SV notes [S23][S24], which the TED SIMAP page links [S25].
- Group count: the SV notes say 42 groups; SPARQL returns 43. [S24][S22]
- The SEMIC page lists ePO v5.2.0 with release date 10/03/2026; GitHub shows the v5.2.0 release on 2025-10-21. [S29][S9]
- I found no OP-TED statement on when TED Open Data will move from ePO 4.x to 5.x.

---

## Appendix A. SPARQL queries used (endpoint [S22], all with LIMIT)

| ID | Purpose | Result |
|---|---|---|
| Q1 | `<http://data.europa.eu/cpv/cpvsuppl/AA26> ?p ?o` | structure of a cpvsuppl concept |
| Q2 | cpvsuppl concepts with notation length ≤ 2, English labels | 19 sections, 43 groups |
| Q3 | all cpvsuppl English labels (LIMIT 1200) | 965 rows |
| Q6 | measurement-unit concepts with English labels | 52 concepts, incl. H87, MMT, CMT, GRM, KGM, MTS, JOU |
| Q7 | `?purpose epo:hasMainClassification cpv/35331500 . ?obj epo:hasPurpose ?purpose` | Lots and Procedures |
| Q8/Q9/Q11 | lot title, purpose, quantity, notice number | examples in §1.4 |
| Q10 | purposes with main 35331500 that have additional CPV or quantity | quantities in H87 |
| Q12 | additional classifications grouped, for 5 of our main CPVs | all CPV |
| Q13 | properties of `cpv/35331500` | inScheme, broader, notation |
| Q14 | concept counts for `/authority/item-classification`, `/authority/cpvsuppl`, `/authority/classification-type`, `/authority/measured-attribute` | all 0 |

Example (Q12):
```sparql
PREFIX epo: <http://data.europa.eu/a4g/ontology#>
SELECT ?main ?add (COUNT(*) AS ?n) WHERE {
  VALUES ?main { <http://data.europa.eu/cpv/cpv/35330000> <http://data.europa.eu/cpv/cpv/35331000>
                 <http://data.europa.eu/cpv/cpv/35320000> <http://data.europa.eu/cpv/cpv/35340000>
                 <http://data.europa.eu/cpv/cpv/35310000> }
  ?purpose epo:hasMainClassification ?main ; epo:hasAdditionalClassification ?add .
} GROUP BY ?main ?add ORDER BY DESC(?n) LIMIT 20
```

---

## Sources (all opened on 2026-10-30)

| Tag | URL |
|---|---|
| S1 | https://github.com/OP-TED/ePO/blob/v5.2.0/implementation/ePO_core/owl_ontology/ePO_core.ttl (Purpose L602, Quantity L615, hasAdditionalClassification L1412, hasMainClassification L2431, hasPurpose L2973, hasUnitCode L3562) |
| S2 | https://github.com/OP-TED/ePO/blob/v5.2.0/implementation/ePO_core/shacl_shapes/ePO_core_shapes.ttl (Contract-hasPurpose L2586, ProcurementElement-hasPurpose L4601, Purpose shapes L4831–4855, Quantity-hasUnitCode L4944) |
| S3 | https://github.com/OP-TED/ePO/blob/v4.2.0/implementation/ePO_core/shacl_shapes/ePO_core_shapes.ttl#L5928-L5953 (CPV scheme constraint); #L5072 (unit); #L857 (contract nature) |
| S3b | https://github.com/OP-TED/ePO/blob/v4.0.0/implementation/ePO_core/shacl_shapes/ePO_core_shapes.ttl#L3952-L3997 |
| S4 | https://github.com/OP-TED/ePO/blob/v4.2.0/implementation/ePO_core/owl_ontology/ePO_core.ttl#L2230 |
| S5 | https://github.com/OP-TED/ePO/blob/v5.2.0/implementation/eCatalogue/owl_ontology/eCatalogue.ttl (Item L71, ItemProperty L105, describesItem L199, hasAttributeType L234, hasClassificationScheme L299, hasItemClassification L397, hasLiteralValue L420, hasQualifiedValue L559, AbstractItem L760) |
| S6 | https://github.com/OP-TED/ePO/blob/v5.2.0/implementation/eCatalogue/shacl_shapes/eCatalogue_shapes.ttl (hasItemClassification L375, DimensionInformation L948, ItemProperty L1066–L1143) |
| S7 | https://github.com/OP-TED/ePO/blob/v4.2.0/implementation/eCatalogue/shacl_shapes/eCatalogue_shapes.ttl (ItemPropertyShape L168, hasLiteralValue L206, ItemShape L454); https://github.com/OP-TED/ePO/blob/v4.2.0/implementation/eCatalogue/owl_ontology/eCatalogue.ttl#L338 |
| S8 | https://github.com/OP-TED/ePO/tree/v5.2.0/implementation (README module list; eOrdering, eFulfilment, eRequest, eContract TTL and shapes; eContract.ttl L23, L85) |
| S9 | https://github.com/OP-TED/ePO/releases (v5.2.0 2025-10-21; v4.2.0 2024-12-02) |
| S10 | https://docs.ted.europa.eu/EPO/latest/release-notes.html |
| S11 | https://docs.ted.europa.eu/EPO/latest/conceptual.html |
| S12 | https://github.com/OP-TED/ePO/issues/341, https://github.com/OP-TED/ePO/issues/410, https://github.com/OP-TED/ePO/issues/773, https://github.com/OP-TED/ePO/issues/612 |
| S13 | https://github.com/OP-TED/eForms-SDK/blob/1.15.1/fields/fields.json |
| S14 | https://github.com/OP-TED/eForms-SDK/blob/1.15.1/codelists/classification-type.gc, https://github.com/OP-TED/eForms-SDK/blob/1.15.1/codelists/cpv.gc, https://github.com/OP-TED/eForms-SDK/blob/1.15.1/codelists/measurement-unit.gc |
| S15 | https://github.com/OP-TED/eForms-SDK/tree/2.0.0-alpha.4 (fields, codelists); https://github.com/OP-TED/eForms-SDK/releases |
| S16 | https://github.com/OP-TED/ted-rdf-mapping-eforms/blob/develop/mappings/package_eforms_sdk1.13_epo4.0/transformation/mappings/Lot.rml.ttl (quantity L3085–3148; CPV L3916–3971) |
| S17 | https://github.com/OP-TED/ted-rdf-mapping-eforms/blob/develop/mappings/package_eforms_sdk1.13_epo4.0/metadata.json |
| S18 | https://github.com/OP-TED/ted-rdf-mapping-eforms/tree/develop/mappings/package_eforms_sdk1.13_epo4.0/transformation/resources (cpv.json, cpvsuppl.json, measurement-unit.json); controlled-lists.rml.ttl in `../mappings/` |
| S19 | https://github.com/OP-TED/ted-rdf-mapping-eforms/blob/develop/mappings/package_eforms_sdk1.13_epo4.0/transformation/mappings/Procedure.rml.ttl#L992-L1036 ; https://github.com/OP-TED/ted-rdf-mapping-eforms/blob/develop/mappings/package_eforms_sdk1.13_epo4.0/transformation/mappings/PlannedProcurementPart-pin.rml.ttl#L433-L481 |
| S20 | https://github.com/OP-TED/ted-rdf-mapping/blob/main/src/mappings/s2_object.rml.ttl#L223-L236 and #L747-L760 |
| S21 | https://github.com/OP-TED/ted-rdf-mapping/issues/119 , https://github.com/OP-TED/ted-rdf-mapping/issues/205 |
| S22 | https://publications.europa.eu/webapi/rdf/sparql (live queries, Appendix A) |
| S23 | https://ted.europa.eu/documents/d/ted/cpv_2008_guide_en (Guide to the CPV, §5.1, §5.2, §6.2, Annex 2) |
| S24 | https://ted.europa.eu/documents/d/ted/cpv_sv_2008_explanatory_notes_en (SV 2008 explanatory notes) |
| S25 | https://ted.europa.eu/en/simap/cpv |
| S26 | https://op.europa.eu/en/web/eu-vocabularies/dataset/-/resource?uri=http://publications.europa.eu/resource/dataset/cpv |
| S27 | https://op.europa.eu/en/web/eu-vocabularies/concept-scheme/-/resource?uri=http://publications.europa.eu/resource/authority/measurement-unit (version 20250924-0) |
| S28 | https://github.com/OP-TED/epo-knowledge/tree/main/v4.2.0 and https://github.com/OP-TED/epo-knowledge/blob/main/v4.2.0/manifest.json |
| S29 | https://interoperable-europe.ec.europa.eu/collection/eprocurement/solution/eprocurement-ontology |
| S30 | https://www.w3.org/TR/skos-reference/#mapping (§10.1, §10.6.1) |
