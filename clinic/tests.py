from datetime import time, timedelta

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Appointment, AuditLog, Consultation, Doctor, DoctorAvailability, DoctorLeave, Patient, User

PW = "Test@pass123"


def next_weekday(wd):
    """The next date (strictly after today) that falls on weekday wd."""
    d = timezone.localdate() + timedelta(days=1)
    while d.weekday() != wd:
        d += timedelta(days=1)
    return d


class Base(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user("admin", password=PW, role=User.Role.ADMIN, first_name="Owner")
        self.recep = User.objects.create_user("recep", password=PW, role=User.Role.RECEPTIONIST, first_name="Priya")
        self.doc_user = User.objects.create_user("drk", password=PW, role=User.Role.DOCTOR, first_name="Arjun")
        self.doc2_user = User.objects.create_user("drm", password=PW, role=User.Role.DOCTOR, first_name="Meena")
        self.doctor = Doctor.objects.create(user=self.doc_user, slot_minutes=15)
        self.doctor2 = Doctor.objects.create(user=self.doc2_user, slot_minutes=20)
        for wd in range(7):
            DoctorAvailability.objects.create(doctor=self.doctor, weekday=wd, start_time=time(9), end_time=time(10))
            DoctorAvailability.objects.create(doctor=self.doctor2, weekday=wd, start_time=time(9), end_time=time(10))
        self.patient = Patient(first_name="Ravi", gender="M", age_years=40, phone="9840011111", consent_given=True)
        self.patient.save()
        self.day = next_weekday(0)

    def book(self, t, doctor=None, day=None, patient=None):
        a = Appointment(patient=patient or self.patient, doctor=doctor or self.doctor, date=day or self.day, time=t)
        a.full_clean()
        a.save()
        return a


class PatientTests(Base):
    def test_patient_codes_are_sequential(self):
        p2 = Patient(first_name="Lakshmi", gender="F", age_years=30, phone="9840022222", consent_given=True)
        p2.save()
        self.assertEqual(self.patient.patient_code, "P000001")
        self.assertEqual(p2.patient_code, "P000002")

    def test_consent_is_required(self):
        p = Patient(first_name="X", gender="M", age_years=30, phone="9840033333", consent_given=False)
        with self.assertRaises(ValidationError):
            p.full_clean()

    def test_consent_time_recorded(self):
        self.assertIsNotNone(self.patient.consent_at)


class SlotTests(Base):
    def test_slots_follow_schedule(self):
        slots = self.doctor.free_slots(self.day)
        self.assertEqual(slots, [time(9), time(9, 15), time(9, 30), time(9, 45)])
        self.assertEqual(self.doctor2.free_slots(self.day), [time(9), time(9, 20), time(9, 40)])

    def test_booked_slot_disappears_and_cancel_frees_it(self):
        a = self.book(time(9, 15))
        self.assertNotIn(time(9, 15), self.doctor.free_slots(self.day))
        a.move_to(Appointment.Status.CANCELLED)
        self.assertIn(time(9, 15), self.doctor.free_slots(self.day))

    def test_no_slots_on_leave_or_in_past(self):
        DoctorLeave.objects.create(doctor=self.doctor, date=self.day)
        self.assertEqual(self.doctor.free_slots(self.day), [])
        self.assertEqual(self.doctor.free_slots(timezone.localdate() - timedelta(days=1)), [])

    def test_split_shift(self):
        DoctorAvailability.objects.create(doctor=self.doctor, weekday=self.day.weekday(), start_time=time(17), end_time=time(17, 30))
        self.assertEqual(self.doctor.free_slots(self.day)[-2:], [time(17), time(17, 15)])


class BookingTests(Base):
    def test_double_booking_is_rejected(self):
        self.book(time(9))
        other = Patient(first_name="Other", gender="F", age_years=20, phone="9840044444", consent_given=True)
        other.save()
        with self.assertRaises(ValidationError):
            self.book(time(9), patient=other)

    def test_database_blocks_double_booking_even_without_validation(self):
        self.book(time(9))
        dup = Appointment(patient=self.patient, doctor=self.doctor, date=self.day, time=time(9))
        with self.assertRaises(IntegrityError), transaction.atomic():
            dup.save()

    def test_same_time_with_different_doctor_is_fine(self):
        self.book(time(9))
        self.book(time(9), doctor=self.doctor2)

    def test_outside_hours_rejected(self):
        with self.assertRaises(ValidationError):
            self.book(time(11))
        with self.assertRaises(ValidationError):
            self.book(time(9, 5))  # not on a slot boundary

    def test_tokens_per_doctor_per_day(self):
        a1 = self.book(time(9))
        a2 = self.book(time(9, 15))
        b1 = self.book(time(9), doctor=self.doctor2)
        self.assertEqual((a1.token, a2.token, b1.token), (1, 2, 1))

    def test_status_transitions(self):
        a = self.book(time(9))
        with self.assertRaises(ValidationError):
            a.move_to(Appointment.Status.COMPLETED)  # can't skip check-in
        a.move_to(Appointment.Status.CHECKED_IN)
        self.assertIsNotNone(a.checked_in_at)
        a.move_to(Appointment.Status.IN_CONSULTATION)
        a.move_to(Appointment.Status.COMPLETED)
        with self.assertRaises(ValidationError):
            a.move_to(Appointment.Status.CANCELLED)


class ViewTests(Base):
    def login(self, user):
        self.client.login(username=user.username, password=PW)

    def test_login_required(self):
        r = self.client.get(reverse("dashboard"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login/", r.url)

    def test_every_page_renders_for_admin(self):
        self.login(self.admin)
        a = self.book(time(9))
        for name, args in [
            ("dashboard", []), ("patient_list", []), ("patient_create", []), ("patient_detail", [self.patient.pk]),
            ("patient_edit", [self.patient.pk]), ("appointment_list", []), ("appointment_book", []),
            ("appointment_walk_in", []), ("staff_list", []), ("staff_create", []), ("staff_edit", [self.doc_user.pk]),
            ("audit_log", []), ("consultation", [a.pk]),
        ]:
            r = self.client.get(reverse(name, args=args))
            self.assertEqual(r.status_code, 200, name)

    def test_receptionist_cannot_manage_staff_or_see_notes(self):
        self.login(self.recep)
        self.assertEqual(self.client.get(reverse("staff_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("audit_log")).status_code, 403)
        a = self.book(time(9))
        self.assertEqual(self.client.get(reverse("consultation", args=[a.pk])).status_code, 403)
        r = self.client.get(reverse("patient_detail", args=[self.patient.pk]))
        self.assertNotContains(r, "Consultation history")

    def test_receptionist_cannot_start_consultation(self):
        self.login(self.recep)
        a = self.book(time(9))
        a.move_to(Appointment.Status.CHECKED_IN)
        r = self.client.post(reverse("appointment_status", args=[a.pk]), {"status": "in_consultation"})
        self.assertEqual(r.status_code, 403)

    def test_doctor_cannot_open_other_doctors_patient(self):
        a = self.book(time(9), doctor=self.doctor2)
        self.login(self.doc_user)
        self.assertEqual(self.client.get(reverse("consultation", args=[a.pk])).status_code, 403)

    def test_register_patient_then_book_flow(self):
        self.login(self.recep)
        r = self.client.post(reverse("patient_create"), {
            "first_name": "Divya", "gender": "F", "age_years": 24, "phone": "9840066666", "consent_given": "on", "next": "book",
        })
        p = Patient.objects.get(phone="9840066666")
        self.assertRedirects(r, f"/appointments/book/?patient={p.pk}", fetch_redirect_response=False)
        r = self.client.get(reverse("doctor_slots_json"), {"doctor": self.doctor.pk, "date": self.day.isoformat()})
        self.assertEqual([s["value"] for s in r.json()["slots"]], ["09:00", "09:15", "09:30", "09:45"])
        r = self.client.post(reverse("appointment_book"), {
            "patient": p.pk, "doctor": self.doctor.pk, "date": self.day.isoformat(), "time": "09:30", "reason": "Fever",
        })
        self.assertEqual(r.status_code, 302)
        appt = Appointment.objects.get(patient=p)
        self.assertEqual((appt.time, appt.kind, appt.token), (time(9, 30), "booked", 1))
        # Second booking in the same slot through the form fails cleanly.
        r = self.client.post(reverse("appointment_book"), {
            "patient": self.patient.pk, "doctor": self.doctor.pk, "date": self.day.isoformat(), "time": "09:30",
        })
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "not available")
        self.assertTrue(AuditLog.objects.filter(action="create", patient=p).exists())

    def test_patient_without_consent_cannot_register(self):
        self.login(self.recep)
        r = self.client.post(reverse("patient_create"), {"first_name": "No", "gender": "M", "age_years": 30, "phone": "9840077777"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(Patient.objects.filter(phone="9840077777").exists())

    def test_walk_in_goes_to_queue_today(self):
        self.login(self.recep)
        self.client.post(reverse("appointment_walk_in"), {"patient": self.patient.pk, "doctor": self.doctor.pk, "reason": "Cough"})
        a = Appointment.objects.get(kind="walk_in")
        self.assertEqual((a.date, a.status, a.time), (timezone.localdate(), "checked_in", None))

    def test_full_consultation_by_doctor(self):
        a = Appointment(patient=self.patient, doctor=self.doctor, date=timezone.localdate(), kind="walk_in", status="checked_in")
        a.save()
        self.login(self.doc_user)
        r = self.client.post(reverse("appointment_status", args=[a.pk]), {"status": "in_consultation"})
        self.assertRedirects(r, reverse("consultation", args=[a.pk]), fetch_redirect_response=False)
        r = self.client.post(reverse("consultation", args=[a.pk]), {
            "complaints": "Fever 2 days", "diagnosis": "Viral fever", "prescription": "Paracetamol 650 — 1-0-1 — 3 days", "finish": "1",
        })
        a.refresh_from_db()
        self.assertEqual(a.status, "completed")
        self.assertEqual(Consultation.objects.get(appointment=a).diagnosis, "Viral fever")
        r = self.client.get(reverse("prescription_print", args=[a.pk]))
        self.assertContains(r, "Paracetamol 650")

    def test_admin_adds_doctor_with_schedule(self):
        self.login(self.admin)
        data = {
            "u-first_name": "Sameer", "u-last_name": "Iyer", "u-username": "dr.sameer", "u-email": "", "u-phone": "",
            "u-role": "doctor", "u-is_active": "on", "u-password": "Strong#Pass2026",
            "d-specialization": "Dermatologist", "d-registration_number": "TN-1234", "d-consultation_fee": "600", "d-slot_minutes": "30",
            "av-TOTAL_FORMS": "2", "av-INITIAL_FORMS": "0", "av-MIN_NUM_FORMS": "0", "av-MAX_NUM_FORMS": "1000",
            "av-0-weekday": "0", "av-0-start_time": "16:00", "av-0-end_time": "18:00",
            "av-1-weekday": "", "av-1-start_time": "", "av-1-end_time": "",
        }
        r = self.client.post(reverse("staff_create"), data)
        self.assertEqual(r.status_code, 302, getattr(r, "context", None) and r.context["form"].errors)
        doc = Doctor.objects.get(user__username="dr.sameer")
        self.assertEqual(doc.free_slots(next_weekday(0)), [time(16), time(16, 30), time(17), time(17, 30)])
        self.assertTrue(self.client.login(username="dr.sameer", password="Strong#Pass2026"))

    def test_admin_cannot_disable_self(self):
        self.login(self.admin)
        r = self.client.post(reverse("staff_edit", args=[self.admin.pk]), {
            "u-first_name": "Owner", "u-username": "admin", "u-role": "admin",
            "av-TOTAL_FORMS": "0", "av-INITIAL_FORMS": "0",
        })
        self.assertEqual(r.status_code, 200)
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_viewing_patient_is_audited(self):
        self.login(self.recep)
        self.client.get(reverse("patient_detail", args=[self.patient.pk]))
        self.assertTrue(AuditLog.objects.filter(action="view", patient=self.patient, user=self.recep).exists())
        self.assertTrue(AuditLog.objects.filter(action="login", user=self.recep).exists())
