"""Role checks and audit logging used by the views."""
from functools import wraps

from django.contrib.auth.decorators import login_required
from django.contrib.auth.signals import user_logged_in, user_logged_out, user_login_failed
from django.core.exceptions import PermissionDenied
from django.dispatch import receiver

from .models import AuditLog


def role_required(*roles, clinical=False):
    """Allow the view only for the given roles (admins always pass).

    clinical=True means "anyone allowed to see consultation notes".
    """

    def decorator(view):
        @wraps(view)
        @login_required
        def wrapped(request, *args, **kwargs):
            user = request.user
            allowed = user.is_clinic_admin or user.role in roles or (clinical and user.can_see_clinical)
            if not allowed:
                raise PermissionDenied
            return view(request, *args, **kwargs)

        return wrapped

    return decorator


def client_ip(request):
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def audit(request, action, description, patient=None):
    AuditLog.objects.create(
        user=request.user if request.user.is_authenticated else None,
        action=action,
        patient=patient,
        description=description[:255],
        ip_address=client_ip(request),
    )


@receiver(user_logged_in)
def _on_login(sender, request, user, **kwargs):
    if request is not None:
        AuditLog.objects.create(
            user=user, action=AuditLog.Action.LOGIN, description=f"{user.username} logged in", ip_address=client_ip(request)
        )


@receiver(user_logged_out)
def _on_logout(sender, request, user, **kwargs):
    if request is not None and user is not None:
        AuditLog.objects.create(
            user=user, action=AuditLog.Action.LOGOUT, description=f"{user.username} logged out", ip_address=client_ip(request)
        )


@receiver(user_login_failed)
def _on_login_failed(sender, credentials, request=None, **kwargs):
    if request is not None:
        AuditLog.objects.create(
            action=AuditLog.Action.LOGIN_FAILED,
            description=f"Failed login for '{credentials.get('username', '')}'"[:255],
            ip_address=client_ip(request),
        )
