from django.conf import settings


def clinic(request):
    return {"CLINIC_NAME": settings.CLINIC_NAME}
