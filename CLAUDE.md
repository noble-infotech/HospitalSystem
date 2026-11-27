# Project context for Claude Code

## What this is
A custom clinic management system for a client opening a small **dental** clinic in India (go-live around mid-December 2026).
The client explicitly did NOT want off-the-shelf clinic software. Main focus:
1. Managing 2–3 doctors and staff (logins, roles, working hours, leave)
2. Recording incoming patients and their data
3. Appointment booking (plus walk-ins and a daily token queue)

## Stack
Django (5.2+/6.x), server-rendered templates, plain CSS/JS (no frontend build step).
SQLite for local dev; PostgreSQL in production when `POSTGRES_DB` is set. Config via env vars (see `.env.example`).
Time zone: Asia/Kolkata. Always use `timezone.localdate()` / `timezone.localtime()`, never `date.today()`.

## Key files
- `clinic/models.py`: User (role: admin/doctor/nurse/receptionist), Doctor, DoctorAvailability (weekly blocks), DoctorLeave,
  Patient (auto ID `P000001`, consent required, dental medical-alert checklist), Appointment (slot + per-doctor daily
  token, status state machine, DB unique constraint against double booking), Consultation (vitals, dental exam,
  `dental_chart` JSON `{FDI tooth: [conditions]}`, diagnosis, prescription), Procedure (price list, seeded by
  migration 0003), TreatmentItem (planned → done, per patient), Invoice (FY-numbered `2026-27/0001`, lines fixed once
  issued) / InvoiceItem / Payment (cash/UPI/card/bank, voided never deleted), AuditLog (append-only)
- `clinic/views.py`: patient, appointment, visit and staff screens; `clinic/views_billing.py`: bills, payments,
  receipts, collection report, price list; `clinic/access.py`: `role_required` decorator and `audit()` helper
- `clinic/templatetags/clinic_extras.py`: `inr` (Indian digit grouping) and `amount_in_words` filters
- `clinic/forms.py`, `clinic/templates/clinic/*.html`, `clinic/static/clinic/app.css`
- `clinic/tests.py`: 41 tests, all passing on SQLite and PostgreSQL. Run: `python manage.py test clinic`
- `clinic/management/commands/seed_demo.py`: demo data (password `Clinic@2026`). Never run on production.

## Rules to keep
- Receptionists must never see consultation notes (`user.can_see_clinical`).
- Doctors only open consultations for their own appointments.
- Every patient view/create/edit/status change calls `audit()`.
- Appointment status only moves via `Appointment.move_to()` (enforces allowed transitions).
- Bills and payments are never edited or deleted: payments are voided (`Payment.void`), bills cancelled
  (`Invoice.cancel`, only with no active payments), admin-only, with a reason, and audited.
- A completed TreatmentItem is billed at most once (excluding cancelled bills); checked again inside the create transaction.
- Sequential numbers (patient codes, bill numbers) go through `save_with_next_number()`, which retries on a clash.
- Patient data protection follows India's DPDP Act 2023 / DPDP Rules 2025: consent, access control, audit trail,
  India-hosted servers, encrypted backups, 72-hour breach reporting.

## Status
Done: staff/doctor management, schedules and leave, patient registration and search (dental history), slot booking,
walk-ins, live queue dashboard, dental visit notes with tooth chart and treatment plan, printable prescription,
billing (bills, part payments, receipts, collections report, dues, price list), access log, backup script,
Render deploy config, README.

## Next up (agreed roadmap)
1. WhatsApp/SMS appointment and dues reminders
2. Printable treatment estimates
3. X-ray / lab report uploads on the patient record
4. Medicine list with autocomplete for prescriptions
5. Online patient booking page
6. ABHA/ABDM integration
Also before go-live: deploy to an India-region server (Nginx + gunicorn + PostgreSQL + HTTPS), test a backup restore.
