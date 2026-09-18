# Requirements Traceability Matrix

## 1. How to read this matrix

This matrix maps the mandatory behavior of both source documents and every Technical Plan §68 completion criterion to the revised backlog in [implementation-contract.md](implementation-contract.md). `S-n` means Specification section *n*; `TP-n` means Technical Plan section *n*; `D-nn` and `SG-nn` refer to contract decisions and safeguards below.

Section rows consolidate closely related bullets but do not waive any bullet in that source section. “Primary tasks” implement the requirement; test/gate tasks verify it. Suggestions are included when they constrain the chosen MVP design. Future-extension ideas in Specification §40 are deliberately not implementation requirements.

## 2. Contract decision and safeguard traceability

| ID | Requirement | Tasks |
|---|---|---|
| D-01 | One Python 3.12 FastAPI modular monolith plus Redis. | 02, 03, 05, 49 |
| D-02 | Nested `Company.contact`; normalize the flat Specification example. | 07, 10, 38 |
| D-03 | Add increments; update replaces. | 18, 33, 39, 43 |
| D-04 | Nonblank name or company name plus transport phone. | 14, 17, 21, 33, 39 |
| D-05 | `QUOTE_REVIEW` on prepare; `AWAITING_CONFIRMATION` after preview delivery with false flag; later explicit confirmation sets true; generation requires both. | 20–22, 28–30, 33, 39, 43 |
| D-06 | `gdf:processed-message:{message_id}`, seven-day TTL. | 16, 30, 40, 45 |
| D-07 | Technical Plan §52 development-chat fields. | 36, 41, 46 |
| D-08 | UTC issue/date/validity behavior. | 09, 17, 19, 22, 39, 44 |
| D-09 | Decimal, `ROUND_HALF_UP`, two places. | 08, 09, 17, 19, 39, 44 |
| D-10 | Optional `contact_person` and `notes`. | 14, 17, 21, 24, 39 |
| D-11 | No exact equality between product and company category lists. | 07, 08, 10, 38 |
| D-12 | Development/internal routes unavailable in production. | 05, 23, 36, 41, 48 |
| D-13 | Demo data never represented as verified commercial data. | 07, 08, 24, 38, 43, 47, 49 |
| SG-01 | Preview revision/digest tracking and confirmation binding. | 14, 20–22, 33, 39, 43 |
| SG-02 | Atomic message claim before side effects. | 16, 30, 40, 45 |
| SG-03 | Transition only after preview delivery. | 20, 28, 30, 39, 43 |
| SG-04 | Test-visible demo-data provenance. | 07, 08, 24, 38, 49 |
| SG-05 | Startup/test enforcement of production route gating. | 05, 23, 36, 41, 48 |

## 3. MVP System Specification traceability

| Source | Mandatory requirement summary | Primary tasks | Verification/gate |
|---|---|---|---|
| S-1 | WhatsApp AI sales agent introduces company; supports categories, browsing/search/details/questions/recommendations/pricing/quantities; builds multi-product quote; collects customer data; previews, explicitly confirms, generates and sends PDF; uses static data; unavailable facts are disclosed. | 07–13, 17–35 | 38–50 |
| S-2 | AI handles understanding/conversation; structured backend and deterministic Python own facts/actions. No invented facts/claims/prices/calculations/discounts/IDs/products or unauthorized quote creation. | 10–13, 18–22, 31–35 | 38–43, 49–50 |
| S-3 | Conversational, not form-based, experience culminating in confirmed PDF; example values are illustrative, not verified data. | 31, 36, 43, 46–47 | 43, 49–50 |
| S-4 Included | Preserve all listed included capabilities: WhatsApp text/PDF, AI conversation, company/product discovery, static pricing, multi-product quote, customer/confirmation/PDF, temporary Redis memory, Docker, dev API, tests, structured logs. | 02–48 | 49–50 |
| S-4 Excluded | Preserve human-handoff, CRM/ERP/inventory/payment/accounts/admin/product UI/PostgreSQL/vector DB/RAG/framework/multi-agent/local-LLM/Kubernetes/voice/image/dynamic-discount/order-fulfillment exclusions. | 31, 35, 49 | 49–50 |
| S-5 | Python 3.12+, FastAPI/Pydantic/settings/Uvicorn; configurable OpenAI Responses API/function calling; direct Meta+HTTPX; Redis only for temporary state; JSON business data; Jinja2/WeasyPrint; pytest; Ruff/mypy; Docker/Compose; optional tunnel. | 02–05, 07–10, 15–16, 24, 29, 31–35, 38–47 | 46, 48–51 |
| S-6 | Maintain the specified end-to-end architecture and quotation/PDF delivery flow. | 05, 10–35 | 41–47, 50 |
| S-7 | One modular monolith; separation among API, agent, services, repositories, schemas, infrastructure, PDF, config, logging; no microservices. | 02–06, 49 | 49–50 |
| S-8 | Store and validate company JSON fields; unknown real contact information null/omitted; never fictional. | 07, 10–11 | 38, 43, 49–50 |
| S-9 | Structured product schema; decimal-compatible string price converted to Decimal; no float money. | 08, 10, 12, 17, 19 | 38–39, 44, 50 |
| S-10 | Approx. 8–12 suggested demo products acceptable; all demo values clearly development-only until real data supplied. | 08, 13 | 38, 43, 49 |
| S-11 | No pathogen, certification, authority, surface-safety, dilution, or contact-time claim unless explicitly stored; unavailable response. | 08, 10, 12–13, 31–35 | 38, 42–43, 49–50 |
| S-12 | Per-sender Redis session with specified state/customer/cart/confirmation/history/timestamps; configurable expiry, initially 24h. | 14–15 | 40–41, 46, 50 |
| S-13 | Implement listed state machine and backend protection of business actions. | 14, 20–22, 33 | 39, 42–43, 50 |
| S-14 | Cart stores product IDs/quantities; current repository prices used for preview/final. | 17–19, 21–22 | 39, 43–44, 50 |
| S-15 | Python-only calculation of quantity/unit/line/subtotal/discount/tax/total; LLM receives calculated values; zero default discount; configured tax. | 09, 17, 19 | 39, 42–44, 50 |
| S-16 | Quotation rules JSON controls currency, prefix, validity, discount, tax, terms; prompt is not authority. | 09–10, 19, 22 | 38–39, 44, 50 |
| S-17 | Backend creates official collision-safe quotation number; model never invents it. | 22, 33 | 39, 42–44, 50 |
| S-18 | Require customer/company identity and WhatsApp phone; support optional contact person/email/address/notes; request missing identity. | 14, 17, 21, 31, 33 | 39, 42–44, 50 |
| S-19 | Present full backend preview before explicit confirmation; ambiguous intent is not confirmation; backend gates generation. | 20–22, 31, 33–35 | 39, 42–43, 50 |
| S-20 | Provide narrow tools for company, categories, products/search/details, cart CRUD/summary, customer, prepare/confirm/create. | 32–34 | 42–43, 50 |
| S-21 | Product/price answers originate in product-details tool and stored data. | 12, 32, 34–35 | 42–43, 50 |
| S-22 | Deterministic case-insensitive search over specified fields; fuzzy matching optional; no vector DB. | 13, 32 | 38, 42–43, 49–50 |
| S-23 | Model receives policy, recent context, state, tools and behaves professionally/helpfully with all factuality/confirmation constraints; cannot bypass backend. | 31–35 | 42–43, 49–50 |
| S-24 | System/developer prompt encodes role, tool use, missing info, quote prerequisites, and no human transfer/escalation. | 31 | 42–43, 49–50 |
| S-25 | Direct Meta integration; inbound text; outbound text/PDF; safely explain unsupported inbound types. | 26–30 | 45, 47, 50 |
| S-26 | GET/POST webhook performs the specified validation, extraction, deduplication, session, agent, tool, persistence, response, and PDF workflow. | 26–30, 35 | 45, 47–50 |
| S-27 | Deduplicate message IDs in Redis with expiring key; duplicates are ignored. | 16, 30 | 40, 45, 50 |
| S-28 | Dev chat uses same agent/business services and synthetic session without WhatsApp. | 36 | 41, 46, 50 |
| S-29 | Provide health, WhatsApp, dev chat, product, and internal quotation routes; internal routes do not replace conversation. | 05, 23, 27–28, 36 | 41, 45–46, 48–50 |
| S-30 | Jinja2/HTML/CSS/WeasyPrint PDF contains factory/logo-if-supplied/title/ID/dates/customer/available contacts/items/descriptions/quantities/prices/totals/currency/terms. | 24–25 | 44, 47, 50 |
| S-31 | Temporary `storage/quotes` volume; storage abstraction replaceable without quotation-logic rewrite. | 03, 25 | 44, 46–47, 50 |
| S-32 | Safe responses for missing facts, unsupported products, technical failure; never offer human handoff. | 06, 12–13, 31, 35 | 42–43, 45, 49–50 |
| S-33 | Structured logs for correlations, tools, failures, transitions, quote IDs; exclude keys/tokens/auth headers. | 06, 28–30, 34–35 | 41, 45, 48, 50 |
| S-34 | Environment secrets, verification, Pydantic validation, length limits/timeouts/dedup, safe paths, no customer code, read-only product data, backend calculation, no model filesystem/arbitrary URL access, allowlisted tools. | 04, 06, 10, 16, 19, 25, 27, 29, 34–35 | 38–45, 48–50 |
| S-35 | Exactly API and Redis Compose services; API holds all application responsibilities. | 03, 05, 49 | 46, 49–50 |
| S-36 | Development `.env`, listed settings/path/TTL/timeouts, commit example but never `.env`. | 04 | 41, 48–51 |
| S-37 | Maintain the proposed responsibility-oriented repository structure or a documented equivalent. | 02 | 49–51 |
| S-38 | Unit, integration, and realistic conversation tests cover the listed operations and emphasize behavior/tool use over exact wording. | 38–45 | 46, 50 |
| S-39 | Customer completes all 15 success steps; values always match backend product data/Python. | 07–47 | 43–47, 50 |
| S-40 | Future CRM/ERP/inventory/multichannel/admin/RAG etc. are extension points, not MVP work. | 49 | 49–50 |

## 4. Technical Implementation Plan traceability

| Source | Mandatory requirement summary | Primary tasks | Verification/gate |
|---|---|---|---|
| TP-1–2 | Dockerized FastAPI sales app; API+Redis responsibilities; no relational DB. | 02–05, 49 | 46, 49–50 |
| TP-3–6 | Lock listed runtime/dev dependencies with uv; initialize project/config/env; model configurable without code change. | 02–04 | 41, 46, 48, 51 |
| TP-7 | Nested company/contact Pydantic models. | 07 | 38, 50 |
| TP-8 | Complete product/packaging/price/technical schema; Decimal price; keywords/default active. | 08 | 38–39, 50 |
| TP-9 | Customer/cart/line/totals/preview/generated schemas and positive quantity. | 17 | 39, 44, 50 |
| TP-10 | Complete conversation enum/message/session schema and fields. | 14 | 39–41, 50 |
| TP-11 | Async Redis get/save/delete/reset, normalized key, JSON, configurable refreshed TTL. | 15 | 40, 50 |
| TP-12 | Redis `SET NX`, mandated key, 604800 TTL, duplicate HTTP 200/no rerun. | 16, 30 | 40, 45, 50 |
| TP-13–14 | Load/cache read-only company/products; validate; build indexes and listed lookup/search methods; no writes. | 10 | 38, 50 |
| TP-15 | Normalize and score deterministic search across stored fields, top five recommendation, no embeddings/vector DB. | 13 | 38, 42–43, 49–50 |
| TP-16–17 | Company safe output and product list/search/detail/recommend services; recommendations repository-bound. | 11–13 | 38, 42–43, 50 |
| TP-18–19 | Quotation service owns cart/customer/preview/confirmation/ID/final model; validates active product/positive quantity; cart stores only ID/quantity/current price. | 18, 20–22 | 39, 42–44, 50 |
| TP-20 | Decimal `ROUND_HALF_UP` money helper and specified calculation order with two-place results. | 19 | 39, 44, 50 |
| TP-21 | Create fails for empty cart, missing identity, wrong state, or false confirmation flag; AI cannot override. | 21–22, 33 | 39, 42–43, 50 |
| TP-22 | Backend prefix+UTC date+random quotation ID, collision retry before PDF. | 22, 25 | 39, 44, 50 |
| TP-23–24 | Prepare/confirm/create transitions; every quote-relevant modification resets confirmation/state and requires fresh preview. | 20–22, 33 | 39, 42–43, 50 |
| TP-25–26 | Safe-path Jinja2-to-WeasyPrint PDF, output verification, print CSS, required sections, no external initial fonts. | 24–25 | 44, 46–47, 50 |
| TP-27–31 | WhatsApp schemas/client, GET verification (challenge/403), POST pipeline, unsupported media safe reply, synchronous MVP/no Celery initially. | 26–30 | 45, 47–50 |
| TP-32 | `AsyncOpenAI` with configured key/timeout; `AgentResult` returns response/session/optional generated quote. | 35 | 42–43, 46, 50 |
| TP-33–34 | Bounded configurable history; prompt contains identity/purpose/factuality/safety/quote/behavior/missing-info/current session; products retrieved by tools. | 31 | 42–43, 49–50 |
| TP-35–39 | Exact company/category/list/search/detail tool contracts; reasonable query; only stored/repository products/values; minimal safe summaries. | 32 | 42–43, 50 |
| TP-40–44 | Cart CRUD/summary/customer tools validate, reset confirmation and transition correctly; phone comes from transport. | 18, 21, 33 | 39, 42–43, 50 |
| TP-45–47 | Prepare validates and previews; confirm only after explicit current-message confirmation; create rechecks authority, creates PDF/ID, hides filesystem path from model. | 20–22, 24–25, 33 | 39, 42–44, 50 |
| TP-48 | Static allowlist maps names to handlers; no dynamic import/execution; unknown tool error. | 34 | 42, 48, 50 |
| TP-49–51 | Responses API tool loop validates calls/results, max 8 iterations, persists messages/session; catches named failures with safe customer response and internal logs. | 34–35 | 42–43, 48, 50 |
| TP-52 | Dev chat exact request/response fields, synthetic sender, same agent. | 36 | 41, 46, 50 |
| TP-53–54 | Safe product list/detail routes; internal quote preview/generate routes reuse service and no duplicate logic. | 12, 23 | 41, 48, 50 |
| TP-55–56 | Named FastAPI app/version/routes; lifespan validates data/Redis/directory and fails on invalid facts; liveness avoids OpenAI; readiness may check Redis. | 05, 37 | 41, 46, 50 |
| TP-57–59 | Python 3.12 slim image with WeasyPrint dependencies, cached dependency layers, storage, non-root where practical, exec CMD; Compose volumes/health/restart; container startup/docs/tunnel workflow. | 02–05, 24–25 | 44, 46, 48, 50–51 |
| TP-60 | Structured request/message/session/quote/tool logs, masked phone, no full sensitive production content. | 06, 28–35 | 41, 45, 48, 50 |
| TP-61 | Named business/infrastructure exceptions mapped safely by routes and conversation. | 06, 12, 18–22, 29, 34–35 | 39, 41–45, 50 |
| TP-62 | Startup validates unique IDs/SKUs, nonnegative valid Decimal prices, active descriptions, allowed/matching currency, sensible categories, no duplicates; critical errors fail startup. Category-label equality is not required. | 10, 37 | 38, 41, 46, 50 |
| TP-63 | Unit tests cover repository search/inactive, quote cases/modification/Decimal/errors/reset, session serialization/TTL. | 38–40 | 50 |
| TP-64 | Integration tests cover health/products/dev chat/webhook/PDF; mock OpenAI/Meta; no paid calls in standard CI. | 41, 44–45 | 50 |
| TP-65 | Agent tests cover named tool triggers and backend behavior; avoid exact prose assertions unless necessary. | 42 | 50 |
| TP-66 | Implement all scenarios A–I: company, discovery, price, quote, multiproduct, modification, unknown certification, reject unconfirmed, confirm/generate. | 43 | 46–47, 50 |
| TP-67 | Preserve outcomes of phases 1–10; revised Tasks 02–50 change ordering/granularity only. | 02–50 | 50 |
| TP-68 | All 30 Definition of Done criteria pass; individually mapped in §5 below. | 50 | 50 |
| TP-69 | Original 28-step order is superseded only by the revised 51-task ordering; deterministic system still precedes AI/WhatsApp integration. | 01–51 | 49–51 |
| TP-70 | Anything affecting money, truth, safety, customer records, quote state, or official action lives in Python; AI handles understanding/communication. | 10–22, 31–35 | 38–50 |

## 5. Technical Plan §68 Definition of Done: criterion-by-criterion

| §68 | Completion criterion | Implementation tasks | Evidence tasks |
|---:|---|---|---|
| 1 | `docker compose up --build` starts the entire application. | 02–05, 37 | 46, 50 |
| 2 | Redis health check succeeds. | 03, 37 | 40–41, 46, 50 |
| 3 | FastAPI `/docs` works. | 05 | 41, 46, 50 |
| 4 | Static company data loads successfully. | 07, 10, 37 | 38, 46, 50 |
| 5 | Product catalog validates on startup. | 08, 10, 37 | 38, 41, 46, 50 |
| 6 | Product search works. | 13 | 38, 41–43, 50 |
| 7 | AI can describe Global Detergent Factory. | 11, 31–35 | 42–43, 46, 50 |
| 8 | AI can list products. | 12, 31–35 | 42–43, 46, 50 |
| 9 | AI answers product-detail questions using tools. | 12, 31–35 | 42–43, 46, 50 |
| 10 | AI recommends only catalog products. | 13, 31–35 | 42–43, 49–50 |
| 11 | AI retrieves correct prices. | 08, 10, 12, 32, 35 | 38, 42–43, 50 |
| 12 | Customer can add quotation items. | 18, 33, 35 | 39, 42–43, 46, 50 |
| 13 | Customer can add multiple products. | 18, 33, 35 | 39, 43, 46, 50 |
| 14 | Customer can modify quantities. | 18, 33, 35 | 39, 43, 46, 50 |
| 15 | Customer can remove items. | 18, 33, 35 | 39, 43, 46, 50 |
| 16 | Customer information can be collected. | 14, 17, 21, 33, 35 | 39, 42–43, 46, 50 |
| 17 | Quote preview uses backend calculations. | 19–21, 33 | 39, 42–44, 46, 50 |
| 18 | Quotation cannot be generated without confirmation. | 20–22, 33 | 39, 42–43, 50 |
| 19 | Modifying quote resets confirmation. | 18, 20, 33 | 39, 43, 50 |
| 20 | Successful confirmation creates a unique quotation ID. | 20, 22, 33 | 39, 43–44, 50 |
| 21 | PDF is generated. | 24–25 | 44, 46–47, 50 |
| 22 | PDF totals match backend totals. | 19, 22, 24 | 39, 44, 50 |
| 23 | PDF can be sent through WhatsApp. | 25, 29–30 | 45, 47, 50 |
| 24 | Redis maintains conversation state. | 14–15, 30 | 40–41, 45–47, 50 |
| 25 | Duplicate webhook events do not duplicate actions. | 16, 30 | 40, 45, 47, 50 |
| 26 | Unknown product facts are not invented. | 08, 10, 12, 31–35 | 38, 42–43, 49–50 |
| 27 | Unknown certifications are not invented. | 08, 10, 12, 31–35 | 38, 42–43, 49–50 |
| 28 | Unsupported incoming media is handled safely. | 26, 28–30 | 45, 47, 50 |
| 29 | Secrets are not committed. | 02, 04 | 48, 50 |
| 30 | Automated tests cover critical business logic. | 38–45 | 46, 50 |

## 6. Execution-environment and acceptance constraints

| Requirement | Tasks | Evidence |
|---|---|---|
| Docker from the beginning; Docker Desktop/Compose and editor are the only host prerequisites. | 02–03, 51 | 46, 50 |
| Python, uv, formatting, type checking, tests, FastAPI, Redis, and WeasyPrint run inside containers. | 02–03, 24, 38–46, 51 | 46, 48, 50 |
| No host Python/uv/Redis/PDF libraries required. | 03, 51 | 46, 50 |
| Bootstrap operation is not completion evidence. | 01, 03, 50 | Task 50 evidence index |
| Startup validation is progressively wired and complete once dependencies exist. | 05, 10, 37 | 38, 41, 46, 50 |
| Both sources remain traceable to revised backlog. | 01 | This matrix; Task 49 audit |
| Conflicting examples have one interpretation. | 01 | Contract §3; Tasks 38–45 |
| Missing real information is recorded, not fabricated. | 07–13, 31–35, 49 | 38, 42–43, 49–50 |
| Only order and execution environment change; scope/outcomes are preserved. | 01–51 | 49–50 |

## 7. Exclusion traceability

Task 49 must explicitly audit that the delivered application contains no human handoff, CRM, ERP, inventory/warehouse synchronization, payments/checkout, customer accounts, admin/product-management UI, PostgreSQL or other persistent database beyond Redis, vector database, RAG, LangChain, CrewAI, LlamaIndex, agent framework, multi-agent application architecture, local LLM, Kubernetes, voice processing, image recognition, dynamic discount approval, or order fulfillment. Tasks 31 and 43 verify the agent does not offer human transfer; Tasks 02–03 and 49 verify architecture/dependencies; Task 50 records the final result.
