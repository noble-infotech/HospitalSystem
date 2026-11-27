from datetime import date

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import Appointment, Consultation, Doctor, DoctorAvailability, DoctorLeave, Patient, User


class DateInput(forms.DateInput):
    input_type = "date"

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%Y-%m-%d")
        super().__init__(**kwargs)


class TimeInput(forms.TimeInput):
    input_type = "time"

    def __init__(self, **kwargs):
        kwargs.setdefault("format", "%H:%M")
        super().__init__(**kwargs)


class PatientForm(forms.ModelForm):
    class Meta:
        model = Patient
        fields = [
            "first_name", "last_name", "gender", "date_of_birth", "age_years", "phone", "email",
            "blood_group", "address", "allergies", "medical_history",
            "emergency_contact_name", "emergency_contact_phone", "abha_number", "consent_given",
        ]
        widgets = {
            "date_of_birth": DateInput(),
            "address": forms.Textarea(attrs={"rows": 2}),
            "allergies": forms.Textarea(attrs={"rows": 2}),
            "medical_history": forms.Textarea(attrs={"rows": 3}),
        }

    def clean(self):
        data = super().clean()
        if not data.get("date_of_birth") and data.get("age_years") is None:
            self.add_error("age_years", "Enter either the date of birth or the age.")
        return data


class AppointmentForm(forms.ModelForm):
    """Booking a time slot. The time options are loaded from the server for the chosen doctor/date."""

    patient = forms.ModelChoiceField(queryset=Patient.objects.all(), widget=forms.HiddenInput)
    time = forms.TimeField(widget=forms.Select(choices=[("", "Pick doctor and date first")]))

    class Meta:
        model = Appointment
        fields = ["patient", "doctor", "date", "time", "reason", "notes"]
        widgets = {"date": DateInput()}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doctor"].queryset = Doctor.objects.filter(is_active=True, user__is_active=True).select_related("user")
        self.fields["date"].widget.attrs["min"] = timezone.localdate().isoformat()
        # Accept whatever slot the browser submits; the model's clean() checks it is really free.
        self.fields["time"].widget.choices = [("", "Pick doctor and date first")]

    def clean_date(self):
        d = self.cleaned_data["date"]
        if d < timezone.localdate():
            raise forms.ValidationError("Appointments can't be booked in the past.")
        return d

    def _post_clean(self):
        self.instance.kind = Appointment.Kind.BOOKED
        super()._post_clean()


class WalkInForm(forms.ModelForm):
    patient = forms.ModelChoiceField(queryset=Patient.objects.all(), widget=forms.HiddenInput)

    class Meta:
        model = Appointment
        fields = ["patient", "doctor", "reason"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["doctor"].queryset = Doctor.objects.filter(is_active=True, user__is_active=True).select_related("user")

    def _post_clean(self):
        self.instance.kind = Appointment.Kind.WALK_IN
        self.instance.date = timezone.localdate()
        self.instance.time = None
        self.instance.status = Appointment.Status.CHECKED_IN
        super()._post_clean()


class ConsultationForm(forms.ModelForm):
    class Meta:
        model = Consultation
        exclude = ["appointment", "created_at", "updated_at", "updated_by"]
        widgets = {
            "complaints": forms.Textarea(attrs={"rows": 3}),
            "diagnosis": forms.Textarea(attrs={"rows": 2}),
            "prescription": forms.Textarea(attrs={"rows": 5, "placeholder": "Paracetamol 650mg — 1-0-1 — 3 days"}),
            "advice": forms.Textarea(attrs={"rows": 2}),
            "follow_up_date": DateInput(),
        }


class StaffForm(forms.ModelForm):
    password = forms.CharField(
        widget=forms.PasswordInput, required=False,
        help_text="Required for new staff. Leave blank when editing to keep the current password.",
    )

    class Meta:
        model = User
        fields = ["first_name", "last_name", "username", "email", "phone", "role", "is_active"]
        labels = {"is_active": "Can log in"}
        help_texts = {"is_active": "Untick when someone leaves the clinic. Their past records stay intact.", "username": "Used to log in. Letters, numbers and . _ - only."}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["first_name"].required = True
        if not self.instance.pk:
            self.fields["password"].required = True

    def clean_password(self):
        pw = self.cleaned_data.get("password")
        if pw:
            validate_password(pw, self.instance)
        return pw

    def save(self, commit=True):
        user = super().save(commit=False)
        if self.cleaned_data.get("password"):
            user.set_password(self.cleaned_data["password"])
        # Only admins may use the built-in Django admin site.
        user.is_staff = user.role == User.Role.ADMIN
        if commit:
            user.save()
        return user


class DoctorForm(forms.ModelForm):
    class Meta:
        model = Doctor
        fields = ["specialization", "registration_number", "consultation_fee", "slot_minutes"]


AvailabilityFormSet = inlineformset_factory(
    Doctor, DoctorAvailability,
    fields=["weekday", "start_time", "end_time"],
    widgets={"start_time": TimeInput(), "end_time": TimeInput()},
    extra=2, can_delete=True,
)


class LeaveForm(forms.ModelForm):
    class Meta:
        model = DoctorLeave
        fields = ["date", "reason"]
        widgets = {"date": DateInput(), "reason": forms.TextInput(attrs={"placeholder": "Reason (optional)"})}
