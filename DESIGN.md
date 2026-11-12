# LastFreeDay — Design document (SRS) v2

**Product:** LastFreeDay  
**Assignment:** Brain3.ai — AI Automation Assignment (Full Stack AI Web Developer)  
**Author:** Salhe Seid  
**Date:** 12 November 2026  
**Status:** approved by Kidus 12 Nov 2026 — implementing the 1-day slice  
**For review:** kidus@brain3.ai

This replaces the 6 November design. Same industry and role. The product is no longer “paste text → calendar → one email”. It is a **demurrage control desk**: parse messy documents, own container-level deadlines, price the exposure, run a chase policy, and keep a human in the loop when the model is unsure.

---

## 1. Why the first design was too small

v1 proved the idea and stopped there: one shipment row, one last-free-day, one chase. That is a script with a UI. It does not show software engineering (domain model, jobs, idempotency, adapters, validation) or product (money at risk, policies, exceptions, review).

Real import ops fails in ways a single field cannot capture:

- One bill of lading has **several containers** with different free-day clocks.
- The **arrival notice LFD** and the **computed LFD** (discharge + free days + holidays) often disagree. Blind trust of Gemini is how people miss a day.
- The owner does not care about “days left”. They care about **birr / dollars at risk if nothing moves**.
- Chasing once is not a process. Forwarders need **T−72 / T−48 / T−24 / gate-day** with different copy, and a stop when the box is already cleared.
- Low-confidence extracts must **not** auto-email. That is how you spam the wrong forwarder.

v2 is built around those failures.

---

## 2. Role and industry (unchanged on purpose)

**Industry:** Import / wholesale distribution (Ethiopia and similar corridor markets).  
**Role:** Import operations assistant — sits between freight forwarder, warehouse, and owner. Job is not “type the BL into Excel”. Job is: **no container sits past free days**.

I am not switching to recruiting, EA, or social. The first review asked for more complexity and value, not a different toy domain. Demurrage is still the expensive, measurable failure.

**Buyer:** owner or ops lead at a small/mid importer. They will pay if the desk (a) shows money at risk, (b) does not email garbage, (c) leaves an audit trail when a box is missed.

---

## 3. Product: demurrage control desk

### 3.1 Personas

| Persona | Need |
|---|---|
| **Owner** | One board: ETB/USD exposure, which boxes will burn tomorrow, approve high-risk nudges |
| **Ops clerk** | Ingest notices, resolve field conflicts, confirm forwarder, mark gated / cleared |
| **System (agent)** | Extract, compute, schedule chases, never send on low confidence without a human |

No public forwarder portal in the 1-day build (designed, not shipped).

### 3.2 Jobs to be done

1. Ingest an arrival notice / BL excerpt (paste **or PDF text extract**).
2. Split into **containers**, not one blob per file.
3. Compute last free day **two ways** (extracted vs tariff calendar) and flag mismatch.
4. Price **projected demurrage** from a tariff table if the box sits.
5. Put the owner on a **policy**: who gets emailed at which T-minus, once per window, idempotent.
6. Block auto-send when confidence or conflicts are bad; queue a **review**.
7. Keep an append-only **audit log** (who parsed, who overrode, what was sent, job id).

### 3.3 Core screens (React)

1. **Exposure board** — containers grouped by risk band (`ok` / `watch` / `critical` / `burning`). Columns: container, BL, LFD, days left, projected cost, confidence, next action.
2. **Ingest** — paste or upload PDF; show field-level extract with confidence chips; side-by-side **extracted vs computed** LFD.
3. **Container detail** — state machine, tariff used, policy timeline, emails sent, overrides.
4. **Review queue** — items the agent refuses to send.
5. **Tariffs** — per shipping line + port: free days, weekday/weekend rate, currency.
6. **Activity** — audit stream, request ids.

---

## 4. Domain model

Not one `shipments` table.

```
Organization (single org in demo)
  User (role: owner | ops | viewer)
  Party (forwarder / consignee) — name, email, phone
  Tariff (line, port, free_days, weekday_rate, weekend_rate, currency, holiday_calendar)
  BillOfLading
    raw_source, source_type (paste|pdf), gemini_run_id
    Container[]  — the unit of risk
      number, type, discharge_date, free_days, lfd_extracted, lfd_computed, lfd_final
      status: ingested → watching → review → nudging → gated → cleared | missed
      projected_cost, confidence, version (optimistic lock)
  NotificationPolicy (windows: 72, 48, 24, 0 hours; template key; channel: email)
  OutboxMessage (email/calendar, idempotency_key, status: pending|sent|dry_run|failed)
  AuditEvent (actor, type, payload, request_id, at)
  Holiday (date, market) — Ethiopia public holidays that skip free-day count
```

**Last free day is computed, then reconciled, then stored.** Gemini does not own the clock.

**State machine (container)**

```
ingested → watching → nudging → gated → cleared
                ↘ review ↗
watching / nudging → missed   (LFD passed, still in port)
```

Illegal jumps are rejected in the service layer, not in the UI.

---

## 5. Extraction and reconciliation pipeline

This is the engineering core, not “call Gemini and save JSON”.

```
source text/PDF
  → normalize (whitespace, OCR-ish PDF text)
  → Gemini structured extract (schema-constrained)
  → validate (ISO container regex, dates, emails)
  → split containers
  → compute LFD from discharge + free_days + holiday calendar
  → reconcile: if |lfd_extracted − lfd_computed| > 0 days → conflict
  → confidence: min(model score, validator score, conflict penalty)
  → persist BL + containers + AuditEvent(parsed)
  → if confidence < 0.75 or conflict → status=review, do not auto-nudge
```

**Gemini output schema (strict)**  
vessel, voyage, bl_number, port, consignee, forwarder_name, forwarder_email, cargo, containers[{number, type, discharge_or_arrival, free_days, lfd_text}], warnings[].

Server rejects extra keys / missing container numbers. Prompt is versioned in code (`prompt_version`) so we can replay.

**PDF:** `pypdf` text layer only (free). Photo OCR is designed, not in the 1-day slice.

---

## 6. Cost engine (why an owner pays)

Tariff row: `line + port → free_days, weekday_rate, weekend_rate, currency`.

```
days_over = max(0, today − lfd_final)
projected = sum(rate(day) for each calendar day in (lfd, pickup_or_today])
```

Weekend/holiday rates differ. Demo tariffs: two lines × two ports, ETB + USD.

**What-if on the detail screen:** “If this box gates tomorrow vs in 3 days, extra cost = X.” Same function, different `as_of` date. No second LLM call.

Exposure board totals `projected_cost` for `watching|nudging|review|missed`. That number is the product.

---

## 7. Notification policy (not one email)

Default policy (editable):

| Window | Condition | Action |
|---|---|---|
| T−72h | watching, confidence ≥ 0.75, no conflict | email forwarder: “pickup window opening” |
| T−48h | still in port | email forwarder + copy owner |
| T−24h | still in port | urgent template; owner must ack if cost > threshold |
| T−0 / burning | LFD today or past | “missed” path; do not keep sending the T−48 body |

Rules:

- **Idempotency key:** `container_id + window + date`. Scan job cannot double-send.
- **Transactional outbox:** write `OutboxMessage` in the same SQLite transaction as the audit row; sender worker drains it. Crash between “mark nudged” and “SMTP” cannot lose or duplicate silently.
- **Dry-run** if `SENDGRID_API_KEY` missing — still writes outbox as `dry_run` so the video is honest.
- **Calendar:** one all-day event per **container** (not per BL), ICS fallback if Google OAuth is absent.

Scheduler: `POST /api/jobs/scan-deadlines` (Cloud Scheduler / local button). Same secret header as v1. Job is now: evaluate policy windows → enqueue outbox → send → audit.

---

## 8. Architecture

```
[React Vite]
    JSON + JWT
[FastAPI]
    ├─ ingest (parse, pdf text, reconcile)
    ├─ containers (state machine, overrides)
    ├─ tariffs + holidays + cost engine
    ├─ review queue
    ├─ jobs (scan, outbox drain)
    ├─ adapters
    │     GeminiClient      (parse + draft; prompt_version)
    │     CalendarPort      (Google or ICS)
    │     MailPort          (SendGrid or dry-run)
    └─ SQLite
          WAL, foreign keys, migrations on boot
[Cloud Scheduler] → scan-deadlines → outbox drain
```

**Engineering practices in the 1-day build (this is the SE demo):**

- Pydantic v2 request/response models; OpenAPI at `/docs`
- JWT + RBAC (`owner` can approve high-cost nudges; `ops` can ingest; `viewer` read-only)
- Optimistic lock `version` on container PATCH
- Request ID middleware → every audit row
- Idempotent job keys
- Adapter ports so Gemini/SendGrid/Calendar are not called from route handlers
- Tests: container number regex, LFD compute vs holidays, cost engine, policy window + idempotency, state-machine illegal transition
- Structured logs (`request_id`, `container_id`, `job_id`)

Still no paid cloud DB. SQLite is enough if the schema is real.

---

## 9. API (v2)

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/auth/login` | demo users: owner / ops |
| GET | `/api/health` | liveness + which adapters are live vs dry-run |
| POST | `/api/ingest/parse` | text → extract (not saved) |
| POST | `/api/ingest/pdf` | PDF upload → text → extract |
| POST | `/api/bls` | persist BL + containers after confirm |
| GET | `/api/board` | exposure board (aggregates + risk bands) |
| GET | `/api/containers` | list + filters |
| GET | `/api/containers/{id}` | detail, timeline, cost what-if |
| PATCH | `/api/containers/{id}` | override LFD / forwarder / status (version) |
| POST | `/api/containers/{id}/approve-nudge` | owner gate for low confidence or high cost |
| GET | `/api/review` | blocked items |
| GET/PUT | `/api/tariffs` | tariff CRUD |
| POST | `/api/containers/{id}/calendar` | ICS or Calendar insert |
| POST | `/api/jobs/scan-deadlines` | policy scan |
| POST | `/api/jobs/drain-outbox` | send pending mail |
| GET | `/api/audit` | filter by container / request_id |

---

## 10. What ships in the 1-day implementation vs designed

Kidus asked for a significantly more valuable product. The **design is the full desk**. The **video** will run a vertical slice of that desk, not a thinner product.

**In the build (must work on camera):**

- Ingest paste + PDF text
- Multi-container BL
- Extracted vs computed LFD + conflict → review queue
- Tariff + projected cost + exposure board
- Policy windows T−48 and T−24 (T−72 coded, may be idle in the sample)
- Outbox + idempotency + dry-run email
- ICS calendar per container
- JWT owner vs ops
- Audit log
- Cost what-if
- Tests listed above

**Designed, not in the 1-day binary:**

- Photo OCR / WhatsApp ingest
- Live shipping-line tracking (paid, flaky)
- Multi-tenant SaaS billing
- Forwarder login portal
- Automatic payment of demurrage
- Full Ethiopian holiday feed from a government API (we ship a static 2026 holiday table)

I will not fake those in the demo.

---

## 11. Risks

| Risk | Mitigation |
|---|---|
| Gemini JSON drift | Versioned prompt + Pydantic schema + server validators |
| Wrong LFD emailed | Conflict + confidence gate; owner approve if cost > threshold |
| Duplicate chases | Outbox idempotency key `container+window+date` |
| Calendar OAuth | ICS always works |
| SendGrid domain | Dry-run still writes outbox |
| 1-day time | Board + ingest + policy + cost are the slice; portal/OCR stay design-only |
| Holiday calendar wrong | Explicit table, visible on container detail |

---

## 12. Success bar

- **Would I pay?** Yes if the board’s projected cost is trustworthy enough to move a truck a day earlier.
- **Hire this over a junior ops person for deadline control?** For watch, price, and chase-with-review, yes. Not for customs filing.
- **Full-stack + product:** UI is an exposure desk; backend owns domain, jobs, adapters, tests.
- **Ambiguity:** we kept a painful local workflow and made the *system* heavy, instead of switching to a generic chatbot.

---

## 13. Ask

Please approve this v2 scope. After approval I implement the “in the build” slice, record the walkthrough (problem → domain → board demo → code: pipeline, state machine, outbox), and send the zip + video.
