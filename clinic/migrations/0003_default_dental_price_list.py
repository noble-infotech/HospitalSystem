"""A starting price list of common dental procedures.

The fees are typical for a small Indian practice and are only a starting point: the clinic owner
edits them (or switches off ones they don't offer) on the Price list screen.
"""
from django.db import migrations

PROCEDURES = [
    ("diagnostic", "Consultation / check-up", 300),
    ("diagnostic", "IOPA X-ray (per film)", 200),
    ("diagnostic", "OPG (full-mouth X-ray)", 600),
    ("preventive", "Scaling & polishing", 1200),
    ("preventive", "Fluoride application", 800),
    ("preventive", "Pit & fissure sealant (per tooth)", 600),
    ("restorative", "Composite filling (per tooth)", 1200),
    ("restorative", "GIC filling (per tooth)", 800),
    ("restorative", "Temporary filling", 300),
    ("endodontic", "Root canal treatment — front tooth", 3500),
    ("endodontic", "Root canal treatment — premolar", 4500),
    ("endodontic", "Root canal treatment — molar", 5500),
    ("endodontic", "Re-root canal treatment", 6500),
    ("periodontic", "Deep cleaning / curettage (per quadrant)", 1500),
    ("periodontic", "Flap surgery (per quadrant)", 6000),
    ("prosthodontic", "Metal crown", 2500),
    ("prosthodontic", "Porcelain-fused-to-metal (PFM) crown", 4000),
    ("prosthodontic", "Zirconia crown", 10000),
    ("prosthodontic", "Removable partial denture", 3500),
    ("prosthodontic", "Complete denture (per arch)", 10000),
    ("surgery", "Simple extraction", 800),
    ("surgery", "Surgical extraction", 2500),
    ("surgery", "Impacted wisdom tooth removal", 5000),
    ("implant", "Dental implant (with crown)", 30000),
    ("orthodontic", "Metal braces (full treatment)", 35000),
    ("orthodontic", "Ceramic braces (full treatment)", 50000),
    ("orthodontic", "Retainer (per arch)", 3000),
    ("cosmetic", "Teeth whitening", 8000),
    ("cosmetic", "Veneer (per tooth)", 8000),
    ("pediatric", "Pulpectomy (milk tooth)", 2000),
    ("pediatric", "Stainless steel crown (milk tooth)", 1800),
]


def load(apps, schema_editor):
    Procedure = apps.get_model("clinic", "Procedure")
    for category, name, price in PROCEDURES:
        Procedure.objects.get_or_create(name=name, defaults={"category": category, "default_price": price})


class Migration(migrations.Migration):
    dependencies = [("clinic", "0002_dental_records_and_billing")]
    operations = [migrations.RunPython(load, migrations.RunPython.noop)]
