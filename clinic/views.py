import json
from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .access import audit, role_required
from .forms import (
    VISIT_REASONS, AppointmentForm, AvailabilityFormSet, ConsultationForm, DoctorForm, LeaveForm, PatientForm,
    StaffForm, TreatmentItemForm, WalkInForm,
)
from .models import (
    TOOTH_CONDITIONS, Appointment, AuditLog, Consultation, Doctor, DoctorLeave, Invoice, Patient, Payment,
    TreatmentItem, User, dental_chart_layout,
)

R = User.Role
FRONT_DESK = (R.RECEPTIONIST, R.NURSE, R.DOCTOR)  # everyone who works with patients


def _selected_date(request):
    return parse_date(request.GET.get("date", "") or "") or timezone.localdate()


# ---------------------------------------------------------------- dashboard

@login_required
def dashboard(request):
    today = timezone.localdate()
    appts = Appointment.objects.filter(date=today).select_related("patient", "doctor__user")
    my_doctor = getattr(request.user, "doctor_profile", None) if request.user.is_doctor else None
    if my_doctor:
        appts = appts.filter(doctor=my_doctor)

    counts = {s: 0 for s in Appointment.Status.values}
    for row in appts.values("status").annotate(n=Count("id")):
        counts[row["status"]] = row["n"]

    doctors = Doctor.objects.filter(is_active=True).select_related("user")
    if my_doctor:
        doctors = doctors.filter(pk=my_doctor.pk)
    queues = []
    for doc in doctors:
        doc_appts = [a for a in appts if a.doctor_id == doc.pk]
        queues.append({
            "doctor": doc,
            "on_leave": DoctorLeave.objects.filter(doctor=doc, date=today).exists(),
            "waiting": [a for a in doc_appts if a.status == Appointment.Status.CHECKED_IN],
            "current": [a for a in doc_appts if a.status == Appointment.Status.IN_CONSULTATION],
            "upcoming": [a for a in doc_appts if a.status == Appointment.Status.SCHEDULED],
            "done": sum(1 for a in doc_appts if a.status == Appointment.Status.COMPLETED),
        })

    # Patients whose visit is finished today but who haven't been billed yet.
    to_bill = (
        Appointment.objects.filter(date=today, status=Appointment.Status.COMPLETED)
        .exclude(invoices__is_cancelled=False).select_related("patient", "doctor__user").order_by("completed_at")
    )
    collected = None
    if request.user.can_see_collections:
        collected = Payment.objects.filter(is_void=False, received_at__date=today).aggregate(s=Sum("amount"))["s"] or 0

    return render(request, "clinic/dashboard.html", {
        "today": today,
        "counts": counts,
        "total": sum(c for s, c in counts.items() if s != Appointment.Status.CANCELLED),
        "queues": queues,
        "new_patients_today": Patient.objects.filter(created_at__date=today).count(),
        "to_bill": to_bill,
        "collected": collected,
    })


# ---------------------------------------------------------------- patients

@role_required(*FRONT_DESK)
def patient_list(request):
    q = request.GET.get("q", "").strip()
    patients = Patient.objects.all()
    if q:
        patients = patients.filter(
            Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q) | Q(patient_code__iexact=q)
        )
    page = Paginator(patients, 25).get_page(request.GET.get("page"))
    return render(request, "clinic/patient_list.html", {"page": page, "q": q})


@role_required(*FRONT_DESK)
def patient_search_json(request):
    """Used by the booking forms to find a patient by name, phone or ID."""
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    patients = Patient.objects.filter(
        Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(phone__icontains=q) | Q(patient_code__iexact=q)
    )[:10]
    return JsonResponse({"results": [
        {"id": p.pk, "label": f"{p.full_name} · {p.patient_code} · {p.phone}"} for p in patients
    ]})


@role_required(*FRONT_DESK)
def patient_create(request):
    form = PatientForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        patient = form.save(commit=False)
        patient.created_by = request.user
        patient.save()
        audit(request, AuditLog.Action.CREATE, f"Registered patient {patient.patient_code}", patient)
        messages.success(request, f"Registered {patient.full_name} as {patient.patient_code}.")
        next_step = request.POST.get("next")
        if next_step == "book":
            return redirect(f"/appointments/book/?patient={patient.pk}")
        if next_step == "walkin":
            return redirect(f"/appointments/walk-in/?patient={patient.pk}")
        return redirect("patient_detail", pk=patient.pk)
    return render(request, "clinic/patient_form.html", {"form": form, "is_new": True, "alert_fields": Patient.ALERT_FIELDS})


@role_required(*FRONT_DESK)
def patient_edit(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    form = PatientForm(request.POST or None, instance=patient)
    if request.method == "POST" and form.is_valid():
        form.save()
        changed = ", ".join(form.changed_data) or "no fields"
        audit(request, AuditLog.Action.UPDATE, f"Edited {patient.patient_code}: {changed}", patient)
        messages.success(request, "Patient details saved.")
        return redirect("patient_detail", pk=patient.pk)
    return render(request, "clinic/patient_form.html", {"form": form, "patient": patient, "is_new": False, "alert_fields": Patient.ALERT_FIELDS})


@role_required(*FRONT_DESK)
def patient_detail(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    appointments = patient.appointments.select_related("doctor__user").order_by("-date", "-token")
    show_clinical = request.user.can_see_clinical
    consultations, treatments, chart = [], [], None
    if show_clinical:
        consultations = (
            Consultation.objects.filter(appointment__patient=patient)
            .select_related("appointment__doctor__user").order_by("-appointment__date")
        )
        treatments = patient.treatments.select_related("procedure", "doctor__user").exclude(status=TreatmentItem.Status.CANCELLED)
        chart = dental_chart_layout(patient.latest_dental_chart)
    invoices = patient.invoices.with_paid().order_by("-date", "-seq")
    audit(request, AuditLog.Action.VIEW, f"Viewed {patient.patient_code}", patient)
    return render(request, "clinic/patient_detail.html", {
        "patient": patient, "appointments": appointments,
        "consultations": consultations, "show_clinical": show_clinical,
        "treatments": treatments, "chart": chart,
        "invoices": invoices, "balance_due": sum((i.balance for i in invoices), 0),
        "unbilled": TreatmentItem.unbilled_for(patient),
    })


# ---------------------------------------------------------------- appointments

@role_required(*FRONT_DESK)
def appointment_list(request):
    day = _selected_date(request)
    appts = Appointment.objects.filter(date=day).select_related("patient", "doctor__user").order_by("doctor", "token")
    doctor_id = request.GET.get("doctor")
    if request.user.is_doctor and doctor_id is None and hasattr(request.user, "doctor_profile"):
        doctor_id = str(request.user.doctor_profile.pk)
    if doctor_id:
        appts = appts.filter(doctor_id=doctor_id)
    return render(request, "clinic/appointment_list.html", {
        "appts": appts, "day": day,
        "prev_day": day - timedelta(days=1), "next_day": day + timedelta(days=1),
        "doctors": Doctor.objects.filter(is_active=True).select_related("user"),
        "doctor_id": doctor_id or "",
    })


def _initial_patient(request):
    pid = request.GET.get("patient")
    return Patient.objects.filter(pk=pid).first() if pid and pid.isdigit() else None


@role_required(*FRONT_DESK)
def appointment_book(request):
    initial_patient = _initial_patient(request)
    form = AppointmentForm(
        request.POST or None,
        initial={"patient": initial_patient, "date": timezone.localdate(), "doctor": request.GET.get("doctor")},
    )
    if request.method == "POST" and form.is_valid():
        appt = form.save(commit=False)
        appt.created_by = request.user
        try:
            appt.save()
        except IntegrityError:
            # Someone else took the same slot a split second earlier.
            form.add_error("time", "That slot was just booked by someone else. Pick another time.")
        else:
            audit(request, AuditLog.Action.CREATE, f"Booked appointment #{appt.token} on {appt.date} {appt.time:%H:%M} with {appt.doctor}", appt.patient)
            messages.success(request, f"Booked {appt.patient.full_name} with {appt.doctor} on {appt.date:%d %b} at {appt.time:%I:%M %p} (token #{appt.token}).")
            return redirect(f"/appointments/?date={appt.date.isoformat()}")
    chosen = form["patient"].value()
    patient = initial_patient or (Patient.objects.filter(pk=chosen).first() if chosen and str(chosen).isdigit() else None)
    return render(request, "clinic/appointment_form.html", {"form": form, "patient": patient, "mode": "book", "reasons": VISIT_REASONS})


@role_required(*FRONT_DESK)
def appointment_walk_in(request):
    initial_patient = _initial_patient(request)
    form = WalkInForm(request.POST or None, initial={"patient": initial_patient})
    if request.method == "POST" and form.is_valid():
        appt = form.save(commit=False)
        appt.created_by = request.user
        appt.checked_in_at = timezone.now()
        appt.save()
        audit(request, AuditLog.Action.CREATE, f"Walk-in token #{appt.token} with {appt.doctor}", appt.patient)
        messages.success(request, f"{appt.patient.full_name} added to {appt.doctor}'s queue — token #{appt.token}.")
        return redirect("dashboard")
    chosen = form["patient"].value()
    patient = initial_patient or (Patient.objects.filter(pk=chosen).first() if chosen and str(chosen).isdigit() else None)
    return render(request, "clinic/appointment_form.html", {"form": form, "patient": patient, "mode": "walkin", "reasons": VISIT_REASONS})


@role_required(*FRONT_DESK)
def doctor_slots_json(request):
    doctor = Doctor.objects.filter(pk=request.GET.get("doctor"), is_active=True).first()
    day = parse_date(request.GET.get("date", "") or "")
    if not doctor or not day:
        return JsonResponse({"slots": [], "message": "Pick a doctor and a date."})
    if DoctorLeave.objects.filter(doctor=doctor, date=day).exists():
        return JsonResponse({"slots": [], "message": f"{doctor} is on leave that day."})
    if not doctor.schedule_for(day).exists():
        return JsonResponse({"slots": [], "message": f"{doctor} doesn't work on {day:%A}s."})
    slots = doctor.free_slots(day)
    return JsonResponse({
        "slots": [{"value": t.strftime("%H:%M"), "label": t.strftime("%I:%M %p")} for t in slots],
        "message": "" if slots else "No free slots left that day.",
    })


@require_POST
@role_required(*FRONT_DESK)
def appointment_status(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("doctor", "patient"), pk=pk)
    new_status = request.POST.get("status")
    # Only the doctor (or an admin) starts or finishes a consultation.
    if new_status in (Appointment.Status.IN_CONSULTATION, Appointment.Status.COMPLETED):
        if not (request.user.is_clinic_admin or (request.user.is_doctor and appt.doctor.user_id == request.user.pk)):
            raise PermissionDenied
    try:
        appt.move_to(new_status)
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    else:
        audit(request, AuditLog.Action.STATUS, f"Token #{appt.token} → {appt.get_status_display()}", appt.patient)
        if new_status == Appointment.Status.IN_CONSULTATION:
            return redirect("consultation", pk=appt.pk)
    return redirect(request.POST.get("next") or "dashboard")


# ---------------------------------------------------------------- consultation

def _treatment_action(request, appt):
    """Mark a planned procedure done or cancel it. Returns an error message or None."""
    for action in ("treat_done", "treat_cancel"):
        item_id = request.POST.get(action)
        if not item_id:
            continue
        item = TreatmentItem.objects.filter(pk=item_id, patient=appt.patient).select_related("procedure").first()
        if item is None:
            return "That procedure no longer exists."
        try:
            if action == "treat_done":
                if not (request.user.is_doctor or request.user.is_clinic_admin):
                    return "Only the dentist can mark a procedure as done."
                item.mark_done(appt)
                audit(request, AuditLog.Action.UPDATE, f"Procedure done: {item.description}", appt.patient)
            else:
                if item.status != TreatmentItem.Status.PLANNED:
                    return "Only a planned procedure can be cancelled."
                item.status = TreatmentItem.Status.CANCELLED
                item.save()
                audit(request, AuditLog.Action.UPDATE, f"Procedure removed from plan: {item.description}", appt.patient)
        except ValidationError as e:
            return " ".join(e.messages)
    return None


@role_required(R.DOCTOR, R.NURSE, clinical=True)
def consultation(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("patient", "doctor__user"), pk=pk)
    if request.user.is_doctor and appt.doctor.user_id != request.user.pk:
        raise PermissionDenied
    record = Consultation.objects.filter(appointment=appt).first()
    if record is None:
        # Start from the chart recorded at the last visit, so teeth don't have to be re-marked.
        record = Consultation(appointment=appt, dental_chart=appt.patient.latest_dental_chart)
    form = ConsultationForm(request.POST or None, instance=record)
    treatment_form = TreatmentItemForm(request.POST or None, prefix="t")

    if request.method == "POST":
        adding = treatment_form.is_filled()
        if form.is_valid() and (not adding or treatment_form.is_valid()):
            with transaction.atomic():
                record = form.save(commit=False)
                record.updated_by = request.user
                record.save()
                audit(request, AuditLog.Action.UPDATE, f"Saved consultation for token #{appt.token}", appt.patient)
                if adding:
                    item = treatment_form.save(commit=False)
                    item.patient, item.planned_at, item.created_by = appt.patient, appt, request.user
                    item.save()
                    if treatment_form.cleaned_data.get("done_today") and (request.user.is_doctor or request.user.is_clinic_admin):
                        item.mark_done(appt)
                    audit(request, AuditLog.Action.UPDATE,
                          f"Procedure {item.get_status_display().lower()}: {item.description}", appt.patient)
                error = _treatment_action(request, appt)
            if error:
                messages.error(request, error)
            elif request.POST.get("finish") and appt.can_move_to(Appointment.Status.COMPLETED) and request.user.is_doctor:
                appt.move_to(Appointment.Status.COMPLETED)
                audit(request, AuditLog.Action.STATUS, f"Token #{appt.token} → {appt.get_status_display()}", appt.patient)
                messages.success(request, f"Visit for {appt.patient.full_name} completed. Reception can now bill it.")
                return redirect("dashboard")
            else:
                messages.success(request, "Saved.")
            return redirect(f"{request.path}#treatment" if adding or "treat_done" in request.POST or "treat_cancel" in request.POST else request.path)

    past = (
        Consultation.objects.filter(appointment__patient=appt.patient).exclude(appointment=appt)
        .select_related("appointment__doctor__user").order_by("-appointment__date")[:5]
    )
    chart_value = form["dental_chart"].value()
    if isinstance(chart_value, str):
        try:
            chart_value = json.loads(chart_value or "{}")
        except ValueError:
            chart_value = {}
    return render(request, "clinic/consultation.html", {
        "appt": appt, "form": form, "record": record, "past": past,
        "treatment_form": treatment_form,
        "treatments": appt.patient.treatments.select_related("procedure", "doctor__user")
                         .exclude(status=TreatmentItem.Status.CANCELLED),
        "chart": dental_chart_layout(chart_value if isinstance(chart_value, dict) else {}),
        "conditions": TOOTH_CONDITIONS,
    })


@role_required(R.DOCTOR, R.NURSE, clinical=True)
def prescription_print(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("patient", "doctor__user"), pk=pk)
    record = get_object_or_404(Consultation, appointment=appt)
    audit(request, AuditLog.Action.VIEW, f"Printed prescription for token #{appt.token}", appt.patient)
    return render(request, "clinic/prescription_print.html", {
        "appt": appt, "record": record,
        "done": appt.treatments_done.select_related("procedure"),
        "planned": appt.patient.treatments.filter(status=TreatmentItem.Status.PLANNED).select_related("procedure"),
    })


# ---------------------------------------------------------------- staff management (admin only)

@role_required()
def staff_list(request):
    staff = User.objects.select_related("doctor_profile").order_by("-is_active", "role", "first_name")
    return render(request, "clinic/staff_list.html", {"staff": staff})


@role_required()
def staff_form(request, pk=None):
    user = get_object_or_404(User, pk=pk) if pk else User()
    doctor = getattr(user, "doctor_profile", None) if pk else None
    doctor = doctor or Doctor(user=user if pk else None)

    form = StaffForm(request.POST or None, instance=user, prefix="u")
    doctor_form = DoctorForm(request.POST or None, instance=doctor, prefix="d")
    formset = AvailabilityFormSet(request.POST or None, instance=doctor if doctor.pk else Doctor(), prefix="av")

    if request.method == "POST":
        is_doctor = request.POST.get("u-role") == R.DOCTOR
        valid = form.is_valid() and (not is_doctor or (doctor_form.is_valid() and formset.is_valid()))
        if valid and user.pk == request.user.pk and not form.cleaned_data["is_active"]:
            form.add_error("is_active", "You can't deactivate your own account.")
            valid = False
        if valid:
            with transaction.atomic():
                user = form.save()
                if is_doctor:
                    doc = doctor_form.save(commit=False)
                    doc.user = user
                    doc.is_active = user.is_active
                    doc.save()
                    formset.instance = doc
                    formset.save()
                elif doctor.pk:
                    doctor.is_active = False  # role changed away from doctor: keep history, hide from booking
                    doctor.save()
            messages.success(request, f"Saved {user}.")
            return redirect("staff_list")

    return render(request, "clinic/staff_form.html", {
        "form": form, "doctor_form": doctor_form, "formset": formset, "staff_user": user if pk else None,
        "leaves": doctor.leaves.filter(date__gte=timezone.localdate()) if doctor.pk else [],
        "leave_form": LeaveForm(prefix="lv") if doctor.pk else None, "doctor": doctor if doctor.pk else None,
    })


@require_POST
@role_required()
def doctor_leave_add(request, pk):
    doctor = get_object_or_404(Doctor, pk=pk)
    form = LeaveForm(request.POST, prefix="lv", instance=DoctorLeave(doctor=doctor))
    if form.is_valid():
        leave = form.save()
        clashing = doctor.appointments.filter(date=leave.date).exclude(
            status__in=[Appointment.Status.CANCELLED, Appointment.Status.COMPLETED]).count()
        msg = f"{doctor} marked on leave for {leave.date:%d %b %Y}."
        if clashing:
            msg += f" {clashing} existing appointment(s) that day need to be rescheduled — see the appointments page."
            messages.warning(request, msg)
        else:
            messages.success(request, msg)
    else:
        messages.error(request, "Could not add leave: " + "; ".join(e for errs in form.errors.values() for e in errs))
    return redirect("staff_edit", pk=doctor.user_id)


@require_POST
@role_required()
def doctor_leave_delete(request, pk):
    leave = get_object_or_404(DoctorLeave, pk=pk)
    user_id = leave.doctor.user_id
    leave.delete()
    messages.success(request, "Leave removed.")
    return redirect("staff_edit", pk=user_id)


@role_required()
def audit_log(request):
    logs = AuditLog.objects.select_related("user", "patient")
    if request.GET.get("patient"):
        logs = logs.filter(patient__patient_code__iexact=request.GET["patient"].strip())
    page = Paginator(logs, 50).get_page(request.GET.get("page"))
    return render(request, "clinic/audit_log.html", {"page": page, "patient_q": request.GET.get("patient", "")})
