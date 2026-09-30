# ALLinHELP — Developer Discovery Notes

**From:** Backend Developer  
**To:** ALLinHELP Product Team  
**Date:** September 24, 2026  
**Re:** PRD v1.0 + SAD v1.0 Review — Pre-Development Alignment

---

## 1. Introduction

Thank you for sharing the PRD and System Architecture Document. Both documents are well-structured and reflect a clear, ambitious product vision. This note is not a rejection of that vision — it is a developer's honest assessment to ensure we deliver something *real and valuable* within the agreed 3-month window, rather than an incomplete version of something larger.

Everything documented below is intended to protect the project's success. I am raising these points **before writing a single line of code**, because resolving them now is 10x cheaper than resolving them at month 2.

---

## 2. Development Team & Capacity

| Role | Capacity |
| ------ | ---------- |
| UI/UX Designer | Full-time |
| Frontend Developer | Full-time |
| Backend Developer | Full time |

This is a workable team for a well-scoped MVP. It is **not sufficient** for the full PRD scope as written.

---

## 3. Scope Reality Check

The PRD MVP includes the following deliverables:

- 2 separate mobile applications (Customer App + Helper App)
- 1 web application
- 1 Admin Dashboard
- 11+ backend microservices
- 6 distinct AI engines (Matching, Pricing, Trust, Dispatch, Forecast, Fraud Detection)
- Real-time GPS tracking
- In-app messaging
- Digital wallet + payment processing
- Government ID + selfie identity verification

**This scope represents an estimated 12–18 months of work for a team of this size.**

Delivering all of the above in 3 months would result in either:
(a) A partially functional system that is not production-ready, or  
(b) A technically complete system with inadequate testing, documentation, or reliability

Neither outcome serves the client or the business.

---

## 4. Our Recommendations

### 4.1 — Single App Instead of Two Separate Apps

**Proposal:** Build **one application** where the displayed interface depends on whether the user registered as a **Customer** or a **Helper (Service Provider)**.

**Reasoning:**

- Both user types share the same core functionality: registration, authentication, profiles, messaging, payments, ratings
- The differentiation is in the *view and workflow*, not the underlying system
- This halves the frontend development effort
- This is a proven pattern used by platforms like Fiverr, TaskRabbit, and numerous others
- A single codebase is significantly easier to maintain and iterate on

**Client question:** Are there specific business or brand reasons requiring two completely separate applications?

---

### 4.2 — Start with a PWA Instead of Native Apps

**Proposal:** For the 3-month MVP, build a **Progressive Web App (PWA)** instead of native mobile apps (iOS/Android).

**What is a PWA?**  
A PWA is a website that behaves like a mobile app. It installs to the home screen, works offline (partially), sends push notifications, and accesses device features like the camera and location. Examples of successful platforms using this model: **BetKing**, **Twitter Lite**, **Starbucks**, and many others.

**Why PWA for ALLinHELP MVP?**

| Factor | PWA | Native App |
|--------|-----|------------|
| Development time | ✅ 1 codebase, faster | ❌ Separate builds for iOS + Android |
| App Store approval | ✅ None needed | ❌ Apple review can take 1–4 weeks |
| Update deployment | ✅ Instant | ❌ Requires re-submission + user update |
| GPS/Location | ✅ Supported | ✅ Supported |
| Push Notifications | ✅ Supported (iOS 16.4+) | ✅ Supported |
| Camera (for ID) | ✅ Supported | ✅ Supported |
| Offline capability | ⚠️ Partial | ✅ Full |
| Real-time tracking | ⚠️ Limited (polling) | ✅ Background GPS |
| User trust/perception | ⚠️ Slightly lower | ✅ Higher |

**PWA is the recommended MVP strategy** because it allows us to deliver a working, testable product to real users without App Store delays or platform-specific development costs. Native apps can be built in Phase 2 based on validated user feedback from the PWA.

**Client question:** Is there a specific business requirement that mandates App Store presence at launch?

---

### 4.3 — Modular Monolith Instead of Full Microservices at Launch

The SAD proposes a full microservices architecture from day one. This is the *correct long-term architecture*, but it introduces significant overhead for a 3-month build:

- API gateway configuration
- Inter-service communication and failure handling
- Distributed logging and tracing
- Independent deployment pipelines per service
- Network latency between services

**Recommendation:** Build a **modular monolith** — code is organized into the exact service modules defined in the SAD (Auth, User, Helper, Booking, Matching, Pricing, etc.) but deployed as a single unit. This gives us:

- All the structural boundaries of microservices
- None of the operational overhead at launch
- A clear, low-risk path to extracting true microservices in Phase 2

This is the approach used by companies like Shopify, Stack Overflow, and Basecamp at early scale.

**Client question:** Is there a technical requirement that demands true microservices from day 1 (e.g., separate teams owning separate services)?

---

### 4.4 — AI Engines: Proposed Phase Allocation

The PRD lists 6 AI engines. We recommend the following phasing:

| AI Engine | PRD Classification | Our Recommendation |
| ----------- | ------------------- | ------------------- |
| AI Adaptive Matching Engine | MVP | ✅ Phase 1 (simplified rule-based + basic ML) |
| AI Fair Price Prediction Engine | MVP | ✅ Phase 1 (rule-based with configurable factors) |
| AI Trust Intelligence Engine | MVP | ⚠️ Phase 2 (use static rating average for MVP) |
| AI Dispatch Engine | MVP | ⚠️ Phase 2 (manual ordering sufficient for MVP) |
| AI Demand Forecasting Engine | Deferred | ✅ Phase 3 (as planned) |
| AI Fraud Detection Engine | MVP | ⚠️ Phase 2 (basic rules for MVP; full ML model later) |

**Rationale:** A rule-based Matching engine (location + rating + availability + skills) + a rule-based Pricing engine (service type + location zone + time + demand factor) are sufficient to validate the marketplace model. Complex ML is high-effort and requires real transaction data to train anyway — which we won't have at launch.

---

## 5. Proposed MVP Scope (Revised)

### ✅ Included in 3-Month MVP

- [ ] Single PWA (Customer + Helper views, role-based)
- [ ] User registration, login, and authentication (JWT + OTP)
- [ ] Identity verification (Government ID upload + admin review queue)
- [ ] Service category management (admin-configurable)
- [ ] Booking request creation and lifecycle management
- [ ] GPS-based helper discovery (location search radius)
- [ ] AI Matching Engine v1 (rule-based: proximity + rating + skills + availability)
- [ ] AI Pricing Engine v1 (rule-based: service type + location + time + urgency)
- [ ] In-app chat (real-time messaging)
- [ ] Payments (Paystack integration)
- [ ] Digital wallet (balance, transactions, withdrawal requests)
- [ ] Ratings and reviews (post-booking)
- [ ] Push notifications (booking updates, messages)
- [ ] Admin dashboard (user management, helper verification, service config, basic analytics)

### ❌ Deferred to Phase 2 (Month 4–6)

- Native iOS + Android apps
- Real-time background GPS tracking
- AI Trust Intelligence Engine
- AI Fraud Detection Engine (full ML)
- AI Dispatch Engine (optimized)
- Voice messaging
- Advanced analytics and BI dashboards

### ❌ Deferred to Phase 3 (Month 7+)

- AI Demand Forecasting
- Enterprise APIs
- Multi-language AI assistant
- Smart contracts / Insurance integration
- Predictive scheduling

---

## 6. Questions Requiring Client Response

Please respond to the following before development begins. These decisions directly affect architecture and delivery timelines.

1. **Platform:** Are you open to launching as a PWA for MVP, with native apps in Phase 2?
2. **App structure:** Can the Customer and Helper experiences live in one app (role-based views) rather than two separate apps?
3. **AI scope:** Do you accept the proposed phasing of AI engines (Matching + Pricing in Phase 1 only)?
4. **Identity verification:** Do you have a preferred third-party verification provider, or should we recommend one (e.g., Smile Identity, Dojah)?
5. **Payments:** Confirm the target launch market — Paystack for Nigeria, Flutterwave for broader Africa?
6. **Definition of "done":** Is the 3-month deliverable expected to be a production launch, or a beta/soft launch for testing?
7. **Design assets:** Is the UI/UX designer working from an existing design system or starting from scratch?
8. **Real-time GPS:** Is live GPS tracking of helpers during a job a hard MVP requirement, or can status updates (En Route → Arrived → In Progress) suffice for launch?
9. **Data and legal:** Is a Privacy Policy and Terms of Service already drafted? (Required for any payment or identity processing)

---

## 7. Proposed Development Approach

| Element | Decision | Rationale |
| --------- | ---------- | ----------- |
| Backend framework | FastAPI (Python) | Async, AI/ML-native ecosystem, auto-generated docs |
| Database | PostgreSQL | As specified in SAD — correct choice |
| Cache | Redis | As specified in SAD |
| Frontend | React (PWA) or React Native Web | Single codebase across platforms |
| Payments | Paystack | Best DX for African market |
| File storage | AWS S3 or Cloudflare R2 | Profile images, ID documents |
| Push notifications | Firebase Cloud Messaging (FCM) | Free, cross-platform |
| Real-time chat | Socket.io | Simple, reliable, well-documented |
| Deployment architecture | Modular monolith → microservices (Phase 2) | Right for timeline and team size |

---

## 8. Proposed Timeline (Pending Your Responses)

| Milestone | Target | Deliverable |
| ----------- | -------- | ------------- |
| **M0 — Alignment** | Week 1 | Agreed scope, tech stack, design kickoff |
| **M1 — Foundation** | End of Month 1 | Auth, Users, Helpers, Bookings, Admin Panel |
| **M2 — Core Flow** | End of Month 2 | Full booking flow, AI matching, payments, chat |
| **M3 — Launch Ready** | End of Month 3 | PWA deployed, tested, identity verification, ratings |

---

## 9. Next Steps

1. Client reviews this document and responds to questions in Section 6
2. Alignment call scheduled (recommended: within 5 business days)
3. Final MVP scope document produced and signed off by both parties
4. Week 1 development kickoff begins

> All scope changes after the aligned MVP document is signed will be evaluated as change requests and may affect the delivery timeline.

---

*This document was prepared in good faith to ensure project success. We are committed to delivering a high-quality, production-ready MVP within the agreed timeline, provided scope is aligned as proposed.*

**Okafor Patrick C**
**Backend Developer — ALLinHELP Project**  
*September 24, 2026*
