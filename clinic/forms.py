from decimal import Decimal

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.forms import inlineformset_factory
from django.utils import timezone

from .models import (
    Appointment, Consultation, Doctor, DoctorAvailability, DoctorLeave, Invoice, Patient, Payment, Procedure,
    TreatmentItem, User, clean_dental_chart,
)

# Common reasons for a dental visit, offered as suggestions in the booking forms.
VISIT_REASONS = [
    "Toothache", "Check-up", "Cleaning / scaling", "Filling", "Root canal sitting", "Extraction", "Crown / bridge",
    "Braces review", "Denture", "Bleeding gums", "Sensitivity", "Broken tooth", "Follow-up", "Implant review",
]
REASON_WIDGET = forms.TextInput(attrs={"list": "visit-reasons", "autocomplete": "off"})


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
            "blood_group", "address", "referred_by",
            *Patient.ALERT_FIELDS,
            "allergies", "current_medications", "medical_history", "tobacco_use", "dental_history",
            "emergency_contact_name", "emergency_contact_phone", "abha_number", "consent_given",
        ]
        widgets = {
            "date_of_birth": DateInput(),
            "address": forms.Textarea(attrs={"rows": 2}),
            "allergies": forms.Textarea(attrs={"rows": 2}),
            "current_medications": forms.Textarea(attrs={"rows": 2}),
            "medical_history": forms.Textarea(attrs={"rows": 2}),
            "dental_history": forms.Textarea(attrs={"rows": 2}),
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
        widgets = {"date": DateInput(), "reason": REASON_WIDGET}

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
        widgets = {"reason": REASON_WIDGET}

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
        fields = [
            "bp_systolic", "bp_diastolic", "pulse", "blood_sugar", "weight_kg",
            "complaints", "extraoral_findings", "intraoral_findings", "oral_hygiene", "investigations",
            "dental_chart", "diagnosis", "prescription", "advice", "follow_up_date",
        ]
        widgets = {
            "complaints": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. Pain in lower left back tooth for 3 days"}),
            "extraoral_findings": forms.Textarea(attrs={"rows": 2}),
            "intraoral_findings": forms.Textarea(attrs={"rows": 2}),
            "investigations": forms.Textarea(attrs={"rows": 2}),
            "dental_chart": forms.HiddenInput(),
            "diagnosis": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. 36 irreversible pulpitis"}),
            "prescription": forms.Textarea(attrs={"rows": 4, "placeholder": "Amoxicillin 500mg — 1-1-1 — 5 days"}),
            "advice": forms.Textarea(attrs={"rows": 2, "placeholder": "e.g. Don't eat for 1 hour. Avoid chewing on the left side."}),
            "follow_up_date": DateInput(),
        }

    def clean_dental_chart(self):
        try:
            return clean_dental_chart(self.cleaned_data.get("dental_chart"))
        except ValidationError as e:
            raise forms.ValidationError(e.messages)


class ProcedureChoiceField(forms.ModelChoiceField):
    """A procedure dropdown whose options carry their fee, so the form can fill in the price."""

    def __init__(self, **kwargs):
        super().__init__(queryset=Procedure.objects.filter(is_active=True), **kwargs)
        self.widget = ProcedureSelect()

    def label_from_instance(self, obj):
        return f"{obj.name} (₹{obj.default_price:,.0f})"


class ProcedureSelect(forms.Select):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        instance = getattr(value, "instance", None)
        if instance is not None:
            option["attrs"]["data-price"] = f"{instance.default_price:.2f}"
            option["attrs"]["data-name"] = instance.name
        return option


class TreatmentItemForm(forms.ModelForm):
    """Adding a procedure to the treatment plan during a visit."""

    procedure = ProcedureChoiceField(required=False)
    done_today = forms.BooleanField(required=False, label="Done today")

    class Meta:
        model = TreatmentItem
        fields = ["procedure", "teeth", "price", "notes"]
        widgets = {
            "teeth": forms.TextInput(attrs={"placeholder": "e.g. 36"}),
            "notes": forms.TextInput(attrs={"placeholder": "e.g. 1st sitting of 3"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["price"].required = False
        self.fields["procedure"].empty_label = "Choose a procedure…"

    def is_filled(self):
        return bool(self.data.get(self.add_prefix("procedure")))

    def clean(self):
        data = super().clean()
        if data.get("procedure") and data.get("price") is None:
            data["price"] = data["procedure"].default_price
            self.instance.price = data["price"]
        return data


class ProcedureForm(forms.ModelForm):
    class Meta:
        model = Procedure
        fields = ["name", "category", "default_price", "is_active"]


ProcedureFormSet = forms.modelformset_factory(Procedure, form=ProcedureForm, extra=3, can_delete=False)


class InvoiceLineForm(forms.Form):
    """A free line on a new bill (anything not already in the treatment plan)."""

    procedure = ProcedureChoiceField(required=False)
    description = forms.CharField(max_length=200, required=False)
    quantity = forms.IntegerField(min_value=1, max_value=99, initial=1, required=False)
    unit_price = forms.DecimalField(min_value=0, max_digits=10, decimal_places=2, required=False, label="Fee (₹)")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["procedure"].empty_label = "— Pick from price list —"

    def clean(self):
        data = super().clean()
        proc = data.get("procedure")
        if not proc and not data.get("description"):
            return data  # blank row
        if proc and not data.get("description"):
            data["description"] = proc.name
        if data.get("unit_price") is None:
            if proc:
                data["unit_price"] = proc.default_price
            else:
                self.add_error("unit_price", "Enter the fee.")
        data["quantity"] = data.get("quantity") or 1
        return data

    @property
    def line(self):
        d = getattr(self, "cleaned_data", {})
        if not d.get("description"):
            return None
        return {"description": d["description"], "quantity": d["quantity"], "unit_price": d["unit_price"]}


InvoiceLineFormSet = forms.formset_factory(InvoiceLineForm, extra=3)


class PaymentForm(forms.ModelForm):
    class Meta:
        model = Payment
        fields = ["amount", "mode", "reference"]
        labels = {"amount": "Amount (₹)"}

    def __init__(self, *args, balance=None, required=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.balance = balance
        self.fields["amount"].required = required
        if balance is not None:
            self.fields["amount"].widget.attrs["max"] = f"{balance:.2f}"

    def clean_amount(self):
        amount = self.cleaned_data.get("amount")
        if amount is None:
            return amount
        if amount <= 0:
            raise forms.ValidationError("Enter an amount above zero.")
        if self.balance is not None and amount > self.balance:
            raise forms.ValidationError(f"That's more than the ₹{self.balance:,.2f} due.")
        return amount


class InvoiceForm(forms.Form):
    discount = forms.DecimalField(min_value=0, max_digits=12, decimal_places=2, required=False, initial=0, label="Discount (₹)")
    notes = forms.CharField(max_length=255, required=False, label="Note on bill",
                            widget=forms.TextInput(attrs={"placeholder": "Optional, e.g. 2nd of 3 RCT sittings"}))


class ReasonForm(forms.Form):
    reason = forms.CharField(max_length=255, widget=forms.TextInput(attrs={"placeholder": "Why? (required)"}))


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
        fields = ["specialization", "qualification", "registration_number", "consultation_fee", "slot_minutes"]


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
