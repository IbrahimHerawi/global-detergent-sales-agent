# Authoritative Implementation Contract

## 1. Status and authority

This document is the implementation contract for the Global Detergent Factory AI Sales Agent MVP. It reconciles:

- `D:/GDF/MVP System Specification.txt` (the **Specification**), sections 1–40; and
- `D:/GDF/Technical Implementation Plan.txt` (the **Technical Plan**), sections 1–70.

The source documents remain the product and technical baseline. This contract changes only implementation order and the development execution environment, while resolving conflicting examples and underspecified behavior. It does not expand the MVP. If source examples conflict, the decisions in §3 are authoritative. If this contract is silent, the source requirement still applies. Traceability is in [requirements-matrix.md](requirements-matrix.md).

Normative words (`must`, `must not`, `required`) are binding. Suggested examples remain suggestions unless this contract or a source requirement makes them binding.

## 2. Product and architecture boundary

The MVP is one modular-monolith Python 3.12 FastAPI application plus Redis, run as two Docker Compose services (`api` and `redis`). FastAPI contains the API, OpenAI orchestration, deterministic business services, static-data repositories, PDF generation, and direct Meta WhatsApp Cloud API integration. Redis contains temporary sessions, idempotency claims, and short-lived operational state. There are no application microservices or multi-agent application architecture.

The AI controls language understanding and conversation. Python and validated structured data control company/product truth, safety claims, price, money, customer records, quotation state, quotation identifiers, and official actions. The model must use registered tools for authoritative facts and cannot bypass backend validation.

The supported customer journey is company/product discovery, supported recommendations, pricing, multi-item cart construction and modification, customer-detail collection, backend-calculated preview, explicit confirmation, PDF generation, and WhatsApp PDF delivery. Static JSON is the business-data source. Missing facts are reported as unavailable.

## 3. Authoritative decisions and conflict resolutions

| ID | Decision | Source relationship |
|---|---|---|
| D-01 | Use one Python 3.12 FastAPI application and Redis. | Selects the Specification §§5, 7, 35 and Technical Plan §§1–2, 57–58 modular-monolith interpretation. |
| D-02 | `Company` uses the Technical Plan §7 nested `contact` object. The Specification §8 flat example fields (`phone`, `email`, `website`, `address`) are normalized under `contact` when JSON is created. | Resolves conflicting schema examples without dropping fields. |
| D-03 | `add_item` increments the quantity of an existing cart item. `update_item_quantity` replaces the existing quantity. | Resolves the Technical Plan §19 “increase or replace” ambiguity and aligns the two distinct tools. |
| D-04 | Required customer identity is at least one nonblank `name` **or** nonblank `company_name`, plus the transport-supplied phone. Whitespace-only values do not qualify. | Makes Specification §18 and Technical Plan §§9, 21 validation precise. |
| D-05 | `prepare_quotation` validates and calculates the preview, records the preview revision, sets `QUOTE_REVIEW`, and returns the preview with `quote_confirmed=false`. Successful delivery/presentation of that preview transitions to `AWAITING_CONFIRMATION`, still false. A later explicit customer confirmation, bound to the current preview revision, sets `quote_confirmed=true` while remaining in `AWAITING_CONFIRMATION`. Generation requires both that state and flag. | Reconciles Specification §13 with Technical Plan §§21, 23 and tool §§45–47. “Successful delivery” means the transport has accepted/successfully returned the preview response, not merely that it was calculated. |
| D-06 | Processed-message keys are `gdf:processed-message:{message_id}` with TTL 604800 seconds (seven days). | Selects Technical Plan §12 over the Specification §27 underscore example and makes its “several days” exact. |
| D-07 | Development chat uses Technical Plan §52 fields: request `session_id`, `message`; response `response`, `state`, `selected_product_id`, `cart`, `generated_quotation_id`. | Resolves the Specification §28 example (`message`, `quotation`) conflict. |
| D-08 | `issued_at`, quotation date, quotation-ID date component, and validity calculation use UTC. `valid_until` is the UTC quotation date plus configured `validity_days`. | Resolves Technical Plan §22 UTC/local-business alternative and avoids host-timezone dependence. |
| D-09 | All money uses `Decimal`, quantized with `ROUND_HALF_UP` to two decimal places at defined calculation boundaries. Floating point is forbidden for monetary calculation. | Makes Specification §§9, 15 and Technical Plan §20 binding. |
| D-10 | Optional customer fields include `contact_person` and `notes`, in addition to optional email and address. | Preserves Specification §18 “Contact person” and “Customer notes”, which Technical Plan schemas omitted. |
| D-11 | Product categories may be more specific than company-level category labels; startup validation must not require exact equality of those lists. | Resolves the Specification §§8–10 example mismatch while retaining category validation. |
| D-12 | Development/internal routes (`/api/v1/dev/chat`, quotation development routes, and any equivalent diagnostics) are unavailable in production—preferably not registered, otherwise unconditionally rejected. | Hardens the Specification §29 and Technical Plan §§52–54 “development/internal” designation. Product customer exposure remains read-only. |
| D-13 | Demo data must be explicitly marked unverified development data and must never be represented as verified commercial data. Unknown real business fields remain `null`/omitted. | Enforces Specification §§8, 10, 32 and the acceptance condition against fabrication. |

## 4. Added safeguards

The following are contract safeguards added to reliably satisfy existing requirements. They are not represented as explicit source-document requirements:

- **Preview revision tracking.** Maintain a monotonic quote revision (or equivalent immutable digest), record the revision shown in the preview, and bind confirmation to it. Any quote-relevant change increments/changes the revision, clears the recorded preview and confirmation, and requires a new preview. This prevents a stale confirmation from authorizing changed items, prices, rules, or customer details.
- **Atomic message claim.** Deduplication is a single Redis atomic claim such as `SET key value NX EX 604800`, performed before side effects. Do not implement separate “exists” then “set” operations. Claim ownership/status must make retry behavior explicit; a duplicate cannot repeat agent, quotation, text, or PDF actions.
- **Preview-delivery transition boundary.** Calculation alone does not prove that the customer saw a preview. The application records `AWAITING_CONFIRMATION` only after the preview is successfully handed to the relevant response transport.
- **Data provenance marker.** Development catalog/company records and generated artifacts derived from them carry an internal, test-visible provenance marker or equivalent banner/metadata so they cannot be mistaken for verified commercial data.
- **Production route gating.** Internal route absence is checked at startup and by integration tests, not left only to deployment convention.

These safeguards implement the same scope; they do not add customer-facing capabilities.

## 5. Development and validation workflow

Docker is the execution environment from the first bootstrap task. Docker Desktop with Compose and an editor are the only host prerequisites. Host Python, `uv`, Redis, WeasyPrint, or PDF system libraries must not be required.

Python commands, dependency locking with `uv`, formatting/linting, mypy, pytest, FastAPI, Redis, Jinja2, and WeasyPrint run inside containers. Documentation-only inspection and Docker/Compose commands may run on the host. Repository scripts and README commands must express container invocations.

Early bootstrap success proves only that the current bootstrap surface operates. It is not evidence that the MVP or Technical Plan §68 is complete. Startup validation is introduced incrementally as schemas/repositories become available and is fully wired once all dependencies exist. Final evidence must come from the Task 50 completion gate.

## 6. Data and business rules

- Company, products, and quotation rules load from validated, read-only JSON. Real values that have not been supplied remain absent or null; no phone, email, address, product fact, certification, approval, safety statement, efficacy claim, instruction, price, discount, tax, or term may be fabricated.
- Demo products (approximately 8–12 is acceptable) are useful for development only. Their labels, documentation, fixtures, and generated output must not imply verification by Global Detergent Factory.
- Product search is deterministic and case-insensitive across stored name, SKU, category, description, applications, and keywords; it returns only active repository products. Recommendations may use model interpretation but must select only products returned by the repository.
- The cart stores product ID and positive integer quantity, never authoritative price. Current repository prices and rules are read on preview/generation. Adding increments; updating replaces; removal and every quote-relevant mutation reset preview/confirmation.
- Money follows D-09. Discount defaults to zero; tax, currency, validity, prefix, and terms come from `quotation_rules.json`.
- Quotation IDs are backend-generated, UTC-dated, collision-checked, and safe as filenames. Customer-controlled text never controls a path.
- Final generation requires a nonempty valid cart, D-04 customer identity and phone, the current preview revision, `AWAITING_CONFIRMATION`, and `quote_confirmed=true`. Success creates the final quotation/PDF and enters `QUOTE_GENERATED`.
- The PDF contains all fields required by Specification §30 and derives totals from the same quotation object as the backend, not recalculation in the template.

## 7. Sessions, messaging, and agent rules

- Session keys are `gdf:session:{normalized_phone}` (development transport uses a synthetic identity). Session TTL is configurable, initially 86400 seconds, and refreshed on save. Context is bounded to configurable recent messages.
- WhatsApp supports inbound text and outbound text/PDF. Unsupported inbound media gets a safe text-only explanation. Non-message events are ignored appropriately.
- GET and POST WhatsApp webhook routes implement verification, validated parsing, atomic claim, session/agent execution, state persistence, and outbound delivery. The POST route returns HTTP 200 promptly; synchronous processing is acceptable for MVP.
- Meta and OpenAI calls use timeouts and safe error handling. Standard automated tests mock paid/external services.
- The prompt enforces concise professional conduct, backend factual authority, missing-information disclosure, explicit confirmation, and no human-handoff offer. The tool executor has an allowlist, Pydantic argument validation, no dynamic execution, and a configurable bounded tool loop (initial maximum 8).
- Natural-language confirmation is a semantic judgment and therefore requires model evaluation for wording beyond the conservative canonical fallback. Backend state checks cannot prove that a customer message expresses confirmation; they instead ensure that any interpreted confirmation belongs to the current session and a later customer turn, and is bound to the successfully delivered, unchanged preview. The confirmation tool accepts no state, delivery, fingerprint, or authorization arguments.
- Logging is structured and includes request/message/session/tool/state/quotation correlation while masking phone numbers and excluding secrets, authorization headers, and full sensitive conversation content by default in production.

## 8. Preserved MVP exclusions

The following remain out of scope: human handoff or sales-agent fallback; CRM; ERP; inventory synchronization or real-time warehouse quantities; payments or online checkout; customer accounts; administration dashboard or product-management UI; PostgreSQL or any persistent database other than Redis; vector databases; RAG; LangChain; CrewAI; LlamaIndex; agent frameworks; multi-agent application architecture; local LLMs; Kubernetes; inbound voice processing; image recognition; dynamic discount approval; order fulfillment; and any capability presented as one of these under another name. Celery/job queues are not added for v1 unless separately authorized after demonstrated latency need.

## 9. Revised 51-task backlog

Task numbers are stable traceability identifiers. A task is complete only with its scoped tests/evidence; later tasks may add the full validation that earlier dependencies could not yet support.

| Task | Deliverable and completion boundary |
|---:|---|
| 01 | Authoritative implementation contract and requirements matrix (this task); no application functionality. |
| 02 | Repository layout, `pyproject.toml`, Python 3.12 constraint, locked runtime/dev dependencies, ignore files. |
| 03 | Dockerfile/Compose developer workflow with API and healthy Redis; all Python/uv/quality/test/PDF commands containerized. |
| 04 | Typed settings, `.env.example`, secret handling, environment/timeouts/paths/model/TTL configuration. |
| 05 | FastAPI application skeleton, route registration foundation, `/health`, lifespan foundation, `/docs`. |
| 06 | Structured logging, correlation IDs, exception taxonomy/mapping, baseline input/security controls. |
| 07 | Company/contact schemas and provenance-safe `company.json`, including flat-to-nested normalization decision. |
| 08 | Product schemas and clearly unverified demo `products.json`, with Decimal-compatible price strings. |
| 09 | Quotation-rules schema/data for currency, prefix, UTC validity, discount, tax, and terms. |
| 10 | Read-only company/product/rules repositories, indexes, complete static-data startup validation. |
| 11 | Company service and safe company/category outputs/endpoints. |
| 12 | Product service and read-only list/detail endpoints with active-product handling. |
| 13 | Deterministic product search and catalog-only recommendation support. |
| 14 | Conversation/session/message schemas, states, bounded history, customer optional fields, revision fields. |
| 15 | Redis session service, normalized keys, serialization, configurable sliding TTL/reset/delete. |
| 16 | Atomic seven-day processed-message claim using the mandated hyphenated key. |
| 17 | Quotation/customer/cart/line/totals/preview/generated schemas and validation. |
| 18 | Cart service: add-increments, update-replaces, remove/clear, current-price authority, confirmation reset. |
| 19 | Decimal quotation calculator, discount/tax/total rules, two-place `ROUND_HALF_UP`. |
| 20 | Quotation state machine, preview revision/digest binding, delivery and explicit-confirmation transitions. |
| 21 | Preview/customer-information service with nonblank identity and transport-phone checks. |
| 22 | UTC quotation ID/final model generation, collision protection, generation authorization. |
| 23 | Development quotation preview/generate routes reusing services and unavailable in production. |
| 24 | Print-oriented Jinja2 quotation template and WeasyPrint service. |
| 25 | Safe volume-backed PDF storage, existence/validity checks, replaceable storage boundary. |
| 26 | WhatsApp payload schemas, event parser, metadata extraction, unsupported-content classification. |
| 27 | WhatsApp GET webhook verification with correct challenge/403 behavior. |
| 28 | WhatsApp POST orchestration route for messages/non-message events and prompt HTTP response. |
| 29 | Direct Meta client for outbound text, upload/document PDF delivery, timeouts, safe failures. |
| 30 | Idempotent WhatsApp side-effect orchestration, claim/retry policy, session save, duplicate suppression. |
| 31 | Agent system prompt and bounded context construction from backend session summaries. |
| 32 | Company/product discovery/detail/search tool schemas and handlers with factuality/safety restrictions. |
| 33 | Cart/customer/preview/confirmation/create tool schemas and handlers implementing D-03–D-05. |
| 34 | Allowlisted tool executor, Pydantic validation, safe business errors, unknown-tool rejection. |
| 35 | Async OpenAI Responses API loop, configurable model/timeouts, maximum iterations, safe failure response. |
| 36 | Development chat endpoint using Technical Plan §52 fields, same agent/services, unavailable in production. |
| 37 | Complete lifespan startup validation, quote directory initialization, Redis readiness endpoint. |
| 38 | Unit tests for schemas, repositories, startup validation, company/product search and provenance. |
| 39 | Unit tests for cart, Decimal totals, required customer data, states, revisions, IDs, confirmation reset. |
| 40 | Unit/integration tests for Redis session TTL and concurrent atomic message claims. |
| 41 | API integration tests for health/readiness, products, dev gating, dev chat, and quotation routes. |
| 42 | Agent/tool tests for tool choice, validation, iteration limits, and backend authority. |
| 43 | Fixed conversation acceptance scenarios for company, discovery, price, quote, modification, unsupported facts, confirmation. |
| 44 | PDF render/inspection tests proving readability, required fields, and totals equal backend totals. |
| 45 | Mocked WhatsApp integration tests for verification, parsing, text/media/document delivery, duplicates, and failures. |
| 46 | Container-only development end-to-end flow from greeting through generated PDF without WhatsApp. |
| 47 | Authorized real Meta sandbox end-to-end WhatsApp flow and PDF receipt; no real commercial-data inference. |
| 48 | Reliability/security/logging hardening: timeouts, safe retries, masking, paths, limits, secret scan, production route audit. |
| 49 | Scope, exclusion, factuality, demo-data provenance, and architecture compliance audit. |
| 50 | Run and record the complete Technical Plan §68 30-criterion acceptance gate in Docker. |
| 51 | Final operator/developer documentation, container commands, configuration, tunnel/setup notes, evidence index, release handoff. |

## 10. Completion rule

Task 01 is documentary only. The MVP is not complete until Tasks 02–51 are completed and Task 50 records evidence for every Technical Plan §68 criterion. Missing real business information remains an explicit open data dependency, never a license to invent it.
