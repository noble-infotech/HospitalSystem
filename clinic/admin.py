from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import Appointment, AuditLog, Consultation, Doctor, DoctorAvailability, DoctorLeave, Patient, User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "first_name", "last_name", "role", "is_active")
    list_filter = ("role", "is_active")
    fieldsets = BaseUserAdmin.fieldsets + (("Clinic", {"fields": ("role", "phone")}),)
    add_fieldsets = BaseUserAdmin.add_fieldsets + (("Clinic", {"fields": ("first_name", "last_name", "role", "phone")}),)


class AvailabilityInline(admin.TabularInline):
    model = DoctorAvailability
    extra = 1


@admin.register(Doctor)
class DoctorAdmin(admin.ModelAdmin):
    list_display = ("__str__", "specialization", "consultation_fee", "slot_minutes", "is_active")
    inlines = [AvailabilityInline]


@admin.register(Patient)
class PatientAdmin(admin.ModelAdmin):
    list_display = ("patient_code", "full_name", "phone", "gender", "created_at")
    search_fields = ("patient_code", "first_name", "last_name", "phone")
    readonly_fields = ("patient_code", "consent_at", "created_at", "updated_at", "created_by")


@admin.register(Appointment)
class AppointmentAdmin(admin.ModelAdmin):
    list_display = ("date", "token", "time", "patient", "doctor", "kind", "status")
    list_filter = ("date", "doctor", "status", "kind")
    search_fields = ("patient__first_name", "patient__phone", "patient__patient_code")


admin.site.register(Consultation)
admin.site.register(DoctorLeave)


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "user", "action", "patient", "description", "ip_address")
    list_filter = ("action",)

    # The audit trail is append-only: nobody edits or deletes it from the admin.
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
