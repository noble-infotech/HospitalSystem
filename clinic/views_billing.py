"""Billing: bills (invoices), payments, receipts, the collection report and the price list."""
import csv
from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.views.decorators.http import require_POST

from .access import audit, role_required
from .forms import InvoiceForm, InvoiceLineFormSet, PaymentForm, ProcedureFormSet, ReasonForm
from .models import Appointment, AuditLog, Invoice, InvoiceItem, Patient, Payment, Procedure, TreatmentItem, User

R = User.Role
BILLING = (R.RECEPTIONIST, R.NURSE, R.DOCTOR)  # anyone at the clinic can bill; admins always can


def _collections_allowed(user):
    if not user.can_see_collections:
        raise PermissionDenied


@role_required(*BILLING)
def invoice_list(request):
    invoices = Invoice.objects.select_related("patient")
    q = request.GET.get("q", "").strip()
    show = request.GET.get("show", "")
    if q:
        match = (
            Q(patient__first_name__icontains=q) | Q(patient__last_name__icontains=q)
            | Q(patient__phone__icontains=q) | Q(patient__patient_code__iexact=q)
        )
        fy, _, num = q.partition("/")  # a bill number like 2026-27/0012
        if num.isdigit():
            match |= Q(fy=fy, seq=int(num))
        invoices = invoices.filter(match)
    invoices = (invoices.with_balance_due() if show == "due" else invoices.with_paid()).order_by("-date", "-seq")
    page = Paginator(invoices, 30).get_page(request.GET.get("page"))
    return render(request, "clinic/billing/invoice_list.html", {"page": page, "q": q, "show": show})


@role_required(*BILLING)
def invoice_create(request):
    patient = get_object_or_404(Patient, pk=request.GET.get("patient") or request.POST.get("patient") or 0)
    appt_id = request.GET.get("appointment") or request.POST.get("appointment")
    appointment = None
    if appt_id and str(appt_id).isdigit():
        appointment = Appointment.objects.filter(pk=appt_id, patient=patient).select_related("doctor__user").first()

    unbilled = list(TreatmentItem.unbilled_for(patient))
    consult_fee = appointment.doctor.consultation_fee if appointment else Decimal("0")

    form = InvoiceForm(request.POST or None)
    lines = InvoiceLineFormSet(request.POST or None, prefix="line")
    pay_form = PaymentForm(request.POST or None, prefix="pay", required=False)

    if request.method == "POST":
        chosen_ids = {int(i) for i in request.POST.getlist("treatments") if i.isdigit()}
        chosen = [t for t in unbilled if t.pk in chosen_ids]
        add_consult = bool(request.POST.get("consult_fee")) and consult_fee > 0
        ok = form.is_valid() & lines.is_valid() & pay_form.is_valid()
        extra = [f.line for f in lines.forms if ok and f.line]
        subtotal = sum((t.price for t in chosen), Decimal("0")) + (consult_fee if add_consult else 0) \
            + sum((x["unit_price"] * x["quantity"] for x in extra), Decimal("0"))
        if ok and not (chosen or add_consult or extra):
            form.add_error(None, "Add at least one item to the bill.")
            ok = False
        discount = (form.cleaned_data.get("discount") or Decimal("0")) if ok else Decimal("0")
        if ok and discount > subtotal:
            form.add_error("discount", "The discount can't be more than the bill.")
            ok = False
        pay_amount = pay_form.cleaned_data.get("amount") if ok else None
        if ok and pay_amount and pay_amount > subtotal - discount:
            pay_form.add_error("amount", f"That's more than the bill total of ₹{subtotal - discount:,.2f}.")
            ok = False

        if ok:
            with transaction.atomic():
                # Re-check inside the transaction so two people can't bill the same procedure twice.
                still_unbilled = set(TreatmentItem.unbilled_for(patient).filter(pk__in=chosen_ids).values_list("pk", flat=True))
                if still_unbilled != chosen_ids:
                    messages.error(request, "Some of those procedures have already been billed. Please check the list and try again.")
                    url = f"{reverse('invoice_create')}?patient={patient.pk}"
                    return redirect(url + (f"&appointment={appointment.pk}" if appointment else ""))
                invoice = Invoice.objects.create(
                    patient=patient, appointment=appointment, discount=discount,
                    notes=form.cleaned_data.get("notes", ""), created_by=request.user,
                )
                if add_consult:
                    InvoiceItem.objects.create(invoice=invoice, description=f"Consultation — {appointment.doctor}",
                                               unit_price=consult_fee)
                for t in chosen:
                    InvoiceItem.objects.create(invoice=invoice, description=t.description, unit_price=t.price, treatment=t)
                for x in extra:
                    InvoiceItem.objects.create(invoice=invoice, **x)
                invoice.recalculate()
                if pay_amount:
                    Payment.objects.create(invoice=invoice, amount=pay_amount, mode=pay_form.cleaned_data["mode"],
                                           reference=pay_form.cleaned_data.get("reference", ""), received_by=request.user)
            audit(request, AuditLog.Action.CREATE, f"Bill {invoice.number} for ₹{invoice.total}", patient)
            if pay_amount:
                audit(request, AuditLog.Action.CREATE, f"Payment ₹{pay_amount} against bill {invoice.number}", patient)
            messages.success(request, f"Bill {invoice.number} created.")
            return redirect("invoice_detail", pk=invoice.pk)

    return render(request, "clinic/billing/invoice_form.html", {
        "patient": patient, "appointment": appointment, "unbilled": unbilled, "consult_fee": consult_fee,
        "form": form, "lines": lines, "pay_form": pay_form,
        "chosen_ids": {int(i) for i in request.POST.getlist("treatments") if i.isdigit()} if request.method == "POST"
        else {t.pk for t in unbilled},
        "consult_checked": bool(request.POST.get("consult_fee")) if request.method == "POST"
        else bool(appointment and consult_fee > 0 and not appointment.active_invoice),
    })


@role_required(*BILLING)
def invoice_detail(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("patient", "appointment__doctor__user", "created_by"), pk=pk)
    pay_form = PaymentForm(request.POST or None, balance=invoice.balance, initial={"amount": invoice.balance})
    if request.method == "POST":
        if invoice.is_cancelled or invoice.balance <= 0:
            messages.error(request, "Nothing is due on this bill.")
            return redirect("invoice_detail", pk=pk)
        if pay_form.is_valid():
            payment = pay_form.save(commit=False)
            payment.invoice, payment.received_by = invoice, request.user
            payment.save()
            audit(request, AuditLog.Action.CREATE, f"Payment ₹{payment.amount} ({payment.get_mode_display()}) against bill {invoice.number}", invoice.patient)
            messages.success(request, f"Received ₹{payment.amount:,.2f} by {payment.get_mode_display()}.")
            return redirect("invoice_detail", pk=pk)
    audit(request, AuditLog.Action.VIEW, f"Viewed bill {invoice.number}", invoice.patient)
    return render(request, "clinic/billing/invoice_detail.html", {
        "invoice": invoice, "items": invoice.items.all(),
        "payments": invoice.payments.select_related("received_by", "voided_by"),
        "pay_form": pay_form, "reason_form": ReasonForm(),
    })


@role_required(*BILLING)
def invoice_print(request, pk):
    invoice = get_object_or_404(Invoice.objects.select_related("patient", "appointment__doctor__user"), pk=pk)
    audit(request, AuditLog.Action.VIEW, f"Printed bill {invoice.number}", invoice.patient)
    return render(request, "clinic/billing/invoice_print.html", {
        "invoice": invoice, "items": invoice.items.all(), "payments": invoice.payments.filter(is_void=False),
    })


@require_POST
@role_required()
def invoice_cancel(request, pk):
    invoice = get_object_or_404(Invoice, pk=pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a reason for cancelling the bill.")
        return redirect("invoice_detail", pk=pk)
    try:
        invoice.cancel(request.user, form.cleaned_data["reason"])
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    else:
        audit(request, AuditLog.Action.UPDATE, f"Cancelled bill {invoice.number}: {invoice.cancel_reason}", invoice.patient)
        messages.success(request, f"Bill {invoice.number} cancelled. Its procedures can be billed again.")
    return redirect("invoice_detail", pk=pk)


@require_POST
@role_required()
def payment_void(request, pk):
    payment = get_object_or_404(Payment.objects.select_related("invoice__patient"), pk=pk)
    form = ReasonForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Give a reason for voiding the payment.")
        return redirect("invoice_detail", pk=payment.invoice_id)
    try:
        payment.void(request.user, form.cleaned_data["reason"])
    except ValidationError as e:
        messages.error(request, " ".join(e.messages))
    else:
        audit(request, AuditLog.Action.UPDATE,
              f"Voided payment ₹{payment.amount} on bill {payment.invoice.number}: {payment.void_reason}", payment.invoice.patient)
        messages.success(request, "Payment voided.")
    return redirect("invoice_detail", pk=payment.invoice_id)


@role_required(*BILLING)
def collection_report(request):
    _collections_allowed(request.user)
    today = timezone.localdate()
    start = parse_date(request.GET.get("from", "") or "") or today
    end = parse_date(request.GET.get("to", "") or "") or start
    if end < start:
        start, end = end, start

    payments = (
        Payment.objects.filter(is_void=False, received_at__date__gte=start, received_at__date__lte=end)
        .select_related("invoice__patient", "received_by").order_by("received_at")
    )

    if request.GET.get("format") == "csv":
        response = HttpResponse(content_type="text/csv")
        response["Content-Disposition"] = f'attachment; filename="collections-{start}-to-{end}.csv"'
        writer = csv.writer(response)
        writer.writerow(["Date", "Time", "Bill no.", "Patient ID", "Patient", "Mode", "Reference", "Amount", "Received by"])
        for p in payments:
            t = timezone.localtime(p.received_at)
            writer.writerow([t.date(), t.strftime("%H:%M"), p.invoice.number, p.invoice.patient.patient_code,
                             p.invoice.patient.full_name, p.get_mode_display(), p.reference, p.amount, p.received_by or ""])
        return response

    by_mode = {row["mode"]: row for row in payments.values("mode").annotate(total=Sum("amount"), n=Count("id"))}
    modes = [{"label": label, "total": by_mode.get(code, {}).get("total") or 0, "count": by_mode.get(code, {}).get("n") or 0}
             for code, label in Payment.Mode.choices]
    by_day = list(payments.values("received_at__date").annotate(total=Sum("amount"), n=Count("id")).order_by("received_at__date"))

    billed = Invoice.objects.filter(is_cancelled=False, date__gte=start, date__lte=end).aggregate(
        total=Sum("total"), discount=Sum("discount"), n=Count("id"))
    dues = [i for i in Invoice.objects.with_balance_due().select_related("patient").order_by("date")]

    return render(request, "clinic/billing/collection_report.html", {
        "start": start, "end": end, "payments": payments, "modes": modes, "by_day": by_day,
        "collected": sum((m["total"] for m in modes), Decimal("0")),
        "billed": billed, "dues": dues, "dues_total": sum((i.balance for i in dues), Decimal("0")),
        "yesterday": today - timedelta(days=1), "today": today,
        "month_start": today.replace(day=1),
    })


@role_required()
def price_list(request):
    formset = ProcedureFormSet(request.POST or None, queryset=Procedure.objects.all())
    if request.method == "POST" and formset.is_valid():
        saved = formset.save()
        if saved:
            audit(request, AuditLog.Action.UPDATE, f"Updated price list ({len(saved)} item(s))")
        messages.success(request, "Price list saved.")
        return redirect("price_list")
    return render(request, "clinic/billing/price_list.html", {"formset": formset})
