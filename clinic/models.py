from datetime import date, datetime, timedelta

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from decimal import Decimal

from django.db import IntegrityError, models, transaction
from django.db.models import Max, Q, Sum
from django.db.models.functions import Coalesce
from django.utils import timezone


phone_validator = RegexValidator(r"^\+?[0-9]{10,13}$", "Enter a valid phone number (10–13 digits).")


def save_with_next_number(assign, save, attempts=5):
    """Save `obj` after `assign()` picks the next sequential number.

    Two people saving at the same moment can pick the same number; the database's unique
    constraint rejects the second one, and we simply try again with a fresh number.
    """
    for attempt in range(attempts):
        try:
            with transaction.atomic():
                assign()
                save()
                return
        except IntegrityError:
            if attempt == attempts - 1:
                raise


# ---------------------------------------------------------------- dental chart (FDI two-digit numbering)

PERMANENT_TEETH = {
    "upper": ([str(n) for n in range(18, 10, -1)], [str(n) for n in range(21, 29)]),
    "lower": ([str(n) for n in range(48, 40, -1)], [str(n) for n in range(31, 39)]),
}
PRIMARY_TEETH = {
    "upper": ([str(n) for n in range(55, 50, -1)], [str(n) for n in range(61, 66)]),
    "lower": ([str(n) for n in range(85, 80, -1)], [str(n) for n in range(71, 76)]),
}
ALL_TEETH = {t for layout in (PERMANENT_TEETH, PRIMARY_TEETH) for halves in layout.values() for half in halves for t in half}

# code, label, short mark shown on the chart, kind (problem / treated / missing) used for colour
TOOTH_CONDITIONS = [
    ("caries", "Decay / caries", "C", "problem"),
    ("fractured", "Fractured", "Fr", "problem"),
    ("rootstump", "Root stump", "RS", "problem"),
    ("mobile", "Mobile", "Mo", "problem"),
    ("impacted", "Impacted", "Im", "problem"),
    ("sensitive", "Sensitive", "S", "problem"),
    ("extract", "To be extracted", "X", "problem"),
    ("filled", "Filled", "F", "treated"),
    ("rct", "Root canal treated", "RCT", "treated"),
    ("crown", "Crown / bridge", "Cr", "treated"),
    ("implant", "Implant", "Imp", "treated"),
    ("missing", "Missing", "M", "missing"),
]
CONDITION_INFO = {code: {"code": code, "label": label, "abbr": abbr, "kind": kind} for code, label, abbr, kind in TOOTH_CONDITIONS}


def clean_dental_chart(chart):
    """Validate a {tooth: [conditions]} dict; raises ValidationError, returns a tidy copy."""
    if chart in (None, ""):
        return {}
    if not isinstance(chart, dict):
        raise ValidationError("Dental chart data is malformed.")
    tidy = {}
    for tooth, conds in chart.items():
        tooth = str(tooth)
        if tooth not in ALL_TEETH:
            raise ValidationError(f"Unknown tooth number {tooth}.")
        if not isinstance(conds, list) or any(c not in CONDITION_INFO for c in conds):
            raise ValidationError(f"Unknown condition on tooth {tooth}.")
        conds = [c for c in CONDITION_INFO if c in conds]  # de-duplicate, fixed order
        if conds:
            tidy[tooth] = conds
    return dict(sorted(tidy.items()))


def dental_chart_layout(chart):
    """Rows of teeth for the odontogram template, each tooth with its conditions."""
    chart = chart or {}
    kind_rank = {"problem": 0, "treated": 1, "missing": 2}

    def tooth(code):
        conds = [CONDITION_INFO[c] for c in chart.get(code, []) if c in CONDITION_INFO]
        kind = min((c["kind"] for c in conds), key=kind_rank.get, default="")
        return {"code": code, "conditions": conds, "kind": kind}

    def rows(layout):
        return [{"jaw": jaw, "right": [tooth(t) for t in halves[0]], "left": [tooth(t) for t in halves[1]]}
                for jaw, halves in layout.items()]

    primary_codes = {t for halves in PRIMARY_TEETH.values() for half in halves for t in half}
    return {
        "permanent": rows(PERMANENT_TEETH),
        "primary": rows(PRIMARY_TEETH),
        "has_primary": any(t in primary_codes for t in chart),
        "summary": [tooth(t) for t in sorted(chart, key=int) if chart[t]],
    }


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

    @property
    def can_see_collections(self):
        """The daily collection report: the owner and the front desk who close the cash each day."""
        return self.is_clinic_admin or self.role == self.Role.RECEPTIONIST

    def __str__(self):
        return self.get_full_name() or self.username


class Doctor(models.Model):
    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="doctor_profile")
    specialization = models.CharField(max_length=100, default="Dental Surgeon")
    qualification = models.CharField(max_length=100, blank=True, help_text="Printed on prescriptions, e.g. BDS, MDS (Endodontics)")
    registration_number = models.CharField("Dental Council reg. no.", max_length=50, blank=True)
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
    allergies = models.TextField(blank=True, help_text="Drug allergies (e.g. penicillin, local anaesthetic, latex) or food allergies")

    # Medical conditions a dentist must know before anaesthesia, extractions or surgery.
    has_diabetes = models.BooleanField("Diabetes", default=False)
    has_hypertension = models.BooleanField("High blood pressure", default=False)
    has_heart_condition = models.BooleanField("Heart disease / stent / pacemaker", default=False)
    on_blood_thinners = models.BooleanField("Takes blood thinners (aspirin, clopidogrel, warfarin)", default=False)
    has_bleeding_disorder = models.BooleanField("Bleeding disorder", default=False)
    has_asthma = models.BooleanField("Asthma / breathing problems", default=False)
    has_epilepsy = models.BooleanField("Epilepsy / fits", default=False)
    has_thyroid_disorder = models.BooleanField("Thyroid disorder", default=False)
    has_infectious_disease = models.BooleanField("Hepatitis B/C, HIV or TB", default=False)
    takes_bisphosphonates = models.BooleanField("Osteoporosis medicine (bisphosphonates)", default=False)
    is_pregnant = models.BooleanField("Pregnant or breastfeeding", default=False)
    current_medications = models.TextField(blank=True, help_text="Medicines the patient takes regularly")
    medical_history = models.TextField("Other medical history", blank=True, help_text="Other conditions, past surgeries, hospital admissions")

    class Tobacco(models.TextChoices):
        NONE = "", "None"
        SMOKING = "smoking", "Smokes"
        CHEWING = "chewing", "Chews tobacco / gutka / paan"
        BOTH = "both", "Smokes and chews"

    tobacco_use = models.CharField(max_length=10, choices=Tobacco.choices, blank=True)
    dental_history = models.TextField(
        "Past dental treatment", blank=True,
        help_text="Previous fillings, root canals, extractions, braces or dentures, and when they last saw a dentist",
    )
    referred_by = models.CharField("Referred by / heard about us from", max_length=100, blank=True)
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

    ALERT_FIELDS = [
        "has_diabetes", "has_hypertension", "has_heart_condition", "on_blood_thinners", "has_bleeding_disorder",
        "has_asthma", "has_epilepsy", "has_thyroid_disorder", "has_infectious_disease", "takes_bisphosphonates",
        "is_pregnant",
    ]

    @property
    def medical_alerts(self):
        """Labels of the ticked medical conditions, shown as a warning before treatment."""
        return [self._meta.get_field(f).verbose_name for f in self.ALERT_FIELDS if getattr(self, f)]

    @property
    def latest_dental_chart(self):
        record = (
            Consultation.objects.filter(appointment__patient=self).exclude(dental_chart={})
            .order_by("-appointment__date", "-pk").first()
        )
        return record.dental_chart if record else {}

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
        if self.patient_code:
            return super().save(*args, **kwargs)

        def assign():
            # Number from the highest existing code (not the row id): PostgreSQL skips ids after
            # failed inserts, which would leave gaps in patient IDs.
            last = Patient.objects.aggregate(m=Max("patient_code"))["m"]
            self.patient_code = f"P{(int(last[1:]) if last else 0) + 1:06d}"

        save_with_next_number(assign, lambda: super(Patient, self).save(*args, **kwargs))


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

    @property
    def active_invoice(self):
        return self.invoices.filter(is_cancelled=False).first()

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
    class OralHygiene(models.TextChoices):
        NOT_RECORDED = "", "Not recorded"
        GOOD = "good", "Good"
        FAIR = "fair", "Fair"
        POOR = "poor", "Poor"

    # Vitals before treatment (often entered by the dental assistant)
    bp_systolic = models.PositiveSmallIntegerField("BP systolic", null=True, blank=True)
    bp_diastolic = models.PositiveSmallIntegerField("BP diastolic", null=True, blank=True)
    pulse = models.PositiveSmallIntegerField("Pulse (bpm)", null=True, blank=True)
    blood_sugar = models.PositiveSmallIntegerField("Blood sugar, random (mg/dL)", null=True, blank=True)
    weight_kg = models.DecimalField("Weight (kg)", max_digits=5, decimal_places=1, null=True, blank=True)

    complaints = models.TextField("Chief complaint", blank=True)
    extraoral_findings = models.TextField(
        "Extra-oral findings", blank=True, help_text="Face, swelling, lymph nodes, TMJ, mouth opening")
    intraoral_findings = models.TextField(
        "Intra-oral findings", blank=True, help_text="Gums, soft tissues, occlusion, calculus, stains")
    oral_hygiene = models.CharField(max_length=4, choices=OralHygiene.choices, blank=True)
    investigations = models.TextField("X-rays & investigations", blank=True, help_text="e.g. IOPA 36: periapical radiolucency; OPG")
    dental_chart = models.JSONField(default=dict, blank=True, help_text="{tooth number: [conditions]}")
    diagnosis = models.TextField(blank=True)
    prescription = models.TextField(blank=True, help_text="One medicine per line, e.g. Amoxicillin 500mg — 1-1-1 — 5 days")
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

    def clean(self):
        self.dental_chart = clean_dental_chart(self.dental_chart)


# ---------------------------------------------------------------- treatments and billing

class Procedure(models.Model):
    """The clinic's price list: every treatment it offers, with the usual fee."""

    class Category(models.TextChoices):
        DIAGNOSTIC = "diagnostic", "Consultation & X-rays"
        PREVENTIVE = "preventive", "Cleaning & prevention"
        RESTORATIVE = "restorative", "Fillings"
        ENDODONTIC = "endodontic", "Root canal"
        PERIODONTIC = "periodontic", "Gum treatment"
        PROSTHODONTIC = "prosthodontic", "Crowns, bridges & dentures"
        SURGERY = "surgery", "Extractions & surgery"
        IMPLANT = "implant", "Implants"
        ORTHODONTIC = "orthodontic", "Braces & aligners"
        COSMETIC = "cosmetic", "Cosmetic"
        PEDIATRIC = "pediatric", "Children's dentistry"
        OTHER = "other", "Other"

    name = models.CharField(max_length=120, unique=True)
    category = models.CharField(max_length=20, choices=Category.choices, default=Category.OTHER)
    default_price = models.DecimalField("Fee (₹)", max_digits=10, decimal_places=2)
    is_active = models.BooleanField("In use", default=True)

    class Meta:
        ordering = ["category", "name"]

    def __str__(self):
        return self.name


def clean_teeth(value):
    """'36, 37' / '36 37' → '36, 37'. Empty is fine (whole mouth, e.g. scaling)."""
    teeth = [t for t in value.replace(",", " ").split() if t]
    bad = [t for t in teeth if t not in ALL_TEETH]
    if bad:
        raise ValidationError(f"Not a valid tooth number: {', '.join(bad)}. Use FDI numbers like 11–48 or 51–85.")
    return ", ".join(dict.fromkeys(teeth))


class TreatmentItem(models.Model):
    """One procedure in a patient's treatment plan: planned, then done (and then billed)."""

    class Status(models.TextChoices):
        PLANNED = "planned", "Planned"
        COMPLETED = "completed", "Done"
        CANCELLED = "cancelled", "Cancelled"

    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="treatments")
    procedure = models.ForeignKey(Procedure, on_delete=models.PROTECT, related_name="+")
    teeth = models.CharField(max_length=60, blank=True, help_text="FDI tooth numbers, e.g. 36 or 11, 21")
    notes = models.CharField(max_length=255, blank=True)
    price = models.DecimalField("Fee (₹)", max_digits=10, decimal_places=2)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PLANNED)
    planned_at = models.ForeignKey(Appointment, on_delete=models.SET_NULL, null=True, blank=True, related_name="planned_treatments")
    done_at = models.ForeignKey(Appointment, on_delete=models.SET_NULL, null=True, blank=True, related_name="treatments_done")
    doctor = models.ForeignKey(Doctor, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    completed_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["status", "created_at"]

    def __str__(self):
        return self.description

    @property
    def description(self):
        return f"{self.procedure.name} — tooth {self.teeth}" if self.teeth else self.procedure.name

    def clean(self):
        try:
            self.teeth = clean_teeth(self.teeth or "")
        except ValidationError as e:
            raise ValidationError({"teeth": e.messages})
        if self.price is not None and self.price < 0:
            raise ValidationError({"price": "Fee can't be negative."})

    @property
    def is_billed(self):
        return self.invoice_items.filter(invoice__is_cancelled=False).exists()

    def mark_done(self, appointment):
        if self.status != self.Status.PLANNED:
            raise ValidationError("Only a planned procedure can be marked done.")
        self.status = self.Status.COMPLETED
        self.done_at = appointment
        self.doctor = appointment.doctor
        self.completed_on = timezone.localdate()
        self.save()

    @classmethod
    def unbilled_for(cls, patient):
        return (
            cls.objects.filter(patient=patient, status=cls.Status.COMPLETED)
            .exclude(invoice_items__invoice__is_cancelled=False)
            .select_related("procedure").order_by("completed_on", "pk")
        )


def financial_year(day):
    """Indian financial year label, April to March: 2026-11-27 → '2026-27'."""
    start = day.year if day.month >= 4 else day.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


class InvoiceQuerySet(models.QuerySet):
    def with_paid(self):
        return self.annotate(
            paid_amount=Coalesce(Sum("payments__amount", filter=Q(payments__is_void=False)), Decimal("0"),
                                 output_field=models.DecimalField(max_digits=12, decimal_places=2)),
        )

    def with_balance_due(self):
        return self.with_paid().filter(is_cancelled=False, total__gt=models.F("paid_amount"))


class Invoice(models.Model):
    """A bill. Its lines are fixed once issued; to correct one, cancel it and issue a new bill."""

    fy = models.CharField("Financial year", max_length=7, editable=False)
    seq = models.PositiveIntegerField(editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT, related_name="invoices")
    appointment = models.ForeignKey(Appointment, on_delete=models.SET_NULL, null=True, blank=True, related_name="invoices")
    date = models.DateField(default=timezone.localdate)
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    notes = models.CharField(max_length=255, blank=True)

    is_cancelled = models.BooleanField(default=False)
    cancel_reason = models.CharField(max_length=255, blank=True)
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    objects = InvoiceQuerySet.as_manager()

    class Meta:
        ordering = ["-date", "-seq"]
        constraints = [models.UniqueConstraint(fields=["fy", "seq"], name="unique_invoice_number")]

    def __str__(self):
        return f"Bill {self.number}"

    @property
    def number(self):
        return f"{self.fy}/{self.seq:04d}"

    def save(self, *args, **kwargs):
        if self.seq:
            return super().save(*args, **kwargs)

        def assign():
            self.fy = financial_year(self.date)
            last = Invoice.objects.filter(fy=self.fy).aggregate(m=Max("seq"))["m"] or 0
            self.seq = last + 1

        save_with_next_number(assign, lambda: super(Invoice, self).save(*args, **kwargs))

    def recalculate(self):
        self.subtotal = sum((i.amount for i in self.items.all()), Decimal("0"))
        self.total = self.subtotal - self.discount
        self.save(update_fields=["subtotal", "total"])

    @property
    def paid(self):
        if hasattr(self, "paid_amount"):
            return self.paid_amount
        return self.payments.filter(is_void=False).aggregate(s=Sum("amount"))["s"] or Decimal("0")

    @property
    def balance(self):
        return Decimal("0") if self.is_cancelled else self.total - self.paid

    @property
    def status(self):
        if self.is_cancelled:
            return "cancelled"
        paid = self.paid
        if paid >= self.total:
            return "paid"
        return "partial" if paid > 0 else "unpaid"

    STATUS_LABELS = {"cancelled": "Cancelled", "paid": "Paid", "partial": "Part paid", "unpaid": "Unpaid"}

    @property
    def status_label(self):
        return self.STATUS_LABELS[self.status]

    def cancel(self, user, reason):
        if self.is_cancelled:
            raise ValidationError("This bill is already cancelled.")
        if self.payments.filter(is_void=False).exists():
            raise ValidationError("Void the payments on this bill before cancelling it.")
        self.is_cancelled = True
        self.cancel_reason = reason
        self.cancelled_at = timezone.now()
        self.cancelled_by = user
        self.save()


class InvoiceItem(models.Model):
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name="items")
    description = models.CharField(max_length=200)
    quantity = models.PositiveSmallIntegerField(default=1)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    treatment = models.ForeignKey(TreatmentItem, on_delete=models.PROTECT, null=True, blank=True, related_name="invoice_items")

    class Meta:
        ordering = ["pk"]

    def __str__(self):
        return self.description

    @property
    def amount(self):
        return self.unit_price * self.quantity


class Payment(models.Model):
    """Money received against a bill. Never edited or deleted; a mistake is voided with a reason."""

    class Mode(models.TextChoices):
        CASH = "cash", "Cash"
        UPI = "upi", "UPI"
        CARD = "card", "Card"
        BANK = "bank", "Bank transfer"
        OTHER = "other", "Other"

    invoice = models.ForeignKey(Invoice, on_delete=models.PROTECT, related_name="payments")
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    mode = models.CharField(max_length=10, choices=Mode.choices, default=Mode.CASH)
    reference = models.CharField(max_length=100, blank=True, help_text="UPI / card transaction ID")
    received_at = models.DateTimeField(default=timezone.now, db_index=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    is_void = models.BooleanField(default=False)
    void_reason = models.CharField(max_length=255, blank=True)
    voided_at = models.DateTimeField(null=True, blank=True)
    voided_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")

    class Meta:
        ordering = ["received_at"]

    def __str__(self):
        return f"₹{self.amount} {self.get_mode_display()} for {self.invoice}"

    def void(self, user, reason):
        if self.is_void:
            raise ValidationError("This payment is already void.")
        self.is_void = True
        self.void_reason = reason
        self.voided_at = timezone.now()
        self.voided_by = user
        self.save()


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
