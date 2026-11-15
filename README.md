# Clinic Booking API

FastAPI, SQLite for local development, PostgreSQL for deployment, and a single-page appointment booking UI. The API implements the assessment routes and response shapes:

- `POST /reset` replaces the slot schedule and clears bookings.
- `GET /slots?provider=Dr%20Ade` returns all slots in start-time order, including availability.
- `POST /bookings` accepts `{ "slot_id", "name", "email" }` and returns the created booking.
- `GET /bookings?email=ada@example.com&limit=10&cursor=...` provides oldest-first cursor pagination.
- `GET /bookings/{id}` looks up a booking.
- `DELETE /bookings/{id}` cancels it and frees its slot.

All assessment API routes require `X-API-Key`. The patient page uses the same-origin `/ui-api/*` bridge, which keeps the key server-side. Booking attempts are limited to five per email per rolling minute. `POST /bookings` accepts an optional `Idempotency-Key` header. SQLite transactions and a partial unique index prevent two confirmed bookings for one slot.

## Run locally

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:API_KEY = "replace-with-a-long-random-secret"
uvicorn app:app --reload
```

Open `http://127.0.0.1:8000`. API documentation is available at `/docs`.

## Deploy with Docker Compose

Copy `.env.example` to `.env`, set a long random `API_KEY`, and run `docker compose up --build -d`. The named `booking-data` volume persists SQLite data. For a public production deployment, serve the page through a server-side proxy so the clinic key is not exposed in each patient's browser.

Set `DATABASE_PATH`, `DATABASE_URL`, `API_KEY`, `CORS_ORIGINS`, and `PORT` through the environment. The current API key default is for local development only.

## Deploy to Vercel

Vercel runs this FastAPI app as a Python Function. Connect a PostgreSQL provider through Vercel Marketplace (for example Neon), then set `DATABASE_URL` and a long random `API_KEY` in the project's Production environment variables. From this folder, authenticate and deploy with:

```powershell
npm exec --yes --package=vercel@65.16.0 -- vercel login
npm exec --yes --package=vercel@65.16.0 -- vercel --prod
```

The function refuses to start on Vercel without `DATABASE_URL` and `API_KEY`, so it will not use ephemeral storage or a predictable development key.
