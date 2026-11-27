"""Load demo staff, dentists, patients, treatments and bills so the clinic can try the system before go-live.

    python manage.py seed_demo            # adds demo data
    python manage.py seed_demo --flush    # wipes clinic data first (never use on live data)
"""
from datetime import date, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from clinic.models import (
    Appointment, AuditLog, Consultation, Doctor, DoctorAvailability, DoctorLeave, Invoice, InvoiceItem, Patient, Payment,
    Procedure, TreatmentItem, User,
)

DEMO_PASSWORD = "Clinic@2026"


class Command(BaseCommand):
    help = "Create demo users, doctors with schedules, patients and today's appointments."

    def add_arguments(self, parser):
        parser.add_argument("--flush", action="store_true", help="Delete existing clinic data first")

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts["flush"]:
            for m in (AuditLog, Payment, InvoiceItem, Invoice, TreatmentItem, Consultation, Appointment,
                      DoctorLeave, DoctorAvailability, Doctor, Patient):
                m.objects.all().delete()
            User.objects.filter(is_superuser=False).delete()

        def user(username, first, last, role, **extra):
            u, _ = User.objects.get_or_create(username=username, defaults={"first_name": first, "last_name": last, "role": role, **extra})
            u.set_password(DEMO_PASSWORD)
            u.is_staff = role == User.Role.ADMIN
            u.save()
            return u

        admin = user("admin", "Clinic", "Owner", User.Role.ADMIN)
        user("reception", "Priya", "Nair", User.Role.RECEPTIONIST, phone="9876500001")
        user("nurse", "Anita", "Joseph", User.Role.NURSE)

        doctors = []
        for uname, first, last, spec, qual, fee, slot, blocks in [
            ("dr.kumar", "Arjun", "Kumar", "Dental Surgeon", "BDS", 300, 30,
             [(d, time(9), time(13)) for d in range(0, 6)] + [(d, time(17), time(20)) for d in range(0, 5)]),
            ("dr.meena", "Meena", "Rao", "Orthodontist", "BDS, MDS (Orthodontics)", 500, 20,
             [(d, time(10), time(14)) for d in (0, 1, 2, 3, 4, 5)]),
            ("dr.sameer", "Sameer", "Iyer", "Endodontist", "BDS, MDS (Endodontics)", 500, 45,
             [(d, time(16), time(20)) for d in (0, 2, 4, 5)]),
        ]:
            u = user(uname, first, last, User.Role.DOCTOR)
            doc, _ = Doctor.objects.get_or_create(user=u, defaults={
                "specialization": spec, "qualification": qual, "consultation_fee": fee, "slot_minutes": slot,
                "registration_number": f"A-{1000 + len(doctors) * 137}"})
            doc.availability.all().delete()
            for wd, s, e in blocks:
                DoctorAvailability.objects.create(doctor=doc, weekday=wd, start_time=s, end_time=e)
            doctors.append(doc)

        people = [
            ("Ravi", "Shankar", "M", 45, "9840011111", "Penicillin",
             {"has_diabetes": True, "current_medications": "Metformin 500mg twice daily", "tobacco_use": "smoking",
              "dental_history": "Filling on 46 in 2021. Last check-up 2 years ago.", "referred_by": "Google Maps"}),
            ("Lakshmi", "Venkat", "F", 32, "9840022222", "",
             {"has_thyroid_disorder": True, "is_pregnant": True, "current_medications": "Thyroxine 50mcg",
              "referred_by": "Friend (Divya S.)"}),
            ("Aarav", "Menon", "M", 6, "9840033333", "",
             {"has_asthma": True, "dental_history": "First dental visit."}),
            ("Fatima", "Begum", "F", 58, "9840044444", "",
             {"has_hypertension": True, "on_blood_thinners": True, "current_medications": "Amlodipine 5mg, Aspirin 75mg",
              "dental_history": "Upper partial denture since 2018."}),
            ("Karthik", "Raj", "M", 27, "9840055555", "", {"tobacco_use": "chewing", "referred_by": "Instagram"}),
            ("Divya", "Subramanian", "F", 24, "9840066666", "Sulfa drugs", {"dental_history": "Braces 2012–2014."}),
        ]
        patients = []
        for first, last, g, age, phone, allergy, extra in people:
            p = Patient.objects.filter(phone=phone).first()
            if not p:
                p = Patient(first_name=first, last_name=last, gender=g, age_years=age, phone=phone,
                            allergies=allergy, consent_given=True, created_by=admin, **extra)
                p.save()
            patients.append(p)

        def proc(name):
            return Procedure.objects.get(name=name)

        today = timezone.localdate()
        # A past visit with a chart, a treatment plan and a part-paid bill, so every screen has something in it.
        past_day = today - timedelta(days=14)
        if not Appointment.objects.filter(patient=patients[0], date=past_day).exists():
            old = Appointment(patient=patients[0], doctor=doctors[0], date=past_day, time=time(10), reason="Toothache",
                              status=Appointment.Status.COMPLETED, created_by=admin)
            old.save()
            Consultation.objects.create(
                appointment=old, bp_systolic=138, bp_diastolic=88, pulse=78, blood_sugar=162,
                complaints="Pain in lower left back tooth for 4 days, worse at night and with cold water.",
                extraoral_findings="No facial swelling. Mouth opening normal.",
                intraoral_findings="Deep caries 36. Generalised calculus. Gums bleed on probing.",
                oral_hygiene="fair", investigations="IOPA 36: deep caries involving pulp, no periapical lesion.",
                dental_chart={"36": ["caries"], "46": ["filled"], "18": ["impacted"], "28": ["missing"], "24": ["caries"]},
                diagnosis="36 irreversible pulpitis. 24 dentine caries. Chronic generalised gingivitis.",
                prescription="Amoxicillin 500mg — 1-1-1 — 5 days\nIbuprofen 400mg + Paracetamol 325mg — SOS for pain",
                advice="Warm saline rinses. Avoid chewing on the left side until the RCT is complete.",
                follow_up_date=today,
            )
            done = [
                TreatmentItem(patient=patients[0], procedure=proc("IOPA X-ray (per film)"), teeth="36", price=200),
                TreatmentItem(patient=patients[0], procedure=proc("Root canal treatment — molar"), teeth="36", price=5500,
                              notes="1st sitting: access opening, working length"),
            ]
            planned = [
                TreatmentItem(patient=patients[0], procedure=proc("Composite filling (per tooth)"), teeth="24", price=1200),
                TreatmentItem(patient=patients[0], procedure=proc("Scaling & polishing"), price=1200),
                TreatmentItem(patient=patients[0], procedure=proc("Porcelain-fused-to-metal (PFM) crown"), teeth="36", price=4000),
            ]
            for t in done + planned:
                t.planned_at, t.created_by = old, admin
                t.save()
            for t in done:
                t.status, t.done_at, t.doctor, t.completed_on = TreatmentItem.Status.COMPLETED, old, doctors[0], past_day
                t.save()
            bill = Invoice.objects.create(patient=patients[0], appointment=old, date=past_day, created_by=admin,
                                          notes="RCT 36 — balance at next sitting")
            InvoiceItem.objects.create(invoice=bill, description=f"Consultation — {doctors[0]}", unit_price=doctors[0].consultation_fee)
            for t in done:
                InvoiceItem.objects.create(invoice=bill, description=t.description, unit_price=t.price, treatment=t)
            bill.recalculate()
            Payment.objects.create(invoice=bill, amount=3000, mode=Payment.Mode.UPI, reference="UPI 4122 8890 1734",
                                   received_by=admin, received_at=timezone.now() - timedelta(days=14))

        # Today's schedule for whichever doctors work today.
        for doc in doctors:
            free = doc.free_slots(today)
            for p, slot in zip(patients[:3] if doc == doctors[0] else patients[3:5], free[:3]):
                if not Appointment.objects.filter(patient=p, date=today).exists():
                    reason = "Root canal sitting" if p == patients[0] else ("Braces review" if doc == doctors[1] else "Check-up")
                    Appointment(patient=p, doctor=doc, date=today, time=slot, reason=reason, created_by=admin).save()

        self.stdout.write(self.style.SUCCESS(
            f"Demo dental clinic ready. Log in as admin / reception / nurse / dr.kumar / dr.meena / dr.sameer "
            f"with password {DEMO_PASSWORD}"
        ))
