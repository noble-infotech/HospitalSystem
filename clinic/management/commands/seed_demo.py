"""Load demo staff, doctors and patients so the clinic can try the system before go-live.

    python manage.py seed_demo            # adds demo data
    python manage.py seed_demo --flush    # wipes clinic data first (never use on live data)
"""
from datetime import date, time, timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from clinic.models import Appointment, AuditLog, Consultation, Doctor, DoctorAvailability, DoctorLeave, Patient, User

DEMO_PASSWORD = "Clinic@2026"


class Command(BaseCommand):
    help = "Create demo users, doctors with schedules, patients and today's appointments."

    def add_arguments(self, parser):
        parser.add_argument("--flush", action="store_true", help="Delete existing clinic data first")

    @transaction.atomic
    def handle(self, *args, **opts):
        if opts["flush"]:
            for m in (AuditLog, Consultation, Appointment, DoctorLeave, DoctorAvailability, Doctor, Patient):
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
        for uname, first, last, spec, fee, slot, blocks in [
            ("dr.kumar", "Arjun", "Kumar", "General Physician", 400, 15,
             [(d, time(9), time(13)) for d in range(0, 6)] + [(d, time(17), time(20)) for d in range(0, 5)]),
            ("dr.meena", "Meena", "Rao", "Paediatrician", 500, 20,
             [(d, time(10), time(14)) for d in (0, 1, 2, 3, 4, 5)]),
            ("dr.sameer", "Sameer", "Iyer", "Dermatologist", 600, 30,
             [(d, time(16), time(20)) for d in (0, 2, 4, 5)]),
        ]:
            u = user(uname, first, last, User.Role.DOCTOR)
            doc, _ = Doctor.objects.get_or_create(user=u, defaults={"specialization": spec, "consultation_fee": fee, "slot_minutes": slot})
            doc.availability.all().delete()
            for wd, s, e in blocks:
                DoctorAvailability.objects.create(doctor=doc, weekday=wd, start_time=s, end_time=e)
            doctors.append(doc)

        people = [
            ("Ravi", "Shankar", "M", 45, "9840011111", "Penicillin", "Type 2 diabetes (2019), on Metformin 500mg"),
            ("Lakshmi", "Venkat", "F", 32, "9840022222", "", "Hypothyroidism"),
            ("Aarav", "Menon", "M", 6, "9840033333", "Peanuts", "Mild asthma"),
            ("Fatima", "Begum", "F", 58, "9840044444", "", "Hypertension, on Amlodipine 5mg"),
            ("Karthik", "Raj", "M", 27, "9840055555", "", ""),
            ("Divya", "Subramanian", "F", 24, "9840066666", "Sulfa drugs", ""),
        ]
        patients = []
        for first, last, g, age, phone, allergy, hist in people:
            p = Patient.objects.filter(phone=phone).first()
            if not p:
                p = Patient(first_name=first, last_name=last, gender=g, age_years=age, phone=phone,
                            allergies=allergy, medical_history=hist, consent_given=True, created_by=admin)
                p.save()
            patients.append(p)

        today = timezone.localdate()
        # A past visit with notes, so the history panels have something in them.
        past_day = today - timedelta(days=14)
        if not Appointment.objects.filter(patient=patients[0], date=past_day).exists():
            old = Appointment(patient=patients[0], doctor=doctors[0], date=past_day, time=time(10), reason="Sugar review",
                              status=Appointment.Status.COMPLETED, created_by=admin)
            old.save()
            Consultation.objects.create(
                appointment=old, bp_systolic=138, bp_diastolic=88, pulse=78, weight_kg=81,
                complaints="Routine diabetes review. Fasting sugar 142.", diagnosis="Type 2 DM — fair control",
                prescription="Metformin 500mg — 1-0-1 — 30 days\nVitamin D3 60K — once a week — 8 weeks",
                advice="Walk 30 min daily. Reduce rice portions.", follow_up_date=today,
            )

        # Today's schedule for whichever doctors work today.
        for doc in doctors:
            free = doc.free_slots(today)
            for p, slot in zip(patients[:3] if doc == doctors[0] else patients[3:5], free[:3]):
                if not Appointment.objects.filter(patient=p, date=today).exists():
                    Appointment(patient=p, doctor=doc, date=today, time=slot, reason="Consultation", created_by=admin).save()

        self.stdout.write(self.style.SUCCESS(
            f"Demo data ready. Log in as admin / reception / nurse / dr.kumar / dr.meena / dr.sameer "
            f"with password {DEMO_PASSWORD}"
        ))
