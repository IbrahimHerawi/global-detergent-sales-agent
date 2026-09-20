# Conversation regression suite

This suite tests outcomes, tool calls, backend state, cart contents,
calculations, and artifacts. It intentionally does not require exact assistant
wording.

The offline suite replaces only OpenAI Responses API results with scripts. The
scripts still pass through the production agent loop, strict tool schemas,
allowlisted executor, repositories, quotation services, confirmation binding,
WeasyPrint renderer, and local artifact storage. It requires no OpenAI or Meta
credential and makes no paid-service call.

Run it with the Compose API image and Redis test database:

```console
docker compose run --rm -e APP_ENV=test -e REDIS_URL=redis://redis:6379/15 api uv run --frozen pytest tests/conversations -m "not live_model"
```

The live-model evaluation consumes the same scenario catalog. It is separately
marked and opt-in because scripted function calls do not prove actual model
behavior. Configure `OPENAI_API_KEY` and `OPENAI_MODEL` in the ignored `.env`,
then run:

```console
docker compose run --rm -e APP_ENV=test -e RUN_LIVE_MODEL_EVAL=1 api uv run --frozen pytest tests/conversations/test_live_model_evaluation.py -m live_model -rA
```

The pytest header reports the effective model, timeout, tool-loop limit, and
history limit. With JUnit XML enabled, the live test also records configuration
as suite properties. The evaluation explicitly checks factual support against
the tool outputs from that turn and distinguishes explicit confirmation from
ambiguous, tentative, premature, or post-preview modification language.

## Requirement traceability

| Scenario ID | Observable behavior | Source requirements |
|---|---|---|
| `TP66-A` | Company introduction uses company tool | TP §66 A; Specification §§1, 8, 21 |
| `TP66-B` | Office-floor discovery is catalog-backed | TP §66 B; Specification §§1, 21–22 |
| `SPEC-DETAILS` | Details and package come from product lookup | Specification §§1, 11, 21, 39 |
| `TP66-C` | 5 L floor-cleaner price is QAR 25.00 | TP §66 C; Specification §§15, 21, 39 |
| `TP66-D` | “I need 20” adds the selected product | TP §66 D; Specification §§14, 39; D-03 |
| `TP66-E` | Second product and two-line calculation | TP §66 E; Specification §§14–15, 39 |
| `TP66-F` | Quantity changes from 20 to 25 and replaces | TP §66 F, §24; D-03; SG-01 |
| `SPEC-REMOVE` | Product removal updates cart/state | Specification §§14, 20, 39; TP §42 |
| `SPEC-CUSTOMER` | Company name collection preserves transport phone | Specification §§18, 39; D-04 |
| `SPEC-PREVIEW` | Backend preview is delivered before confirmation | Specification §§19, 39; D-05; SG-03 |
| `TP66-G` | Unknown certification is not invented | TP §66 G; Specification §§11, 32 |
| `SPEC-MISSING-TECHNICAL` | Missing dilution/contact time remain unavailable | Specification §§11, 32; TP §39 |
| `SPEC-UNKNOWN-PRODUCT` | Unknown product search remains empty | Specification §§1–2, 32 |
| `SPEC-AMBIGUOUS-SELECTION` | Ambiguous cleaner does not mutate cart | Specification §§19, 23; TP §34 |
| `SPEC-TENTATIVE-QUANTITY` | “I might need 20” does not mutate cart | Specification §§19, 23; TP §34 |
| `TP66-H` | Premature yes/model create attempt cannot generate | TP §66 H, §21; Specification §19; D-05 |
| `SPEC-MODIFY-AFTER-PREVIEW` | Modification invalidates preview/confirmation | Specification §19; TP §24; SG-01 |
| `TP66-I` | Explicit confirmation creates correct ID/total/PDF | TP §66 I; Specification §§17, 19, 30, 39 |
| `SPEC-EXPIRED-SESSION` | Expired Redis state starts a fresh session | Specification §12; TP §11 |

`tests/conversations/scenarios.py` is the executable source of this mapping.
Its completeness test requires all TP §66 A–I identifiers, all journey steps,
unique IDs, and at least one recognized source reference per case.
