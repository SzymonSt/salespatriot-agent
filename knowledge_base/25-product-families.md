# Product families: two-level taxonomy for our four CPV classes

`[TEAM DECISION — proposed]` for the taxonomy. Data facts are `[VERIFIED 2026-10-30]` (TED pull and
SPARQL queries that day) unless marked. Answers the open question "which capability taxonomy do we
start with" ([18](18-open-questions.md)).

Machine-readable source of truth: [`../data/families.yaml`](../data/families.yaml), deployed to Supabase
(`product_*` tables) by [`../seed_product_taxonomy.py`](../seed_product_taxonomy.py). How items get
classified live: [26-product-classification-pipeline.md](26-product-classification-pipeline.md).

## Decisions in one place
1. **Two levels.**
   - Level 1 = the CPV class (35310000, 35320000, 35330000, 35340000), plus an `out_of_scope` bucket.
   - Level 2 = product family: **28 families in focus + 7 out of scope**.
2. **Precision comes from attributes, not depth.** Each family has a fixed attribute schema. Calibre,
   round type, part type and similar fields do the fine filtering. A third level is added only by the
   rule in §3.
3. **Compatible with ePO.**
   - CPV stays on the procurement `Purpose`, exactly as TED publishes it.
   - Our family is a concept in our own SKOS scheme (`urn:salespatriot:product-family:<id>`), attached to
     an `epo:Item` through `epo:hasItemClassification`.
   - Attributes follow the `epo:ItemProperty` name/value pattern.
   - Quantity sits on the line, not in the attributes. Units come from the EU `measurement-unit` table.
4. **Every family carries a regulatory crosswalk** (EU Common Military List, NATO supply class, HS
   heading, Firearms Directive category, ADR). It drives the D2/D3/D8 checks in [22](22-kyc-and-deal-due-diligence.md).
5. **The taxonomy changes only by a human-approved proposal.** Observations flow in automatically
   ([10](10-ontology-data-model.md) rule).

## 1. What TED and the OP-TED stack give us
Primary sources: OP-TED GitHub (ePO v4.2.0/v5.2.0, eForms SDK 1.15.1, ted-rdf-mapping) and the
Publications Office SPARQL endpoint. Full notes with line references:
[research/2026-10-30-epo-item-model.md](research/2026-10-30-epo-item-model.md). The essentials:

| Fact | Consequence for us | Source |
|---|---|---|
| The subject of a procedure or lot is `epo:Purpose` with `hasMainClassification` (1..*), `hasAdditionalClassification` (0..*), `hasTotalQuantity` → `epo:Quantity` | CPV per lot is the only structured "what" in TED | [ePO_core.ttl v5.2.0](https://github.com/OP-TED/ePO/blob/v5.2.0/implementation/ePO_core/owl_ontology/ePO_core.ttl) |
| Both classification properties are **CPV only**: SHACL in ePO 4.x, the definition in 5.2.0, code list `classification-type` = {`cpv`} in eForms, and TED's mapping drops any other list | Never write our family code into `Purpose` | [ePO 4.2.0 shapes L5928](https://github.com/OP-TED/ePO/blob/v4.2.0/implementation/ePO_core/shacl_shapes/ePO_core_shapes.ttl#L5928-L5953), [eForms codelists](https://github.com/OP-TED/eForms-SDK/blob/1.15.1/codelists/classification-type.gc) |
| eCatalogue has `epo:Item` with `epo:hasItemClassification` (any scheme: "CPV, UNSPC, eCl@ss, GPC") and `epo:ItemProperty` (`describesItem`, `hasAttributeType`, `hasLiteralValue` or `hasQualifiedValue` → Quantity with unit) | Family = item classification in our scheme; attributes = ItemProperty rows | [eCatalogue.ttl v5.2.0](https://github.com/OP-TED/ePO/blob/v5.2.0/implementation/eCatalogue/owl_ontology/eCatalogue.ttl) |
| No ePO property links `Lot`/`Purpose` to `Item` | We add `sp:derivedFromLot` | eCatalogue / eOrdering shapes v5.2.0 |
| Items appear in `CatalogueLine`, `RequestForOfferLine`, `OrderLine` (→ `specifiesItem`) and `Contract` → `Deliverable` | Offer ≈ CatalogueLine, Demand ≈ RequestForOfferLine, Deal ≈ OrderLine | [ePO v5.2.0 modules](https://github.com/OP-TED/ePO/tree/v5.2.0/implementation) |
| eForms has **no item-attribute fields**. Quantity is BT-25 and unit is BT-625 (a 12-code list), at lot level only | Calibre, type and standard exist only in free text (BT-21 title, BT-24 description); extraction is unavoidable | [eForms fields.json](https://github.com/OP-TED/eForms-SDK/blob/1.15.1/fields/fields.json) |
| Live TED RDF example: lot "Cartucho 9 x 19 mm Ordinario" carries only CPV 35331500; a PL lot "5,56 mm naboi ślepych" has quantity 40,000,000 `H87` | Count units are `H87` (piece); there is no "round" unit | SPARQL, 2026-10-30 |
| CPV supplementary vocabulary (`cpvsuppl`, 965 concepts) has tags FB01 "For military use", FB02 "For weapons and ammunition", FB03 "For the police force", FB13 "With military specifications", AA44 "Brass", DA44 "Spare parts". It has **no calibre codes**, and TED never publishes cpvsuppl ("dropped forever", ted-rdf-mapping #205) | We derive these tags ourselves (`product_cpvsuppl_tags`) | [ted-rdf-mapping #205](https://github.com/OP-TED/ted-rdf-mapping/issues/205), SPARQL |
| The EU `measurement-unit` table has 52 codes, incl. H87, KGM, MTR, LTR, GRM, MMT, MTS, JOU, SEC (no SET, no KMT) | Units for attributes and lines | SPARQL, 2026-10-30 |
| `epo-knowledge` (LLM-ready ePO) covers only ePO_core and eNotice v4.2.0; the eCatalogue terms are missing | Add `Item`, `ItemProperty`, `hasItemClassification` definitions to our RAG ourselves | [epo-knowledge](https://github.com/OP-TED/epo-knowledge/tree/main/v4.2.0) |

## 2. What our data says
**TED pull, 2026-10-30.** All notices in the four classes, supplies only, queries from [21](21-focus-scope.md):
2,230 competition and 2,610 result notices, with 3,083 and 1,810 lot titles (`title-lot`, or `title-proc` when there are no lots).

| Finding | Competition | Result | Consequence |
|---|---|---|---|
| Notices whose focus CPVs are only class-level (e.g. just 35330000) | 911 (41%) | 1,092 (42%) | CPV often stops at level 1; the family must come from text |
| Lot titles matching none of our multilingual product keywords | 1,615 (52%) | 790 (44%) | Many titles are empty labels ("Los 1 2023", "Časť 5"); read lot descriptions too |
| Lot titles with a calibre | 805 (26%) | 568 (31%) | Calibre is the strongest single signal for ammunition |
| Calibre bands (lot titles): < 20 mm / 20–57 mm / ≥ 60 mm | 515 / 158 / 134 | 425 / 74 / 73 | Small calibre dominates; medium and large are real but smaller |
| Top calibres | 5.56x45 (137), 9x19 (119), 7.62x51 (82), 155 mm (66), 30 mm (46), 40 mm (39), 12 ga (32) | 9x19 (133), 5.56x45 (94), 7.62x51 (51), 4.6x30 (30), 7.62x39 (25), 155 mm (22) | Seeded in `product_calibres` |
| Distinct calibres | 74 | 68 | Long tail → calibre is an attribute, not a family level |

CPV leaf codes (notices, competition / result): 35331500 Cartridges 440 / 426 · 35331000 Ammunition for
firearms and warfare 272 / 330 · 35340000 Parts 161 / 263 · 35320000 Firearms 127 / 162 · 35331300
Grenades 119 / 146 · 35321100 Hand guns 111 / 117 · 35310000 Misc weapons 98 / 95 · 35321200 Rifles
82 / 76 · 35341000 Parts of light firearms 75 / 123 · 35321300 Machine guns 51 / 63 · 35322400 Mortars
41 / 53 · 35333100 Bombs 37 / 37 · 35331200 Shells 36 / 31 · 35331400 Land mines 24 / 15 · 35341100
Gunmetal pipe fittings 23 / 12 · 35331100 Bullets 21 / 19 · 35311100 Swords 18 / 18 · other codes < 18.

Rough family sizes from multilingual keyword stems over lot titles (competition / result lots). This is
a sizing aid only, not a classifier:
- rifles 143 / 100 · training and blank 133 / 68 · components 116 / 35 · pistols 104 / 76 · spare parts 101 / 97
- pyrotechnics and signals 94 / 53 · explosives and initiation 75 / 29 · grenades 52 / 43 · optics and accessories 38 / 18 · sniper 37 / 20
- artillery 28 / 22 · shotguns 28 / 24 · bladed 27 / 17 · mortar 24 / 13 · less-lethal 17 / 11 · machine guns 13 / 26 · launchers 8 / 12

**Noise inside the focus CPVs:**
- `35341100` Gunmetal pipe fittings is plumbing ([21](21-focus-scope.md)); 33 notices carry it as their only focus code.
- **New:** `35311400` Lances is used for fire-hose nozzles ("Lance à débit variable", French fire services). 11 of the 14 notices with this code are fire-fighting or water tenders.
- 35310000 notices also carry police uniforms, medals and close-combat kit.
- Non-focus CPVs that co-occur and help classification: 39225500 Pyrotechnics (66 notices), 24613000 Signalling flares (50), 24600000 Explosives (29), 24615000 Fuses, caps, igniters and detonators (20), 18925200 Holsters (19), 38633000 Telescopic sights (17), 35815100 Bullet-proof vests (31), 35811200 Police uniforms (30), 50620000 Repair of firearms (16).

**Supplier catalogue** (`data/profiles/suppliers.json`, 20 suppliers, 339 items):
- 227 different `specs` keys, 148 of them used once. This sprawl is what the fixed schemas fix.
- One calibre has up to four spellings ("9×19 mm", "9×19 mm Luger", "Parabellum", "NATO").
- 159 of 339 items are outside our four classes (less-lethal, protective, optics, explosives, services).
- Evidence status: 233 `document_verified`, 82 `declared`, 24 `inferred`.

## 3. When a family exists (rules)
- **Create or keep a family** only if both hold:
  - **(a) Enough data:** about 30 notices or lots a year in TED, or a clear platform need.
  - **(b) It differs from its neighbours** in at least one of: a different attribute schema, a different
    regulatory or transport regime (licence category, ADR class, Firearms Directive), or a different
    supplier base or buyer type.
- **Merge** families that stay thin and share attributes (e.g. all heavy ordnance in one family, all
  misc weapons in two).
- **Never split by a value of an attribute** (calibre, colour, brand). Use the attribute.
- **Ids are stable** (`<class>.<family>`). Never rename; deprecate and add a new one.
- **Split** an existing family only when the backtest ([23](23-self-learning-rag.md)) shows better
  recall@5 / MRR or a better price estimate. This is the rule from our earlier discussion: data first, then depth.

## 4. Level 1
| Class | CPV | Note |
|---|---|---|
| `ammunition` | 35330000 | Also takes explosives and pyrotechnics found in the same notices (CPV 24…, 39225500) |
| `firearms` | 35320000 | CPV puts artillery and mortars here too |
| `parts` | 35340000 | Includes accessories and weapon sights bought under parts CPVs |
| `misc_weapons` | 35310000 | Thin and noisy in TED |
| `out_of_scope` | — | Classified and stored, never matched in the demo; seeds for future classes (e.g. 35800000) |

## 5. Level 2 families
Match keys are hard filters when both sides state them. **Bold** = required on platform listings (never forced on TED extraction).

### Ammunition (13)
| Family | Covers | Match keys | CPV | Evidence |
|---|---|---|---|---|
| `ammunition.small_calibre` | Pistol, rifle and MG cartridges below 20 mm (incl. 12.7 and 14.5 mm) | **calibre**, round_type | candidate 35331500, 35331000, 35331100 | Calibre < 20 mm in 515 / 425 lot titles |
| `ammunition.shotgun` | 12/16/20-gauge buckshot, slug, birdshot, breaching | **calibre**, shotgun_load | candidate 35331500, 35331000 | 12 ga in 32 / 16 lot titles; often outside the military list |
| `ammunition.training_blank` | Blank, marking (FX/Simunition/UTM), reduced-range, frangible, dummy | **calibre**, round_type | candidate 35331500, 35331000 | 133 / 68 lot titles |
| `ammunition.less_lethal` | Impact, irritant, flash-bang/stun, less-lethal shotgun and 37/40 mm | calibre, **payload_type** | candidate 35331300, 35331000, 35331500 | 34 catalogue items; police buyers |
| `ammunition.medium_calibre` | 20–57 mm cannon rounds (not 40 mm grenades) | **calibre**, round_type | candidate 35331000, 35331200, 35332000 | ~120 / 40 lot titles |
| `ammunition.grenade_40mm` | 40x46 LV and 40x53 HV grenades | **calibre**, round_type | candidate 35331300, 35331000 | 40 mm in ~60 / 34 lot titles |
| `ammunition.hand_grenade` | Fragmentation, blast, smoke, illumination, incendiary, practice hand grenades; rifle grenades | **grenade_type** | candidate 35331300 | CPV Grenades 119 / 146 notices |
| `ammunition.mortar` | 60–120 mm mortar bombs, charges, fuzes | **calibre**, round_type | candidate 35331200, 35331000 | 24 / 13 lot titles + 60/81/120 mm |
| `ammunition.artillery` | Loaded projectiles, complete rounds, propelling charges ≥ 75 mm (artillery, tank, naval), not mortar | **calibre**, round_type, component_type | candidate 35331200, 35331000, 35332000 | 155 mm in 66 / 22 lot titles |
| `ammunition.heavy_ordnance` | Bombs, rockets, missiles, loitering munitions, torpedoes, sea mines, decoys, land mines | **ordnance_type**, calibre | exact 35331400, 35332x00, 35333x00 | ~100 notices per kind of notice |
| `ammunition.pyrotechnics` | Signal and illumination flares, smoke signals, distress, simulators, thunderflashes | **signal_type** | candidate 35331000; related 39225500, 24613000 | 94 / 53 lot titles |
| `ammunition.explosives_demolition` | Bulk/plastic explosives, charges, detonators, det cord, shock tube | **explosive_form**, initiation_system | related 24600000, 24610000, 24615000 | 75 / 29 lot titles |
| `ammunition.components` | Bullets, cases, primers, propellant, fuzes, boosters, links, unfilled bodies | **component_type**, calibre | candidate 35331100 (buyers also use "Bullets" for complete rounds), 35331000; related 24615000 | 116 / 35 lot titles; Tier-2/3 inputs for D7 |

### Firearms (9)
| Family | Covers | Match keys | CPV | Evidence |
|---|---|---|---|---|
| `firearms.handguns` | Pistols, revolvers | **calibre**, action | exact 35321100 | 111 / 117 notices |
| `firearms.submachine_guns` | SMGs, PDWs | **calibre** | candidate 35321000, 35321200 | 4.6x30 in 15 / 30 lot titles |
| `firearms.rifles_carbines` | Assault rifles, carbines, battle and semi-auto rifles, training rifles | **calibre**, action | candidate 35321200, 35321000 | 82 / 76 notices |
| `firearms.precision_rifles` | Sniper, DMR, anti-materiel | **calibre**, action, weapon_role | candidate 35321200, 35321000 | 37 / 20 lot titles |
| `firearms.machine_guns` | LMG, GPMG, HMG | **calibre**, weapon_role | exact 35321300 | 51 / 63 notices |
| `firearms.shotguns` | Pump, semi-auto, break-action | **calibre**, action | candidate 35321000, 35321200 | 28 / 24 lot titles |
| `firearms.launchers` | Grenade launchers (incl. less-lethal), recoilless and shoulder-launched rocket weapons | **calibre**, weapon_role | candidate 35321000, 35322000 | 8 / 12 lot titles + model names |
| `firearms.mortars` | Mortar systems | **calibre** | exact 35322400 | 41 / 53 notices |
| `firearms.artillery_systems` | Towed/self-propelled guns and howitzers, AA guns, rocket artillery | **calibre**, weapon_role | exact 35322100/200/300/500; candidate 35322000 | ~50 / 60 notices |

### Parts (4)
| Family | Covers | Match keys | CPV | Evidence |
|---|---|---|---|---|
| `parts.small_arms_parts` | Spare parts and sub-assemblies (barrels, bolts, trigger groups, kits) | **part_type**, **compatible_platforms**, calibre | candidate 35341000 | 75 / 123 notices |
| `parts.accessories_tools` | Magazines, suppressors, muzzle devices, mounts, slings, holsters, bipods, cleaning kits, armourer tools | **part_type**, compatible_platforms, calibre | candidate 35341000; related 18925200 | Holsters CPV in 19 notices |
| `parts.sights_optics` | Weapon sights, night/thermal weapon sights, clip-ons, laser aiming, fire control | **sight_technology**, compatible_platforms | candidate 35341000, 35340000; related 38633000 | 28 catalogue items; e.g. PL CKE-1N sights, DK Carl Gustaf M4 FCS |
| `parts.heavy_weapon_parts` | Parts for mortars, artillery, cannons, rocket launchers | **weapon_class_served**, **compatible_platforms** | exact 35342000, 35343000 | 18 / 25 notices (HOMAR-K, Krab/REGINA) |

### Miscellaneous weapons (2)
| Family | Covers | Match keys | CPV | Evidence |
|---|---|---|---|---|
| `misc_weapons.bladed` | Swords and sabres (mostly ceremonial), daggers, bayonets, knives, machetes, lances | **blade_type** | exact 35311000/100/200/300; candidate 35311400 (noise rule) | Swords 18 / 18 notices |
| `misc_weapons.other_weapons` | Gas pistols and launchers, irritant sprays, signal and flare pistols, line-throwing guns, conducted-energy weapons, air guns, airsoft/paintball, crossbows | **misc_weapon_type** | exact 35312000 | Gas guns 9 / 12 notices |

### Out of scope (7, `in_focus = false`)
`protective_equipment` · `law_enforcement_equipment` · `optronics` (hand-held, not weapon-mounted) ·
`uniforms_clothing` · `ranges_targets` · `services` · `noise` (fire-fighting, plumbing, sports, building materials, medals).

## 6. Boundary rules (put these in the classifier prompt)
| Case | Goes to | Why |
|---|---|---|
| 12.7 mm and 14.5 mm cartridges | `small_calibre` | Same regime: ML3, FSC 1305, ADR "small arms" ≤ 19.1 mm. For the **weapons**, the military list splits at 12.7 mm (ML1 vs ML2); that is an item rule (§7) |
| Signal and flare pistols, line-throwing guns | `misc_weapons.other_weapons` | FSC 1095; excluded from ML2.b and from the Firearms Directive definition of a firearm |
| 40x46 / 40x53 grenades | `grenade_40mm`; 40 mm L70 cannon rounds → `medium_calibre` | Different weapons and buyers |
| 40 mm, 37 mm or 12 ga impact/irritant rounds | `less_lethal` | Different effect, buyers and often regime |
| Stun, flash-bang, irritant grenades | `less_lethal`; lethal and practice hand grenades → `hand_grenade` | Effect decides |
| Hand-thrown smoke grenade | `hand_grenade` (grenade_type smoke); hand-held or launched signals → `pyrotechnics` | Item form decides |
| Practice rounds of 20 mm and above | Their calibre family with `round_type = practice` | Not a separate family |
| Marking and blank small-arms rounds | `training_blank` | Conversion kits and suppliers differ |
| Mortar bombs | `ammunition.mortar`; 120 mm tank rounds → `artillery` | Check the context for 120 mm |
| Fuzes, primers, unfilled shell bodies, powder | `components`; loaded projectiles and modular charges → `artillery` | Tier-2/3 inputs vs. system munitions |
| Weapon-mounted sights (incl. thermal clip-ons) | `parts.sights_optics`; hand-held observation devices and goggles → `out_of_scope.optronics` | Mounting decides |
| Tasers / conducted-energy weapons | `misc_weapons.other_weapons` | Seen in police lots under 35310000 |
| 35311400 "Lances" in a fire-fighting or water tender | `out_of_scope.noise` | Fire-hose nozzles |
| Ceremonial swords and sabres | `misc_weapons.bladed`, `ceremonial = true` | Most sword notices are ceremonial |
| Anti-personnel land mines | `heavy_ordnance`, and every deal ESCALATEs | Ottawa Convention obligations differ between EU states (`[OWN KNOWLEDGE]`) |
| A lot with several products | Dominant product; the others go to `extra.other_products` | One family per item |

## 7. Regulatory and standards crosswalk
Stored in `product_family_crosswalk`. There are 177 non-CPV rows, of which 143 are verified in the source; every row has its source URL.
Status per row:
- `[V]` verified in the source;
- `[V2]` secondary source only;
- `[I]` our inference (`verified = false`).

Full research with every source and quote:
- [research/2026-10-30-defence-taxonomies.md](research/2026-10-30-defence-taxonomies.md)
- [research/2026-10-30-epo-item-model.md](research/2026-10-30-epo-item-model.md)

Sources used:
- EU Common Military List: Council, 23 Feb 2026, OJ C/2026/1640. The OJ page itself was not opened.
- NATO FSC: GSA PSC manual FY2025, which follows DLA H2.
- CN 2026: Reg. (EU) 2025/1926.
- Firearms Directive (EU) 2021/555 and Reg. (EU) 2025/41.
- ADR 2025 Table A.
- STANAGs via public Czech national adoptions (ČOS).

| Family | EU Military List | NATO FSC | HS / CN 2026 | ADR (typical) |
|---|---|---|---|---|
| small_calibre | ML3.a | 1305 | 9306 30 10 / 30 30 / 30 90 | UN0012 1.4S |
| shotgun | ML3.a only for ML1.b weapons | 1305 | 9306 21 00 | UN0012 1.4S |
| training_blank | ML3.a; crimped blanks and pierced dummies excluded (Note 2) | 1305 | 9306 30 90 (I) | UN0014 1.4S; practice UN0362 1.4G |
| less_lethal | ML4.a if military-designed; RCA payloads ML7.d | 1330 (V2) | 9306 90 90 (I) | UN0301 1.4G (tear) |
| medium_calibre | ML3.a | 1305 (≤ 30 mm), 1310 (> 30 mm) | 9306 30 30 (I) | UN0328 / UN0348 by type |
| grenade_40mm | ML3.a (ML4.a N.B.) | 1310 (V2) | 9306 30 30 or 90 10 (I) | UN0348; practice UN0362 |
| hand_grenade | ML4.a | 1330 | 9306 90 10 (I) | UN0284/0285/0292/0293; practice UN0110 1.4S |
| mortar | ML3.a | 1310 (60 mm), 1315 (81/120 mm) | 9306 90 10 (I) | by type (I) |
| artillery | ML3.a | 1315 (75–125 mm), 1320 (> 125 mm) | 9306 90 10 (I) | projectiles UN0168…; charges UN0279… |
| heavy_ordnance | ML4.a | 1325 bombs, 1340 rockets, 1345 land mines, 1351 sea mines, 1356 torpedoes | 9306 90 10 (I) | UN0034, UN0181, UN0137, UN0329 |
| pyrotechnics | ML4.a if military; signal cartridges excluded from ML3 | 1370 | 3604 90 00 | UN0373, UN0405 (1.4S), UN0403 |
| explosives_demolition | ML4.a charges; ML8 energetic materials | 1375, 1376 | 3602 00 00, 3603 40 00, 3603 50 00 | per article (not researched) |
| components | ML3 Note 1; ML8 propellants; ML16 forgings | 1305, 1390 fuzes/primers, 1395 | 3601 00 00, 3603 30 00 | UN0044 primers, UN0055 cases, UN0509 powder, UN0367 fuzes |
| handguns | ML1.a | 1005 | 9302 00 00 | — |
| submachine_guns | ML1.a | 1005 | 9301 90 00 | — |
| rifles_carbines | ML1.a | 1005 | 9301 90 00 (military), 9303 30 00 (sporting) | — |
| precision_rifles | ML1.a ≤ 12.7 mm; ML2.a anti-materiel | 1005 | 9301 90 00 / 9303 30 00 (I) | — |
| machine_guns | ML1.a ≤ 12.7 mm; ML2.a > 12.7 mm | 1005 | 9301 90 00 | — |
| shotguns | ML1.b (military, auto, semi or pump; hunting/sport excluded) | 1005 (I) | 9303 20 95 / 9301 90 00 (I) | — |
| launchers | ML2.a | 1010 grenade launchers, 1055 rocket launchers | 9301 20 00 | — |
| mortars | ML2.a | 1010 (60 mm), 1015 (81/120 mm) | 9301 10 00 | — |
| artillery_systems | ML2.a | 1015 / 1020 / 1025 by bore | 9301 10 00 | — |
| small_arms_parts | ML1/ML2 "specially designed components"; ML16 | same as weapon, or 1090 | 9305 10 00 / 20 00 / 91 00 | — |
| accessories_tools | ML1.d / ML2.c (magazines, suppressors, mounts) | 1005 (I), 8465 belts | 9305 99 00 (I) | — |
| sights_optics | ML1.d.5–6 / ML2.c.1 | 1240, 5855 night vision | 9013 10 90 (unless presented with the weapon) | — |
| heavy_weapon_parts | ML2.a components | same as weapon, 1055 | 9305 91 00 | — |
| bladed | not in ML1–ML4 (I); national law | 1095 bayonets, 8465 swords, 5110 machetes | 9307 00 00 | — |
| other_weapons | ML2.b military gas projectors (not signal pistols); ML7.d | 1095 signal and line-throwing guns | 9303 90 00, 9304 00 00 | — |

**Rules at item level** (the calibre, the item and the buyer decide; implement in backlog T11 of [26](26-product-classification-pipeline.md)):
- **Weapons:** ML1 if calibre ≤ 12.7 mm (smooth-bore < 20 mm), otherwise ML2. Hunting or sporting smooth-bore weapons that are neither military-designed nor fully automatic are excluded.
- **Ammunition:**
  - for ML1/ML2 weapons → ML3.a;
  - grenade ammunition for ML1/ML2 launchers → ML3;
  - hand grenades, bombs, rockets, mines and military pyrotechnics → ML4.a;
  - crimped blanks, pierced dummies and signalling cartridges are not ML3.
- **FSC by bore:**
  - guns: 1005 ≤ 30 mm · 1010 > 30–< 75 · 1015 75–125 · 1020 > 125–150 · 1025 > 150–200;
  - ammunition: 1305 ≤ 30 · 1310 > 30–< 75 · 1315 75–125 · 1320 > 125;
  - fuzes and primers for 1310–1320 ammunition → 1390.
- **ADR:**
  - "small arms" = weapons ≤ 19.1 mm, and every shotgun cartridge counts → UN0012 1.4S is typical, blanks are UN0014.
  - 1.4S is transport category 4: unlimited quantity under the 1.1.3.6 exemption (no placards). The transport document, extinguishers and driver training still apply.
  - 1.4B–1.4G are transport category 2, with much less relief.
  - The real entry depends on the tested article (ADR 2.2.1.1.4), so a value stated by the supplier overrides the family default.
- **Firearms Directive:**
  - It does **not** apply to armed forces, police or public authorities (Art. 2(2)), which covers most TED buyers. For them the category is a product attribute, not a buyer check.
  - Ammunition follows its firearm (Art. 13(1)), and essential components take the firearm's category.
- **Licences (D3):** an item on the Military List needs an intra-EU transfer licence (Directive 2009/43/EC, whose annex equals the list) or an export licence (Common Position 2008/944/CFSP), whatever the buyer type (the combination is `[I]`). Civilian firearms export falls under Reg. 258/2012 today and Reg. (EU) 2025/41 from 12 Feb 2029 (some articles since 11 Feb 2025). Annex I of 2025/41 maps Directive categories to CN codes.
- **STANAG per calibre** is in `product_calibres.standard`:
  - 5.56x45 4172, 7.62x51 2310, 9x19 4090, 12.7x99 4383, 5.7x28 4509, 4.6x30 4820, 27x145 3820, 30x173 4624, 25x137 4173 (V2);
  - mortars, 105 and 155 mm: STANAG 4425 / AOP-29 Parts 2, 3 and 1;
  - 40 mm grenades: only the design-safety STANAG 4599.

**Where we deviate from the research proposal:**
- **14.5 mm cartridges stay in `small_calibre`.** The research suggested a separate sub-family. The ammunition regime is the same (ML3, FSC 1305, ADR small arms ≤ 19.1 mm) and the TED data is thin. The weapon split ML1/ML2 at 12.7 mm is an item rule.
- **Tank rounds stay in `artillery`.** Rockets, bombs, mines and torpedoes stay in `heavy_ordnance`. FSC is derived from calibre or `ordnance_type`.

**Gaps** (`[UNVERIFIED]`, see the research file):
- 8-digit CN codes for 40 mm, stun and mortar rounds: use the supplier's declared code or a binding tariff ruling.
- Calibre STANAGs for 14.5 mm, 30x113, 35x228, 40 mm L70, 57 mm, 40 mm grenades and the 120 mm mortar.
- Whether STANAG 4385 (120 mm tank) is still current.
- ML6 treatment of self-propelled artillery.
- Whether Regulation (EU) 2019/125 (anti-torture goods) covers some less-lethal items. Not researched.

## 8. Attributes
The full dictionary (59 keys with type, unit and enum) is in `data/families.yaml` and `product_attribute_defs`.
Each key has one meaning everywhere. Class-level keys are inherited by every family in the class.

| Group | Keys |
|---|---|
| Every class | end_use, condition, brand_model, country_of_origin (+ standards, nsn, part_number, packaging where relevant) |
| Ammunition (class) | un_number, adr_classification, net_explosive_quantity_g |
| Ammunition (families) | calibre, round_type, projectile_designation, bullet_weight_g, primer_type, case_material, lead_free, link_type, shotgun_load, shell_length_mm, fuze_type, component_type, max_range_m, grenade_type, fuze_delay_s, payload_type, ordnance_type, guidance, signal_type, colour, burn_time_s, explosive_type, explosive_form, initiation_system, propellant_type, material |
| Firearms | calibre, action, weapon_role (narrowed per family), barrel_length_mm, barrel_length_calibres, magazine_capacity, feed_type, rail_interface, optics_ready, mount_type, accuracy_moa, firearms_directive_category, weight_kg |
| Parts | part_type (narrowed per family), compatible_platforms, sight_technology, magnification, oem_authorised, weapon_class_served |
| Misc | blade_type, ceremonial, misc_weapon_type, muzzle_energy_j |

Not attributes: quantity and unit. They are line fields (`product_items.quantity`, `unit` ∈ {H87, KGM, MTR, LTR}),
following ePO (`Purpose.hasTotalQuantity`, `OrderLine.hasQuantity`).

## 9. Calibres
- `product_calibres` holds 57 codes: `kind`, bore, case length, `related_codes` and default family.
  Related codes are compatible with a warning: `.223` ~ `5.56x45`, `.308` ~ `7.62x51`, `.338` ~ `8.6x70`.
  Matching treats `related_codes` as "compatible, warn".
- Canonical form: `<bore>x<case>` (`5.56x45`, `7.62x54R`), `<n>mm` when no case is defined or stated
  (`155mm`, `60mm`), gauges as `12ga`, imperial as `.22LR`, `.308`.
- `product_normalise_calibre(text)` does the following:
  1. lower case, `,` → `.`, `×` → `x`, no spaces;
  2. alias table;
  3. code match;
  4. `AxB` pattern;
  5. `Nmm` pattern.

  Example: "7,62mm x 51" → `7.62x51`. Unknown spellings resolve to a pattern value. The proposal job
  collects them ([26 §5](26-product-classification-pipeline.md)).
- Context words decide two ambiguous bores: 120 mm (mortar or tank) and 105/122 mm (gun or rocket).

## 10. How our tables map to ePO (for export and for the agents' RAG)
| Ours | ePO |
|---|---|
| `product_families` (level 2) | `skos:Concept` in our scheme; `skos:broadMatch` → CPV class concept (`http://data.europa.eu/cpv/cpv/35330000`) |
| `product_family_crosswalk` scheme `cpv`, `exact` | `skos:closeMatch` / `skos:narrowMatch` to the CPV leaf |
| `product_items` | `epo:Item`; `epo:hasItemClassification` = {family URI, CPV URIs, cpvsuppl URIs in `item_classifications`}; `sp:derivedFromLot` for TED lots |
| `product_items.attrs` key/value | `epo:ItemProperty` (`describesItem`, `hasAttributeType` = key, `hasClassificationScheme` = our scheme; `hasLiteralValue` for text and codes, `hasQualifiedValue` + `hasUnitCode` for numbers with units) |
| `product_items.quantity`, `unit` | `epo:Quantity` on `Purpose.hasTotalQuantity` (TED) or `OrderLine.hasQuantity` (deal) |
| Offer / Demand / Deal lines | `CatalogueLine` / `RequestForOfferLine` / `OrderLine` → `specifiesItem` |
| `un_number` | `epo:hasHazardousItemUNDGCode` |
| `country_of_origin` | `epo:hasCountryOfOrigin` |

## 11. Open questions
- Which classifier model and how much LLM budget the backfill gets (`PLAN.md` §4 decision).
- Whether `firearms_directive_category` should be derived by rules now or left to the D2 check.
- Lot-level parsing of pre-eForms notices (before late 2023), whose XML schema differs. Count them before writing a parser.
- A base URI we control for the family scheme (the `urn:` form is a placeholder until then).
