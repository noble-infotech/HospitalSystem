from datetime import date, timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .access import audit, role_required
from .forms import (
    AppointmentForm, AvailabilityFormSet, ConsultationForm, DoctorForm, LeaveForm, PatientForm, StaffForm, WalkInForm,
)
from .models import Appointment, AuditLog, Consultation, Doctor, DoctorLeave, Patient, User

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

    return render(request, "clinic/dashboard.html", {
        "today": today,
        "counts": counts,
        "total": sum(c for s, c in counts.items() if s != Appointment.Status.CANCELLED),
        "queues": queues,
        "new_patients_today": Patient.objects.filter(created_at__date=today).count(),
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
    return render(request, "clinic/patient_form.html", {"form": form, "is_new": True})


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
    return render(request, "clinic/patient_form.html", {"form": form, "patient": patient, "is_new": False})


@role_required(*FRONT_DESK)
def patient_detail(request, pk):
    patient = get_object_or_404(Patient, pk=pk)
    appointments = patient.appointments.select_related("doctor__user").order_by("-date", "-token")
    show_clinical = request.user.can_see_clinical
    consultations = []
    if show_clinical:
        consultations = (
            Consultation.objects.filter(appointment__patient=patient)
            .select_related("appointment__doctor__user").order_by("-appointment__date")
        )
    audit(request, AuditLog.Action.VIEW, f"Viewed {patient.patient_code}", patient)
    return render(request, "clinic/patient_detail.html", {
        "patient": patient, "appointments": appointments,
        "consultations": consultations, "show_clinical": show_clinical,
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
    return render(request, "clinic/appointment_form.html", {"form": form, "patient": patient, "mode": "book"})


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
    return render(request, "clinic/appointment_form.html", {"form": form, "patient": patient, "mode": "walkin"})


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

@role_required(R.DOCTOR, R.NURSE, clinical=True)
def consultation(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("patient", "doctor__user"), pk=pk)
    if request.user.is_doctor and appt.doctor.user_id != request.user.pk:
        raise PermissionDenied
    record = Consultation.objects.filter(appointment=appt).first() or Consultation(appointment=appt)
    form = ConsultationForm(request.POST or None, instance=record)
    if request.method == "POST" and form.is_valid():
        record = form.save(commit=False)
        record.updated_by = request.user
        record.save()
        audit(request, AuditLog.Action.UPDATE, f"Saved consultation for token #{appt.token}", appt.patient)
        if request.POST.get("finish") and appt.can_move_to(Appointment.Status.COMPLETED) and request.user.is_doctor:
            appt.move_to(Appointment.Status.COMPLETED)
            messages.success(request, f"Consultation for {appt.patient.full_name} completed.")
            return redirect("dashboard")
        messages.success(request, "Consultation notes saved.")
        return redirect("consultation", pk=appt.pk)
    past = (
        Consultation.objects.filter(appointment__patient=appt.patient).exclude(appointment=appt)
        .select_related("appointment__doctor__user").order_by("-appointment__date")[:5]
    )
    return render(request, "clinic/consultation.html", {"appt": appt, "form": form, "record": record, "past": past})


@role_required(R.DOCTOR, R.NURSE, clinical=True)
def prescription_print(request, pk):
    appt = get_object_or_404(Appointment.objects.select_related("patient", "doctor__user"), pk=pk)
    record = get_object_or_404(Consultation, appointment=appt)
    audit(request, AuditLog.Action.VIEW, f"Printed prescription for token #{appt.token}", appt.patient)
    return render(request, "clinic/prescription_print.html", {"appt": appt, "record": record})


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
