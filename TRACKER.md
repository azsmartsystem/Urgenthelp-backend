# 🚀 UrgentHelp BACKEND — PROJECT TRACKER

**Developer:** Backend Dev, Patrick Okafor
**Client:** UrgentHelp
**Contract Start:** October 1, 2026
**Contract End:** December 31, 2026 (13 weeks)
**Daily Commitment:** ~4 hours/day (AI-assisted)

---

## MILESTONE OVERVIEW

### M1: Foundation & Core API

**Status:** 🔄 IN PROGRESS
**Deadline:** November 1, 2026 (4 weeks)
**Deliverable:** Admin can log in, see registered users, manage categories. Auth fully working.

### M2: Core Booking Flow + Payments

**Status:** ⏳ NOT STARTED
**Deadline:** December 1, 2026 (4 weeks)
**Deliverable:** Customer can create booking, match with helper, chat, and pay end-to-end.

### M3: Polish, Identity Verification & Deployment

**Status:** ⏳ NOT STARTED
**Deadline:** December 31, 2026 (4 weeks)
**Deliverable:** UAT-ready platform deployed to production with monitoring.

---

## MONTH 1: FOUNDATION (Oct 1 – Nov 1)

> **Goal:** Client can log in to admin panel, see registered users, and manage service categories. All auth flows working.

---

### Week 1: Oct 1–7 — Setup & Auth Foundation

**Focus:** Project bootstrap + Registration + OTP Login

#### 🎯 Tasks 001

- [x] **Project Bootstrap** *(Done — Sep 30)*
  - [x] uv + FastAPI project scaffold
  - [x] Modular monolith structure (`app/modules/`, `app/engines/`, `app/core/`)
  - [x] pyproject.toml with all dependencies
  - [x] `.env.example` with all required variables
  - [x] Gunicorn production config
  - [x] pre-commit hooks (ruff + mypy + conventional commits)
  - [x] Git repo initialised + remote added
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [x] **Database Setup** *(Done — Oct 4)*
  - [x] PostgreSQL running locally (Docker or native)
  - [x] Alembic initialised (`alembic.ini`, `script.py.mako`, `env.py`)
  - [x] First migration: `users` table
  - [x] `uv run alembic upgrade head` — confirm table created
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [x] **Auth — Registration Endpoint** `POST /api/v1/auth/register` *(Done — Oct 7)*
  - [x] Implement `AuthService.register()` — hash password (bcrypt), persist `User`
  - [x] Return access + refresh JWT pair
  - [x] Handle `PhoneAlreadyRegisteredError` and `EmailAlreadyRegisteredError`
  - [x] Write `test_service.py` tests
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [x] **Auth — Password Login** `POST /api/v1/auth/login` *(Done — Oct 7)*
  - [x] Implement `AuthService.login()` — verify bcrypt password hash
  - [x] Return access + refresh JWT pair
  - [x] Handle `InvalidCredentialsError` and `InactiveUserError`
  - [x] Write tests
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [x] **Auth — OTP Send/Verify** `POST /api/v1/auth/otp/send` + `/verify` *(Done — Oct 7)*
  - [x] Generate 6-digit OTP, store in Redis with 10min TTL
  - [x] Verify OTP, return JWT pair on success, invalidate OTP in Redis
  - [x] Handle `OTPExpiredError` and `InvalidOTPError`
  - [x] Write tests
  - **Est. Time:** 5h | **Blocker?** ❌ None

- [x] **Auth — Token Refresh** `POST /api/v1/auth/refresh` *(Done — Oct 7)*
  - [x] Decode refresh token, issue new access + refresh token pair
  - [x] Invalidate old refresh token (Redis blacklist)
  - [x] Write tests
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [x] **Nigerian Phone Normalization & DB Constraint** *(Done — Oct 8)*
  - [x] Auto-convert `08...`, `234...`, `+234...`, formatted strings to `234XXXXXXXXXX`
  - [x] Pydantic `NigerianPhone` type validator across all auth request schemas
  - [x] PostgreSQL CHECK constraint `ck_users_phone_format` on `users.phone`
  - [x] Alembic migration `26db6f25013c`
  - [x] Unit & integration tests (70 passed, 92.88% coverage)
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [x] **Email Channel — Schema** *(Done — Oct 4)*
  - [x] `users.email` (nullable, unique, not a login identifier)
  - [x] `users.email_verified_at` (only verified emails may receive reset links)
  - [x] `notification_preferences` table (user, channel, event type, enabled)
  - [x] Email settings in `Settings` and `.env.example`
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **Email Channel — Verification** `POST /api/v1/users/me/email` + `/email/verify`
  - [ ] Accept optional email at registration (`EmailStr`)
  - [ ] Send single-use verification link (hashed token in Redis, 24h TTL)
  - [ ] Set `email_verified_at` on success; reset it if the email changes
  - [ ] Integrate email provider (Resend / SendGrid / SES); log-only in dev
  - [ ] Write tests
  - **Est. Time:** 5h | **Blocker?** ⚠️ Email provider account + sender domain

- [ ] **Account Recovery (tiered)** `POST /api/v1/auth/password/forgot` + `/reset`
  - [ ] Verified email on file: send reset link (default)
  - [ ] No verified email: SMS OTP to registered phone (fallback)
  - [ ] Neither available: manual admin recovery via ID verification
  - [ ] Identical response whether or not the account exists (no enumeration)
  - [ ] Single-use random token, hash stored in Redis, 30 min TTL (not a JWT)
  - [ ] Revoke all refresh tokens after a successful reset
  - [ ] Rate limit per phone, email and IP
  - [ ] Write tests
  - **Est. Time:** 6h | **Blocker?** ⚠️ Depends on OTP + email verification tasks

- [ ] **Notification Preferences** `GET/PUT /api/v1/notifications/preferences`
  - [ ] List and update per-channel, per-event opt-ins
  - [ ] Transactional events (reset, security, receipts) are never opt-out
  - [ ] Write tests
  - **Est. Time:** 3h | **Blocker?** ❌ None

**Week 1 Goals:**

- [x] User can register with phone + password
- [x] User can request and verify OTP
- [x] JWT pair returned correctly
- [x] All auth tests passing (92% coverage, strict mypy clean)

---

### Week 2: Oct 8–14 — User & Helper Profiles

**Focus:** Profile management (single model, role-based views)

#### 🎯 Tasks 002

- [ ] **Migration: `helper_profiles` extension fields**
  - [ ] Skills (array), service radius, availability schedule, bio, certifications
  - [ ] Alembic migration
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **Users Module** — `GET/PUT /api/v1/users/me`
  - [ ] `UserService.get_me()` — fetch authenticated user profile
  - [ ] `UserService.update_profile()` — update name, photo, address
  - [ ] Profile photo upload to S3
  - [ ] Write tests
  - **Est. Time:** 4h | **Blocker?** ⚠️ AWS S3 credentials

- [ ] **Helpers Module** — `GET/PUT /api/v1/helpers/me`
  - [ ] `HelperService.update_helper_profile()` — skills, availability, radius
  - [ ] `HelperService.get_helpers_nearby()` — geospatial query (lat/lon + radius)
  - [ ] Write tests
  - **Est. Time:** 5h | **Blocker?** ❌ None

- [ ] **Service Categories Module** — `GET /api/v1/categories`
  - [ ] `CategoryService.list_categories()` — return all active categories
  - [ ] Migration: `service_categories` table
  - [ ] Seed initial 18 categories from PRD
  - [ ] Write tests
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **Identity Verification — Upload** `POST /api/v1/users/verify-identity`
  - [ ] Accept government ID image upload (jpg/png/pdf, max 5MB)
  - [ ] Store securely in S3 (`/identity-docs/<user-id>/`)
  - [ ] Set `is_id_verified = False`, flag for admin review
  - [ ] Write tests
  - **Est. Time:** 3h | **Blocker?** ❌ None

**Week 2 Goals:**

- [ ] Customer and Helper can update their profiles
- [ ] Helper can set skills, radius, and availability
- [ ] Categories API returns the 18 PRD categories
- [ ] ID document upload working

---

### Week 3: Oct 15–21 — Booking Lifecycle + Matching

**Focus:** Create a booking, match a helper, state machine

#### 🎯 Tasks 003

- [ ] **Migration: `bookings` table**
  - [ ] All fields: status, location, notes, prices, customer_id, helper_id, category_id
  - [ ] Alembic migration
  - **Est. Time:** 1h | **Blocker?** ❌ None

- [ ] **Bookings Module — Create** `POST /api/v1/bookings`
  - [ ] `BookingService.create_booking()` — validate category, calculate price via `engines.pricing`
  - [ ] Return recommended price range to customer
  - [ ] Write tests
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Bookings Module — Matching** `POST /api/v1/bookings/{id}/match`
  - [ ] `BookingService.match_helpers()` — query available helpers, run `engines.matching.rank_helpers()`
  - [ ] Send push notification to top-3 ranked helpers (FCM stub)
  - [ ] Transition: `requested → matched`
  - [ ] Write tests including `NoAvailableHelpersError`
  - **Est. Time:** 5h | **Blocker?** ❌ None

- [ ] **Bookings Module — State Transitions**
  - [ ] `POST /api/v1/bookings/{id}/accept` → `matched → accepted`
  - [ ] `POST /api/v1/bookings/{id}/start` → `accepted → en_route`
  - [ ] `POST /api/v1/bookings/{id}/complete` → `in_progress → completed`
  - [ ] `POST /api/v1/bookings/{id}/cancel` — cancel from valid states
  - [ ] Enforce `booking.can_transition_to()` on every update
  - [ ] Write state machine tests
  - **Est. Time:** 5h | **Blocker?** ❌ None

- [ ] **Bookings Module — List/Get**
  - [ ] `GET /api/v1/bookings` — customer's booking history (paginated)
  - [ ] `GET /api/v1/bookings/{id}` — booking detail
  - [ ] `GET /api/v1/helpers/me/bookings` — helper's job queue
  - **Est. Time:** 2h | **Blocker?** ❌ None

**Week 3 Goals:**

- [ ] Full booking lifecycle working end-to-end (create → match → accept → complete)
- [ ] State machine enforced — invalid transitions return typed errors
- [ ] AI Matching engine integrated and returning ranked helpers

---

### Week 4: Oct 22–31 — Admin Dashboard APIs

**Focus:** Admin can see and manage everything

#### 🎯 Tasks

- [ ] **Admin — User Management**
  - [ ] `GET /api/v1/admin/users` — list all users, filter by role/status (paginated)
  - [ ] `GET /api/v1/admin/users/{id}` — full user detail
  - [ ] `POST /api/v1/admin/users/{id}/suspend` — suspend account
  - [ ] `POST /api/v1/admin/users/{id}/activate` — reactivate
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Admin — Identity Verification Queue**
  - [ ] `GET /api/v1/admin/verifications` — pending ID reviews
  - [ ] `POST /api/v1/admin/verifications/{id}/approve`
  - [ ] `POST /api/v1/admin/verifications/{id}/reject`
  - [ ] Approved → sets `is_id_verified = True`, sends notification
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **Admin — Category Management**
  - [ ] `POST /api/v1/admin/categories` — create new category
  - [ ] `PUT /api/v1/admin/categories/{id}` — update (name, base price, active)
  - [ ] `DELETE /api/v1/admin/categories/{id}` — soft delete
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **Admin — Bookings Overview**
  - [ ] `GET /api/v1/admin/bookings` — all bookings, filter by status/date
  - [ ] `GET /api/v1/admin/bookings/stats` — count by status, this week vs last week
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **M1 Review Prep**
  - [ ] Run `uv run pytest --cov` — confirm ≥80% coverage
  - [ ] Run `uv run mypy app/` — confirm zero errors
  - [ ] Test all endpoints via `/docs` Swagger UI
  - [ ] Record short Loom demo for client
  - **Est. Time:** 4h | **Blocker?** ❌ None

**Week 4 Goals:**

- [ ] Admin can see all users, approve ID verifications, manage categories
- [ ] M1 demo video recorded
- [ ] Test coverage ≥80%, mypy clean

---

## MONTH 2: CORE FEATURES (Nov 3 – Dec 1)

> **Goal:** End-to-end booking flow working. Customer books → matches helper → chats → pays. Wallet functional.

---

### Week 5: Nov 3–9 — Payments + Wallet

**Focus:** Paystack integration, wallet balance, booking payment

#### 🎯 Tasks 004

- [ ] **Migration: `wallets` + `transactions` tables**
  - [ ] Wallet: user_id, balance, currency
  - [ ] Transaction: wallet_id, type, amount, reference, status
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **Paystack — Initiate Payment** `POST /api/v1/payments/initiate`
  - [ ] `PaymentService.initiate()` — call Paystack Initialize Transaction API
  - [ ] Return `authorization_url` to client (redirect or WebView)
  - [ ] Write `TypeGuard` for Paystack response shape
  - **Est. Time:** 4h | **Blocker?** ⚠️ Paystack test key needed

- [ ] **Paystack — Webhook Handler** `POST /api/v1/payments/webhook`
  - [ ] Verify `x-paystack-signature` HMAC — reject if invalid
  - [ ] Handle `charge.success` → credit user wallet
  - [ ] Handle `transfer.success` → confirm withdrawal
  - [ ] Write tests for signature verification
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Wallet Module**
  - [ ] `GET /api/v1/wallet` — balance + recent transactions
  - [ ] `POST /api/v1/wallet/withdraw` — request withdrawal (queued)
  - [ ] `WalletService.deduct()` / `credit()` — atomic balance ops
  - [ ] Handle `InsufficientWalletBalanceError`
  - [ ] Write tests
  - **Est. Time:** 5h | **Blocker?** ❌ None

- [ ] **Booking Payment Flow**
  - [ ] Deduct wallet on booking completion (`completed` transition)
  - [ ] Calculate and retain platform commission (10%)
  - [ ] Credit helper wallet after commission
  - **Est. Time:** 3h | **Blocker?** ❌ None

**Week 5 Goals:**

- [ ] Can top up wallet via Paystack
- [ ] Booking payment deducted from wallet on completion
- [ ] Commission split working correctly

---

### Week 6: Nov 10–16 — Real-time Messaging

**Focus:** In-app chat via WebSockets

#### Tasks 005

- [ ] **Migration: `conversations` + `messages` tables**
  - [ ] Conversation linked to booking_id (1-to-1 per booking)
  - [ ] Message: sender_id, content, type, read_at
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **WebSocket Connection Manager**
  - [ ] `ConnectionManager` class — tracks active WS connections per user
  - [ ] `POST /api/v1/messages/ws/{booking_id}` — WS endpoint
  - [ ] Authenticate via JWT query param (WS headers limited)
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Messaging Service**
  - [ ] `MessagingService.send_message()` — persist + broadcast
  - [ ] `MessagingService.get_history()` — paginated message history
  - [ ] Mark messages as read
  - [ ] Write tests (mock WS manager)
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **HTTP Fallback Endpoints** (for REST access)
  - [ ] `GET /api/v1/messages/{booking_id}` — message history
  - [ ] `POST /api/v1/messages/{booking_id}` — send (non-WS fallback)
  - **Est. Time:** 2h | **Blocker?** ❌ None

**Week 6 Goals:**

- [ ] Two users can exchange messages in real-time via WebSocket
- [ ] Message history persisted and retrievable
- [ ] Works with the `/docs` Swagger WS test

---

### Week 7: Nov 17–23 — Notifications + Reviews

**Focus:** Firebase push notifications + post-booking ratings

#### 🎯 Tasks 006

- [ ] **Firebase FCM Setup**
  - [ ] `NotificationService.send_push()` — wrap FCM Admin SDK
  - [ ] Store FCM token on user record (`PUT /api/v1/users/me/fcm-token`)
  - [ ] Send on: booking matched, helper accepted, booking completed, message received
  - **Est. Time:** 4h | **Blocker?** ⚠️ Firebase project + service account JSON

- [ ] **In-App Notification Store**
  - [ ] `GET /api/v1/notifications` — list unread notifications (paginated)
  - [ ] `POST /api/v1/notifications/{id}/read` — mark as read
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **Reviews Module**
  - [ ] `POST /api/v1/reviews` — submit rating (1–5) + text after booking `completed`
  - [ ] Prevent reviewing before completion; prevent double review
  - [ ] `GET /api/v1/helpers/{id}/reviews` — helper's public review list
  - [ ] Trigger `calculate_trust_score()` after each review submission
  - [ ] Write tests
  - **Est. Time:** 4h | **Blocker?** ❌ None

**Week 7 Goals:**

- [ ] Push notification arrives on helper's device when job matched
- [ ] Customer can rate and review after job completion
- [ ] Trust score updates automatically post-review

---

### Week 8: Nov 24 – Dec 1 — Integration, Tests & M2 Review

**Focus:** Wire everything together, polish, test

#### 🎯 Tasks 007

- [ ] **End-to-End Flow Test**
  - [ ] Register customer + helper
  - [ ] Customer creates booking → AI matches helper → helper accepts → completes → payment → review
  - [ ] Verify every state transition, notification, and wallet balance
  - **Est. Time:** 6h | **Blocker?** ❌ None

- [ ] **Fraud Engine Integration**
  - [ ] Run `run_fraud_check()` on registration and on booking creation
  - [ ] Auto-suspend on `critical` severity flags
  - [ ] Log all fraud events to admin dashboard
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **API Documentation Polish**
  - [ ] All endpoints have descriptions, example payloads, and error responses in docstrings
  - [ ] FastAPI auto-docs at `/docs` is comprehensive enough for frontend team
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **M2 Review Prep**
  - [ ] Full test run: `uv run pytest --cov` ≥ 80%
  - [ ] mypy strict clean: `uv run mypy app/`
  - [ ] Record end-to-end demo video
  - [ ] Update this tracker
  - **Est. Time:** 4h | **Blocker?** ❌ None

**Week 8 Goals:**

- [ ] Complete booking lifecycle working end-to-end, tested
- [ ] Demo video recorded for M2 submission

---

## MONTH 3: POLISH & LAUNCH (Dec 3 – Dec 31)

> **Goal:** UAT-ready, deployed, monitored, documented. Client can run independently.

---

### Week 9: Dec 3–9 — Location Tracking + Helper App APIs

**Focus:** GPS status updates, helper-side endpoints

#### 🎯 Tasks 008

- [ ] **Status-Based Location Tracking** *(MVP: no live GPS streaming)*
  - [ ] `POST /api/v1/bookings/{id}/location` — helper posts lat/lon at key status changes
  - [ ] Store on booking record: `helper_lat`, `helper_lon`, `last_location_update`
  - [ ] Customer polls `GET /api/v1/bookings/{id}` for location — no WebSocket needed for MVP
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **Helper Dashboard APIs**
  - [ ] `GET /api/v1/helpers/me/earnings` — earnings summary (today, this week, this month)
  - [ ] `GET /api/v1/helpers/me/stats` — jobs completed, avg rating, trust score
  - [ ] `PUT /api/v1/helpers/me/availability` — set online/offline toggle
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Dispute Flow**
  - [ ] `POST /api/v1/bookings/{id}/dispute` — raise dispute on completed/in-progress
  - [ ] Transition: `in_progress → disputed`
  - [ ] Admin can resolve: `disputed → completed` or `disputed → cancelled`
  - **Est. Time:** 3h | **Blocker?** ❌ None

**Week 9 Goals:**

- [ ] Helper can update their GPS location at status changes
- [ ] Helper earnings dashboard working
- [ ] Dispute flow implemented

---

### Week 10: Dec 10–16 — Security Hardening + Rate Limiting

**Focus:** Make it production-safe

#### 🎯 Tasks 010

- [ ] **Rate Limiting**
  - [ ] OTP endpoints: 3 requests per phone per 10 minutes (Redis counter)
  - [ ] Auth endpoints: 10 requests per IP per minute
  - [ ] Implement via FastAPI middleware using Redis
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Input Sanitisation Review**
  - [ ] Audit all `str` fields for max_length constraints
  - [ ] Confirm all file upload endpoints validate MIME type + file size
  - [ ] SQL injection: confirm all queries use ORM (no raw SQL)
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **Sentry Integration**
  - [ ] Add Sentry DSN to `.env`
  - [ ] Verify unhandled exceptions appear in Sentry dashboard
  - [ ] Set environment and release tags
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **Secrets Audit**
  - [ ] Confirm `.env` is in `.gitignore` (it is ✅)
  - [ ] Rotate all test keys before production deployment
  - [ ] Verify no secrets in git history
  - **Est. Time:** 2h | **Blocker?** ❌ None

**Week 10 Goals:**

- [ ] OTP brute-force protected
- [ ] Sentry capturing errors in staging
- [ ] Security audit complete

---

### Week 11: Dec 17–23 — Staging Deployment + Load Testing

**Focus:** Get it running on a real server

#### 🎯 Tasks 011

- [ ] **Choose & Configure Cloud Provider**
  - [ ] Recommended: Railway (fast) or AWS EC2 (more control)
  - [ ] Set up staging environment (separate from prod)
  - [ ] Configure env vars in server environment (never upload .env file)
  - **Est. Time:** 4h | **Blocker?** ⚠️ Server access / billing

- [ ] **Database on Cloud**
  - [ ] Provision managed PostgreSQL (Railway Postgres / AWS RDS / Supabase)
  - [ ] Run `uv run alembic upgrade head` on production DB
  - [ ] Verify connections pooling correctly
  - **Est. Time:** 3h | **Blocker?** ❌ None

- [ ] **CI/CD Pipeline**
  - [ ] GitHub Actions: run tests + mypy on every push to `main`
  - [ ] Auto-deploy to staging on push to `main`
  - [ ] Notify on failure
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Load Testing**
  - [ ] Use `locust` or `k6` to simulate 100 concurrent booking requests
  - [ ] Identify bottlenecks (DB connections, matching engine)
  - [ ] Tune Gunicorn worker count
  - **Est. Time:** 3h | **Blocker?** ❌ None

**Week 11 Goals:**

- [ ] App running on staging URL
- [ ] Client can access staging `/docs` for manual testing
- [ ] CI/CD running on GitHub

---

### Week 12–13: Dec 24–31 — UAT, Bug Fixes & Handoff

**Focus:** Fix everything, hand over cleanly

#### 🎯 Tasks 012

- [ ] **User Acceptance Testing (UAT)**
  - [ ] Share staging URL with client + frontend team
  - [ ] Collect and log all bugs found during UAT
  - [ ] Fix all P0 (critical) and P1 (high) bugs
  - **Est. Time:** 8h | **Blocker?** ⚠️ Frontend team availability

- [ ] **Final Test Run**
  - [ ] `uv run pytest --cov` — ≥80% coverage confirmed
  - [ ] `uv run mypy app/` — zero errors
  - [ ] `uv run ruff check app/` — zero issues
  - **Est. Time:** 2h | **Blocker?** ❌ None

- [ ] **Production Deployment**
  - [ ] Deploy to production server
  - [ ] Run migrations on production DB
  - [ ] Smoke test all critical endpoints
  - [ ] Disable `/docs` in production (already configured ✅)
  - **Est. Time:** 4h | **Blocker?** ❌ None

- [ ] **Handoff Package**
  - [ ] Updated README with setup, deploy, and maintenance instructions
  - [ ] `.env.example` fully documented ✅
  - [ ] Recorded walkthrough video of codebase architecture
  - [ ] Post-contract roadmap: what to build in Month 4–6
  - [ ] Transfer repo ownership or add client as collaborator
  - **Est. Time:** 5h | **Blocker?** ❌ None

**Week 12–13 Goals:**

- [ ] Production live and stable
- [ ] Client can operate and extend the platform independently
- [ ] Final invoice submitted

---

## SPRINT VELOCITY TRACKER

| Week | Planned Hours | Actual Hours | Tasks Done | Notes |
| ------ | ------------- | ------------- | ----------- | ------- |
| W1 (Bootstrap) | 8h | ~4h | ✅ Full scaffold | Done Sep 30 |
| W1 (Auth) | 14h | — | ⏳ | Starts Oct 1 |
| W2 | 18h | — | ⏳ | |
| W3 | 18h | — | ⏳ | |
| W4 | 17h | — | ⏳ | |
| W5 | 18h | — | ⏳ | |
| W6 | 12h | — | ⏳ | |
| W7 | 10h | — | ⏳ | |
| W8 | 16h | — | ⏳ | |
| W9 | 10h | — | ⏳ | |
| W10 | 11h | — | ⏳ | |
| W11 | 14h | — | ⏳ | |
| W12–13 | 19h | — | ⏳ | |
| **TOTAL** | **~185h** | — | — | Budget: ~260h |

> **Buffer:** ~75 hours available for scope creep, fixes, and client requests.

---

## RISK REGISTER

| Risk | Likelihood | Impact | Mitigation |
| ------ | ----------- | -------- | ----------- |
| SMS provider signup delayed | Medium | High | Register Termii/Twilio Week 1 Day 1 |
| Paystack integration issues | Low | High | Use sandbox aggressively; test webhooks with ngrok |
| Firebase setup delayed | Low | Medium | Use mock notification service until ready |
| Frontend team blocks on API | Medium | Medium | Deliver Swagger docs by end of W1 |
| Scope creep (client adds features) | High | High | Refer to Discovery Notes; log all change requests |
| DB connection pool exhaustion | Low | High | Tune pool size; add pgbouncer if needed |

---

## BLOCKERS LOG

| Date | Blocker | Owner | Resolved? |
|------|---------|-------|-----------|
| —--- | -—----- | —---- | —-------- |

---

## 🔑 CREDENTIALS & KEYS NEEDED FROM CLIENT

- [ ] Paystack Secret Key (live)
- [ ] Firebase Service Account JSON
- [ ] Google Maps API Key
- [ ] SMS Provider account (Termii / Twilio)
- [x] AWS S3 or Cloudflare R2 credentials
- [ ] Domain name + SSL (for production)
- [x] Cloud server access (Railway / AWS)
