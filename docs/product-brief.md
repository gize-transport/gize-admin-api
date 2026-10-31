# Product brief – Gize Admin API

## The business

Gize Transport (ጊዜ ትራንስፖርት) sells **monthly minibus subscriptions** in Addis Ababa, Ethiopia. A route takes riders from one area to another (A to B) within set times; riders board at A or in an area the route passes, and ride to work or school every weekday. Gize matches riders to routes, collects payment and pays partner drivers. Website: gizetransport.com. Target scale: up to ~15,000 subscribers and ~1,000 vans at peak; under 1,000 subscribers in the first six months.

## Who uses this API

| Client | Users | What they do |
| --- | --- | --- |
| Admin portal (Next.js, separate repo) | Gize **Admin** and **Staff** | Manage routes, riders, companies, route requests, drivers |
| Rider registration app (mobile) | The public: adult riders, parents | Sign up for a route, or request one |
| Later: rider app, driver app, company portal | Riders, parents, drivers, company HR | Not in scope now; design so they can be added |

## Roles and permissions

| Action | Staff | Admin |
| --- | --- | --- |
| View and edit routes, the areas they serve, times, seats | Yes | Yes |
| Change prices | No | Yes |
| View and edit riders, move riders between routes | Yes | Yes |
| Manage areas, corridors, companies (including deactivating one) and route requests; move routes through their statuses (forming, launch, pause); add a van | Yes | Yes |
| Review driver applications, record inspections, approve drivers | Yes | Yes |
| Delete (soft-delete) any record | No | Yes |
| Create, deactivate and change staff accounts and roles | No | Yes |
| Read the audit log | No | Yes |

Public (unauthenticated, rate-limited) endpoints exist only for rider registration, route requests and phone verification.

## Two kinds of customer (owner decision, GZ-025)

| | Individual riders | Companies |
| --- | --- | --- |
| Routes | `public` (adults) and `student` | `corporate`, one company's own route |
| Payment | **Prepaid** monthly: a paid month is guaranteed, used or not | **Postpaid**: billed after the period for the service provided (they may not need service some days, e.g. seminars) |
| Joining and leaving | From the 1st of a month; leaving takes effect at the end of a month | Any day; the company decides who rides |
| Starting a route | Collects sign-ups (`forming`) until its launch minimum is reached | Starts from the agreement with the company |
| Prices on the route | Round trip, one-way, Saturday add-on | None: the company's rate is agreed with the company |

## Business rules the API must enforce

**Routes**
- A route goes from point A to point B: it runs one corridor (A to B) and may pass other areas on the way, the **areas it serves** (in order, e.g. Ayat → CMC → Megenagna), where riders also board or get off. There are no stops with their own times (owner decision, 2026-10-31): what counts is punctuality from A to B.
- Times: the morning run **leaves A** and **arrives at B** at set times; the evening run leaves B and arrives at A. Riders get **5 minutes** at A to board, and the van may reach B up to **5 minutes** after its arrival time (fixed rules; checked once trips are recorded, later).
- Route types: `public` (adult commuters), `student` (one school per route), `corporate` (one company's staff).
- Each route is one van, with a seat count; a route is never overbooked. Seats are counted per day: a rider holds a seat on every day from their start date to their end date. Individual riders hold whole months (they start on the 1st and leave at a month's end); company riders can swap on any day.
- Prices (ETB per month, whole birr): round trip, one-way, Saturday add-on (`public` routes only, optional). Only `public` and `student` routes have prices. Only Admin changes prices; copying a route (add a van) copies its prices unchanged.
- Areas (neighbourhoods and landmarks) are a list staff manage; a corridor is a pair of areas (from, to); a route's served areas come from the same list.
- Route times are local Addis Ababa times of day, in order: leave A, arrive at B, leave B, arrive at A.
- Route status:
  - `public` and `student`: `draft` → `forming` (needs round-trip and one-way prices) → `active` (staff launch it for the 1st of a month, not before today, once riders reserved for that month reach the route's launch minimum; they become active riders from that day, reservations for an earlier month move to it, and reservations for a later month become active from their own start date; a launch for a later month makes the route `active` at once, owner decision 2026-10-31) ⇄ `paused`. Back from `forming` to `draft` only while nobody is reserved.
  - `corporate`: `draft` → `active` (needs an active company) ⇄ `paused`.
  - Admin soft-deletes a route only while no rider is reserved or active on it.
- A route's corridor and school are fixed once it has prices or open route requests (owner decision, after M2): to serve somewhere else, Gize adds a new route.
- When a route is full, Gize adds another van: staff copy the route (times, areas served, prices, type, school or company) under a new code, with its own seats and launch minimum. A copy of an individual route starts `forming` if the original meets the forming requirements, otherwise `draft`; a copy of a corporate route starts `draft`.

**Riders**
- Adult riders register themselves with name, phone, home area, destination area, plan (round trip / morning only / evening only), optional Saturday add-on.
- Students (elementary or high school) never have their own login; a **parent/guardian** registers and owns the child's record. Students ride only on their school's student route, never with adults.
- Corporate riders belong to a company and ride that company's dedicated route. Gize staff record them (name, phone, company) so drivers have a passenger list and seats are counted.
- Phone numbers are verified by SMS one-time code before a public registration is accepted.
- Individual subscriptions are prepaid and monthly; no mid-month start, no trial, no pause. A rider who leaves keeps their seat to the end of the paid month: **a freed seat stays empty until the next month** (owner decision, GZ-025). Company riders start and stop on any day.
- (Payments and invoicing are out of scope now, but the data model must not block either path.)

**Route requests and forming routes** (replace the corridor waitlist, owner decision GZ-025)
- There is no waitlist. Unmet demand from individual riders is a **route request**, of one of two kinds:
  - **new areas**: no route serves their from/to areas yet;
  - **a specific route**: they want a particular route that is running (usually because its van is full).
- Staff record requests from phone calls; later the public app does too (phone-verified). A request is `new`; staff mark it `matched` to a route (a forming one, or a van to come), `fulfilled` once the person has a place, or `declined` with a reason. `new` and `matched` requests are open. Staff see counts per area pair and per route, next to free seats, to decide where the next route or van goes.
- A route that is `forming` collects **reservations** until its launch minimum (set per route, at most its seats) is reached; staff then launch it.
- A free seat on a running route (freed by a leaver, or never filled) goes, from the next month, to the oldest open request for that route; staff may skip a request, giving a reason. Requests are offered a seat (by SMS, later) and the offer expires. The public app never gives a place on an active route directly: it reserves on a forming route or records a request, so nobody jumps the queue.
- Companies' demand is handled with the company directly: staff add a route or a van for it.

**Companies**
- A company account has a name, a contact person (name, phone, email), billing `postpaid`, and is active or inactive. A corporate route belongs to one company. Staff may deactivate a company while none of its routes is active or paused; only Admin deletes one.
- Companies may need no service on some days (seminars, holidays): staff record these days later (M4), so drivers know and billing can count them.

**Drivers**
- Owners/drivers apply (name, phone, vehicle type, seats, plate, areas served, owner or driver).
- Staff review documents (licence, vehicle registration, insurance), record a vehicle inspection (pass/fail + notes), then approve or reject.
- Only approved drivers with a passed inspection can be assigned to a route.
- Drivers are free to work elsewhere between morning and evening runs.

## Out of scope for now

Payments and payouts, live tracking, trips and boarding records, complaints, schools calendar, corporate invoicing, backup-van dispatch, reports. Design the data model so these can be added without rewriting the core tables.

## Open questions (do not guess – ask)

- How companies are charged (per seat per month, per van per day, or per day run): decides what M4 records; nothing before depends on it.
- Notice a rider must give to leave at the end of the month (e.g. by the 25th), if any.
- How long a seat offer to a requester stays open (48 hours proposed; M4).
- SMS provider (an Ethiopian SMS gateway) – build behind an interface with a fake implementation until chosen.
- Exact driver document list and inspection checklist.
