from django.apps import AppConfig


class ClinicConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "clinic"
    verbose_name = "Clinic"

    def ready(self):
        from . import access  # noqa: F401  (registers login/logout audit signals)
