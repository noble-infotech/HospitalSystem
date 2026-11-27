from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone


phone_validator = RegexValidator(r"^\+?[0-9]{10,13}$", "Enter a valid phone number (10–13 digits).")


class User(AbstractUser):
    """A staff login. Every person who uses the system has one."""

    class Role(models.TextChoices):
        ADMIN = "admin", "Admin / Owner"
        DOCTOR = "doctor", "Doctor"
        RECEPTIONIST = "receptionist", "Receptionist"
        NURSE = "nurse", "Nurse / Assistant"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.RECEPTIONIST)
    phone = models.CharField(max_length=15, blank=True, validators=[phone_validator])

    @property
    def is_clinic_admin(self):
        return self.is_superuser or self.role == self.Role.ADMIN

    @property
    def is_doctor(self):
        return self.role == self.Role.DOCTOR

    @property
    def can_see_clinical(self):
        """Doctors, nurses and admins can see consultation notes; receptionists cannot."""
        return self.is_clinic_admin or self.role in (self.Role.DOCTOR, self.Role.NURSE)

    def __str__(self):
        return self.get_full_name() or self.username


class Doctor(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="doctor_profile")
    specialization = models.CharField(max_length=100, default="General Physician")
    registration_number = models.CharField("Medical registration no.", max_length=50, blank=True)
    consultation_fee = models.DecimalField(max_digits=8, decimal_places=2, default=0)
    slot_minutes = models.PositiveSmallIntegerField("Slot length (minutes)", default=15)
    is_active = models.BooleanField(default=True)

    class Meta:
        ordering = ["user__first_name"]

    def __str__(self):
        name = self.user.get_full_name() or self.user.username
        return f"Dr. {name}"

    def schedule_for(self, day):
        return self.availability.filter(weekday=day.weekday()).order_by("start_time")

    def free_slots(self, day, exclude_appointment=None):
        """All bookable start times on `day` that are inside working hours and not taken."""
        taken = Appointment.objects.filter(doctor=self, date=day).exclude(status=Appointment.Status.CANCELLED)
        if exclude_appointment is not None:
            taken = taken.exclude(pk=exclude_appointment.pk)
        taken_times = set(taken.exclude(time__isnull=True).values_list("time", flat=True))
        now = timezone.localtime()
        if day < now.date() or DoctorLeave.objects.filter(doctor=self, date=day).exists():
            return []

        slots = []
        step = timedelta(minutes=self.slot_minutes)
        for block in self.schedule_for(day):
            current = datetime.combine(day, block.start_time)
            end = datetime.combine(day, block.end_time)
            while current + step <= end:
                t = current.time()
                in_past = day == now.date() and t <= now.time()
                if t not in taken_times and not in_past:
                    slots.append(t)
                current += step
        return slots


class DoctorAvailability(models.Model):
    """A weekly working block, e.g. Monday 09:00–13:00. A doctor can have several per day."""

    class Weekday(models.IntegerChoices):
        MONDAY = 0
        TUESDAY = 1
        WEDNESDAY = 2
        THURSDAY = 3
        FRIDAY = 4
        SATURDAY = 5
        SUNDAY = 6

    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name="availability")
    weekday = models.IntegerField(choices=Weekday.choices)
    start_time = models.TimeField()
    end_time = models.TimeField()

    class Meta:
        ordering = ["doctor", "weekday", "start_time"]
        verbose_name_plural = "Doctor availability"

    def clean(self):
        if self.start_time and self.end_time and self.start_time >= self.end_time:
            raise ValidationError("End time must be after start time.")

    def __str__(self):
        return f"{self.doctor} · {self.get_weekday_display()} {self.start_time:%H:%M}–{self.end_time:%H:%M}"


class DoctorLeave(models.Model):
    """A day the doctor is not available (holiday, conference, sick)."""

    doctor = models.ForeignKey(Doctor, on_delete=models.CASCADE, related_name="leaves")
    date = models.DateField()
    reason = models.CharField(max_length=200, blank=True)

    class Meta:
        unique_together = ("doctor", "date")
        ordering = ["date"]

    def __str__(self):
        return f"{self.doctor} off on {self.date}"


class Patient(models.Model):
    class Gender(models.TextChoices):
        MALE = "M", "Male"
        FEMALE = "F", "Female"
        OTHER = "O", "Other"

    class BloodGroup(models.TextChoices):
        UNKNOWN = "", "Unknown"
        A_POS = "A+", "A+"
        A_NEG = "A-", "A−"
        B_POS = "B+", "B+"
        B_NEG = "B-", "B−"
        AB_POS = "AB+", "AB+"
        AB_NEG = "AB-", "AB−"
        O_POS = "O+", "O+"
        O_NEG = "O-", "O−"

    patient_code = models.CharField("Patient ID", max_length=12, unique=True, editable=False)
    first_name = models.CharField(max_length=60)
    last_name = models.CharField(max_length=60, blank=True)
    gender = models.CharField(max_length=1, choices=Gender.choices)
    date_of_birth = models.DateField(null=True, blank=True)
    age_years = models.PositiveSmallIntegerField("Age (if DOB unknown)", null=True, blank=True)
    phone = models.CharField(max_length=15, validators=[phone_validator], db_index=True)
    email = models.EmailField(blank=True)
    address = models.TextField(blank=True)
    blood_group = models.CharField(max_length=3, choices=BloodGroup.choices, blank=True)
    allergies = models.TextField(blank=True, help_text="Drug or food allergies")
    medical_history = models.TextField(blank=True, help_text="Chronic conditions, past surgeries, ongoing medication")
    emergency_contact_name = models.CharField(max_length=100, blank=True)
    emergency_contact_phone = models.CharField(max_length=15, blank=True, validators=[phone_validator])
    abha_number = models.CharField("ABHA number", max_length=20, blank=True)

    consent_given = models.BooleanField(
        "Patient consented to storing their data",
        default=False,
        help_text="Required. Explain to the patient how their data will be used before ticking.",
    )
    consent_at = models.DateTimeField(null=True, blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.full_name} ({self.patient_code})"

    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    @property
    def age(self):
        if self.date_of_birth:
            today = timezone.localdate()
            dob = self.date_of_birth
            return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
        return self.age_years

    def clean(self):
        if not self.consent_given:
            raise ValidationError({"consent_given": "Patient consent is required to register."})
        if self.date_of_birth and self.date_of_birth > timezone.localdate():
            raise ValidationError({"date_of_birth": "Date of birth cannot be in the future."})

    def save(self, *args, **kwargs):
        if self.consent_given and not self.consent_at:
            self.consent_at = timezone.now()
        if not self.patient_code:
            with transaction.atomic():
                last = Patient.objects.select_for_update().aggregate(m=Max("id"))["m"] or 0
                self.patient_code = f"P{last + 1:06d}"
                super().save(*args, **kwargs)
                return
        super().save(*args, **kwargs)


class Appointment(models.Model):
    class Status(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        CHECKED_IN = "checked_in", "Waiting"
        IN_CONSULTATION = "in_consultation", "With doctor"
        COMPLETED = "completed", "Completed"
        CANCELLED = "cancelled", "Cancelled"
        NO_SHOW = "no_show", "No-show"

    class Kind(models.TextChoices):
        BOOKED = "booked", "Booked"
        WALK_IN = "walk_in", "Walk-in"

    # Which status moves are allowed, so the queue can't get into a nonsense state.
    TRANSITIONS = {
        Status.SCHEDULED: {Status.CHECKED_IN, Status.CANCELLED, Status.NO_SHOW},
        Status.CHECKED_IN: {Status.IN_CONSULTATION, Status.CANCELLED, Status.NO_SHOW},
        Status.IN_CONSULTATION: {Status.COMPLETED},
        Status.COMPLETED: set(),
        Status.CANCELLED: set(),
        Status.NO_SHOW: set(),
    }

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="appointments")
    doctor = models.ForeignKey(Doctor, on_delete=models.PROTECT, related_name="appointments")
    date = models.DateField(db_index=True)
    time = models.TimeField(null=True, blank=True, help_text="Empty for walk-ins")
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.BOOKED)
    token = models.PositiveIntegerField(editable=False)
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.SCHEDULED)
    reason = models.CharField("Reason for visit", max_length=255, blank=True)
    notes = models.CharField("Front-desk notes", max_length=255, blank=True)

    checked_in_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["date", "token"]
        constraints = [
            # Database-level guarantee against double booking the same doctor slot.
            models.UniqueConstraint(
                fields=["doctor", "date", "time"],
                condition=~models.Q(status="cancelled") & models.Q(time__isnull=False),
                name="unique_active_doctor_slot",
            ),
            models.UniqueConstraint(fields=["doctor", "date", "token"], name="unique_token_per_doctor_day"),
        ]

    def __str__(self):
        when = f"{self.time:%H:%M}" if self.time else "walk-in"
        return f"#{self.token} {self.patient.full_name} with {self.doctor} on {self.date} {when}"

    def clean(self):
        if self.kind == self.Kind.BOOKED and not self.time:
            raise ValidationError({"time": "Pick a time slot for a booked appointment."})
        if not (self.doctor_id and self.date):
            return
        if DoctorLeave.objects.filter(doctor_id=self.doctor_id, date=self.date).exists():
            raise ValidationError({"date": f"{self.doctor} is on leave that day."})
        if self.kind == self.Kind.BOOKED and self.status == self.Status.SCHEDULED:
            free = self.doctor.free_slots(self.date, exclude_appointment=self if self.pk else None)
            if self.time not in free:
                raise ValidationError({"time": "That slot is not available. Pick another time."})

    def save(self, *args, **kwargs):
        if not self.token:
            with transaction.atomic():
                last = (
                    Appointment.objects.select_for_update()
                    .filter(doctor_id=self.doctor_id, date=self.date)
                    .aggregate(m=Max("token"))["m"]
                    or 0
                )
                self.token = last + 1
                super().save(*args, **kwargs)
                return
        super().save(*args, **kwargs)

    def can_move_to(self, new_status):
        return new_status in self.TRANSITIONS.get(self.status, set())

    def move_to(self, new_status):
        if not self.can_move_to(new_status):
            raise ValidationError(f"Cannot change from {self.get_status_display()} to {new_status}.")
        self.status = new_status
        if new_status == self.Status.CHECKED_IN:
            self.checked_in_at = timezone.now()
        if new_status == self.Status.COMPLETED:
            self.completed_at = timezone.now()
        self.save()


class Consultation(models.Model):
    """What the doctor records during the visit. Visible to doctors, nurses and admins only."""

    appointment = models.OneToOneField(Appointment, on_delete=models.PROTECT, related_name="consultation")
    # Vitals (often entered by a nurse before the doctor sees the patient)
    bp_systolic = models.PositiveSmallIntegerField("BP systolic", null=True, blank=True)
    bp_diastolic = models.PositiveSmallIntegerField("BP diastolic", null=True, blank=True)
    pulse = models.PositiveSmallIntegerField("Pulse (bpm)", null=True, blank=True)
    temperature = models.DecimalField("Temp (°F)", max_digits=4, decimal_places=1, null=True, blank=True)
    weight_kg = models.DecimalField("Weight (kg)", max_digits=5, decimal_places=1, null=True, blank=True)
    spo2 = models.PositiveSmallIntegerField("SpO₂ (%)", null=True, blank=True)

    complaints = models.TextField("Chief complaints", blank=True)
    diagnosis = models.TextField(blank=True)
    prescription = models.TextField(blank=True, help_text="One medicine per line, e.g. Paracetamol 650mg — 1-0-1 — 3 days")
    advice = models.TextField(blank=True)
    follow_up_date = models.DateField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    def __str__(self):
        return f"Consultation for {self.appointment}"

    @property
    def prescription_lines(self):
        return [line for line in self.prescription.splitlines() if line.strip()]


class AuditLog(models.Model):
    """Who looked at or changed which patient record, and when. Never edited, only appended."""

    class Action(models.TextChoices):
        VIEW = "view", "Viewed"
        CREATE = "create", "Created"
        UPDATE = "update", "Updated"
        STATUS = "status", "Status change"
        LOGIN = "login", "Logged in"
        LOGOUT = "logout", "Logged out"
        LOGIN_FAILED = "login_failed", "Failed login"

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    action = models.CharField(max_length=20, choices=Action.choices)
    patient = models.ForeignKey(Patient, on_delete=models.SET_NULL, null=True, blank=True)
    description = models.CharField(max_length=255)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-timestamp"]

    def __str__(self):
        return f"{self.timestamp:%Y-%m-%d %H:%M} {self.user} {self.get_action_display()}: {self.description}"
