# Product brief – Gize Admin API

## The business

Gize Transport (ጊዜ ትራንስፖርት) sells **monthly minibus subscriptions** in Addis Ababa, Ethiopia. Riders board partner minibuses at fixed pickup points along a corridor at fixed times and ride to work or school every weekday. Gize matches riders to routes, collects payment and pays partner drivers. Website: gizetransport.com. Target scale: up to ~15,000 subscribers and ~1,000 vans at peak; under 1,000 subscribers in the first six months.

## Who uses this API

| Client | Users | What they do |
| --- | --- | --- |
| Admin portal (Next.js, separate repo) | Gize **Admin** and **Staff** | Manage routes, riders, waitlists, drivers |
| Rider registration app (mobile) | The public: adult riders, parents | Register for a route or join a waitlist |
| Later: rider app, driver app, company portal | Riders, parents, drivers, company HR | Not in scope now; design so they can be added |

## Roles and permissions

| Action | Staff | Admin |
| --- | --- | --- |
| View and edit routes, pickup points, times, seats | Yes | Yes |
| Change prices | No | Yes |
| View and edit riders, move riders between routes | Yes | Yes |
| Manage waitlists, open a route from a waitlist | Yes | Yes |
| Review driver applications, record inspections, approve drivers | Yes | Yes |
| Delete (soft-delete) any record | No | Yes |
| Create, deactivate and change staff accounts and roles | No | Yes |
| Read the audit log | No | Yes |

Public (unauthenticated, rate-limited) endpoints exist only for rider registration, waitlist sign-up and phone verification.

## Business rules the API must enforce

**Routes**
- A route runs one corridor with fixed pickup points (ordered), a morning time and an evening return time.
- Route types: `public` (adult commuters), `student` (one school per route), `corporate` (one company's staff).
- Each route has a seat count; a route is never overbooked.
- Prices (ETB per month, whole birr): round trip, one-way, Saturday add-on (adult routes only). Only Admin changes prices.

**Riders**
- Adult riders register themselves with name, phone, home area, destination area, plan (round trip / morning only / evening only), optional Saturday add-on.
- Students (elementary or high school) never have their own login; a **parent/guardian** registers and owns the child's record. Students ride only on their school's student route, never with adults.
- Corporate riders belong to a company and ride that company's dedicated route.
- Phone numbers are verified by SMS one-time code before a public registration is accepted.
- Subscriptions are monthly; no mid-month start, no trial, no pause. (Payments are out of scope now, but the data model must not block them.)

**Waitlists**
- A rider from an area with no matching route joins the **waitlist for a corridor**.
- A corridor has an opening threshold (number of committed riders) – **[THRESHOLD, to be confirmed]**.
- When the threshold is reached, staff can open a route from the waitlist; waitlisted riders are assigned (respecting seats) and notified by SMS.

**Drivers**
- Owners/drivers apply (name, phone, vehicle type, seats, plate, areas served, owner or driver).
- Staff review documents (licence, vehicle registration, insurance), record a vehicle inspection (pass/fail + notes), then approve or reject.
- Only approved drivers with a passed inspection can be assigned to a route.
- Drivers are free to work elsewhere between morning and evening runs.

## Out of scope for now

Payments and payouts, live tracking, trips and boarding records, complaints, schools calendar, corporate invoicing, backup-van dispatch, reports. Design the data model so these can be added without rewriting the core tables.

## Open questions (do not guess – ask)

- Waitlist opening threshold per corridor.
- SMS provider (an Ethiopian SMS gateway) – build behind an interface with a fake implementation until chosen.
- Exact driver document list and inspection checklist.
