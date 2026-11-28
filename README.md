# Dental Clinic Management System

A web app for a small dental clinic with 2–3 dentists and front-desk staff. It handles staff logins and roles, dentists' working hours and leave, patient registration with a dental medical-history checklist, appointment booking, walk-ins with token numbers, the live waiting queue, dental visit notes with a tooth chart, treatment plans, printable prescriptions, and billing with receipts and a daily collection report.

It runs in any browser on the reception PC, the doctor's laptop or a tablet. There's nothing to install on those machines.

## What each role can do

| | Admin / Owner | Doctor | Nurse | Receptionist |
|---|:-:|:-:|:-:|:-:|
| Register and edit patients | ✓ | ✓ | ✓ | ✓ |
| Book appointments, add walk-ins, check in, cancel | ✓ | ✓ | ✓ | ✓ |
| See visit notes, tooth chart, treatment plan, prescriptions | ✓ | ✓ | ✓ | – |
| Call in a patient, write notes, finish a visit | ✓ | own patients only | vitals and notes | – |
| Plan procedures / mark them done | ✓ | plan and mark done | plan only | – |
| Create bills, take payments, print receipts | ✓ | ✓ | ✓ | ✓ |
| See the daily collection report and dues | ✓ | – | – | ✓ |
| Void a payment, cancel a bill, edit the price list | ✓ | – | – | – |
| Add or disable staff, set doctor hours and leave | ✓ | – | – | – |
| View the access log | ✓ | – | – | – |

## How a day works

1. **Reception** searches for the patient by name, phone or ID. A new patient gets registered with their consent ticked, the medical-history checklist filled in (diabetes, blood thinners, pregnancy and so on), and gets an ID like `P000123`.
2. **Booking:** pick the doctor and date, and the system shows only free slots inside that doctor's working hours. Leave days, past times and booked slots never appear. Each doctor gets token numbers in order (#1, #2…) every day.
3. **Walk-ins** go straight into today's waiting queue with the next token.
4. When a booked patient arrives, reception clicks **Check in**, and the patient moves to *Waiting*.
5. The **dentist** sees only their own queue and clicks **Call in**. The visit screen shows any medical alerts in red at the top. The dentist records BP, pulse and sugar, the chief complaint and findings, marks teeth on the **dental chart** (FDI numbering, milk teeth too), and adds procedures from the price list to the **treatment plan**. Procedures done today are marked **Done**, and the rest stay planned for later visits. **Save & finish** completes the visit. The prescription prints on the clinic letterhead, with the treatment done and what's planned next.
6. The finished visit appears under **Ready to bill** on the Today screen. Reception clicks **Create bill**: the procedures marked done are already ticked, with the consultation fee. Reception adds anything else (X-rays, medicines), gives a discount if any, and records the payment by cash, UPI, card or bank transfer. Part payments are fine, and the balance shows on the patient's record until it's paid.
7. **Print receipt** gives a bill-cum-receipt with the amount in words. Bills are numbered by financial year (`2026-27/0001`).
8. At closing time, **Collections** shows the day's total by payment mode, every payment, and all money still due. It can be downloaded as CSV for the accountant.

Bills and payments are never deleted. A wrong payment is **voided** with a reason, and a wrong bill is **cancelled** with a reason (after voiding its payments). Both are admin-only and recorded in the access log. Cancelling a bill frees its procedures to be billed again.

The **Price list** (admin) starts with 31 common dental procedures at typical fees. Edit them to the clinic's own fees before go-live. Dental treatment is exempt from GST in India, so bills carry no GST. If the clinic offers purely cosmetic work that its accountant says is taxable, that needs adding.

The appointment status can only move in this order: Scheduled → Waiting → With doctor → Completed. A scheduled or waiting appointment can also be Cancelled or marked No-show. The database itself blocks two active bookings in the same doctor's slot, even if two receptionists click at the same moment.

## Run it on a laptop (5 minutes)

```bash
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_demo          # demo doctors, staff and patients
python manage.py runserver
```

Open http://127.0.0.1:8000. The demo logins are `admin`, `reception`, `nurse`, `dr.kumar` (dental surgeon), `dr.meena` (orthodontist) and `dr.sameer` (endodontist), all with the password `Clinic@2026`. Patient Ravi Shankar has a past visit with a tooth chart, a treatment plan and a part-paid bill.

Run the tests with `python manage.py test clinic`.

## Demo / staging on Render

The repo includes a `render.yaml` Blueprint (web service + PostgreSQL, Singapore region).

1. Push this repo to GitHub, then in Render: **New → Blueprint** and pick the repo.
2. Render asks for `DJANGO_SUPERUSER_PASSWORD`. That becomes the password for the `owner` admin login.
3. Wait for the build (`build.sh`: install, collectstatic, migrate, create owner). Open `https://<service>.onrender.com`.

Free-plan notes: the web service sleeps after 15 minutes idle (first load takes ~1 minute), and the free database expires after 30 days. **Use Render for demos with dummy data only.** Render has no India region, so real patient data belongs on the India-hosted server below.

## Going live

**Server:** a small Linux VM in an Indian region (AWS Mumbai, Azure Central India or GCP Mumbai) with 1–2 GB RAM is plenty for one clinic. Use managed PostgreSQL if the budget allows.

```bash
# on the server
git clone <your repo> /srv/clinicms && cd /srv/clinicms
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in every value, including CLINIC_ADDRESS and CLINIC_PHONE for receipts
set -a; source .env; set +a
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser        # the clinic owner's admin login
gunicorn config.wsgi --bind 127.0.0.1:8001 --workers 3
```

Put Nginx in front with an HTTPS certificate from Let's Encrypt (`certbot --nginx`). Serve `/static/` from the `staticfiles/` folder, and run gunicorn under systemd so it restarts on reboot. **Never run with `DJANGO_DEBUG=true` in production.** The app refuses to start without a real secret key.

After go-live, log in as the owner, open **Doctors & staff**, add each doctor with their working hours, then add the receptionist and nurse logins. Do **not** run `seed_demo` on the live server.

**Backups:** `scripts/backup.sh` dumps the database every night and keeps 30 days of copies. Schedule it with cron, copy the backups off the server as well, and **test a restore before opening day**.

## Patient data protection

The app has these built in:
- **Consent:** a patient can't be registered without consent being ticked, and the time of consent is stored.
- **Role-based access:** receptionists never see clinical notes, and doctors can only open consultations for their own patients.
- **Access log:** every login, failed login, record view, edit and status change is logged with the user, time and IP address. Nobody can edit or delete the log, not even the admin.
- **Sessions:** staff are logged out after 8 hours or when the browser closes. In production, cookies are HTTPS-only, with HSTS and clickjacking protection on.
- **Staff who leave:** untick "Can log in" for them. Their history is kept, but they can no longer sign in.

The clinic also needs to handle these outside the app:
- Host in India, and encrypt the database disk and the backups.
- Show patients a short privacy notice (what's collected, why, how long it's kept, and who to contact).
- Keep a written breach-response plan. Under India's DPDP Rules 2025, a breach must be reported to the Data Protection Board and to the affected patients within 72 hours.
- Have a lawyer review the setup before go-live.

## Project layout

```
config/            settings, URLs
clinic/models.py   User (roles), Doctor, DoctorAvailability, DoctorLeave, Patient,
                   Appointment, Consultation (incl. dental chart), Procedure (price list),
                   TreatmentItem, Invoice, InvoiceItem, Payment, AuditLog
clinic/views.py    patients, appointments, visits, staff
clinic/views_billing.py  bills, payments, receipts, collections, price list
clinic/access.py   role checks and audit logging
clinic/forms.py    forms and validation
clinic/templates/  HTML screens        clinic/static/  styles
clinic/templatetags/clinic_extras.py   ₹ formatting (1,23,456) and amounts in words
clinic/tests.py    41 tests: slots, double booking, tokens, permissions, visit flow,
                   dental chart, treatment plan, billing, payments, collections
scripts/backup.sh  nightly database backup
```

The built-in Django admin is at `/django-admin/` (owner only), for fixing data directly.

## Suggested next phases

1. **WhatsApp/SMS reminders:** a reminder the day before each appointment (e.g. via the WhatsApp Business API or MSG91), plus payment reminders for dues.
2. **Treatment estimates:** print the planned procedures with fees as a quotation for the patient.
3. **X-ray and photo uploads:** IOPA/OPG images and lab reports on the patient's record.
4. **Medicine list:** autocomplete prescriptions from a saved medicine list.
5. **Online booking page** for patients, using the same slot rules.
6. **ABHA / ABDM integration.**
