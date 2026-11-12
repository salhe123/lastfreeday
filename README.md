# LastFreeDay

Demurrage control desk for import ops. Kidus approved v2 on 12 Nov 2026.

Parse messy arrival notices, **container-level** last free day (Gemini extract vs computed calendar), price exposure from tariffs, chase T−72/48/24/0, **never auto-email** on conflict or low confidence.

Design: `DESIGN.md`.

## Stack

- React (Vite)
- FastAPI + SQLite
- Gemini (or offline parse if no key — still goes to review)
- SendGrid or dry-run outbox
- ICS calendar per container

## Run

If you still have the v1 database:

```bash
rm -f backend/lastday.sqlite
```

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp -n .env.example .env
# optional: GEMINI_API_KEY
uvicorn app:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Open http://127.0.0.1:5173

Demo logins: `ops` / `ops` (ingest) · `owner` / `owner` (approve nudges) · `viewer` / `viewer`

Health: http://127.0.0.1:8000/api/health  
OpenAPI: http://127.0.0.1:8000/docs

## Tests

```bash
cd backend
source .venv/bin/activate
PYTHONPATH=. pytest tests -q
```

## Demo script (~4 min)

1. Login as **ops**. Load sample. Parse. Show two containers: extracted vs computed LFD. TCLU row conflicts → will not auto-mail.
2. Confirm save. Board shows risk bands + projected cost. Review tab has the conflict box.
3. Login as **owner**. Approve nudge on the review item. Scan + drain outbox (dry-run if no SendGrid).
4. Open a container: what-if extra cost if it waits 3 days. Download ICS.
5. Activity tab: request ids, parsed, outbox.

Sample: `backend/sample_notice.txt`.

Jobs (same secret as v1): `X-Scan-Secret: lastday-dev`

- `POST /api/jobs/scan-deadlines`
- `POST /api/jobs/drain-outbox`
