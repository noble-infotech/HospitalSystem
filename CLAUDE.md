# Project context for Claude Code

## What this is
A custom clinic management system for a client opening a small clinic in India (go-live around mid-December 2026).
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
  Patient (auto ID `P000001`, consent required), Appointment (slot + per-doctor daily token, status state machine,
  DB unique constraint against double booking), Consultation (vitals, diagnosis, prescription), AuditLog (append-only)
- `clinic/views.py`: all screens; `clinic/access.py`: `role_required` decorator and `audit()` helper
- `clinic/forms.py`, `clinic/templates/clinic/*.html`, `clinic/static/clinic/app.css`
- `clinic/tests.py`: 25 tests, all passing. Run: `python manage.py test clinic`
- `clinic/management/commands/seed_demo.py`: demo data (password `Clinic@2026`). Never run on production.

## Rules to keep
- Receptionists must never see consultation notes (`user.can_see_clinical`).
- Doctors only open consultations for their own appointments.
- Every patient view/create/edit/status change calls `audit()`.
- Appointment status only moves via `Appointment.move_to()` (enforces allowed transitions).
- Patient data protection follows India's DPDP Act 2023 / DPDP Rules 2025: consent, access control, audit trail,
  India-hosted servers, encrypted backups, 72-hour breach reporting.

## Status
Done: staff/doctor management, schedules and leave, patient registration and search, slot booking, walk-ins,
live queue dashboard, consultation notes, printable prescription, access log, backup script, README.

## Next up (agreed roadmap)
1. Billing: fee receipts (cash/UPI), daily collection report
2. WhatsApp/SMS appointment reminders
3. Lab report uploads on the patient record
4. Medicine list with autocomplete for prescriptions
5. Online patient booking page
6. ABHA/ABDM integration
Also before go-live: deploy to an India-region server (Nginx + gunicorn + PostgreSQL + HTTPS), test a backup restore.
