from django.urls import path

from . import views, views_billing as billing

urlpatterns = [
    path("", views.dashboard, name="dashboard"),

    path("patients/", views.patient_list, name="patient_list"),
    path("patients/new/", views.patient_create, name="patient_create"),
    path("patients/search.json", views.patient_search_json, name="patient_search_json"),
    path("patients/<int:pk>/", views.patient_detail, name="patient_detail"),
    path("patients/<int:pk>/edit/", views.patient_edit, name="patient_edit"),

    path("appointments/", views.appointment_list, name="appointment_list"),
    path("appointments/book/", views.appointment_book, name="appointment_book"),
    path("appointments/walk-in/", views.appointment_walk_in, name="appointment_walk_in"),
    path("appointments/slots.json", views.doctor_slots_json, name="doctor_slots_json"),
    path("appointments/<int:pk>/status/", views.appointment_status, name="appointment_status"),
    path("appointments/<int:pk>/consultation/", views.consultation, name="consultation"),
    path("appointments/<int:pk>/prescription/", views.prescription_print, name="prescription_print"),

    path("staff/", views.staff_list, name="staff_list"),
    path("staff/new/", views.staff_form, name="staff_create"),
    path("staff/<int:pk>/", views.staff_form, name="staff_edit"),
    path("doctors/<int:pk>/leave/", views.doctor_leave_add, name="doctor_leave_add"),
    path("leave/<int:pk>/delete/", views.doctor_leave_delete, name="doctor_leave_delete"),

    path("billing/", billing.invoice_list, name="invoice_list"),
    path("billing/new/", billing.invoice_create, name="invoice_create"),
    path("billing/<int:pk>/", billing.invoice_detail, name="invoice_detail"),
    path("billing/<int:pk>/print/", billing.invoice_print, name="invoice_print"),
    path("billing/<int:pk>/cancel/", billing.invoice_cancel, name="invoice_cancel"),
    path("billing/payments/<int:pk>/void/", billing.payment_void, name="payment_void"),
    path("billing/collections/", billing.collection_report, name="collection_report"),
    path("billing/prices/", billing.price_list, name="price_list"),

    path("audit/", views.audit_log, name="audit_log"),
]
