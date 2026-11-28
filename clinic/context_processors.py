from django.conf import settings


def clinic(request):
    return {
        "CLINIC_NAME": settings.CLINIC_NAME,
        "CLINIC_ADDRESS": settings.CLINIC_ADDRESS,
        "CLINIC_PHONE": settings.CLINIC_PHONE,
    }
