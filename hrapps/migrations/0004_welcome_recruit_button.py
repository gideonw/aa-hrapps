# Handwritten: makemigrations needs the full Alliance Auth stack configured.
#
# default=True preserves existing behaviour across the upgrade. The welcome
# message already carried the Recruit Me button unconditionally, so defaulting
# to False would silently remove it from every install that has one.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("hrapps", "0003_recruitment_channels"),
    ]

    operations = [
        migrations.AddField(
            model_name="hrappdiscordsettings",
            name="welcome_include_recruit_button",
            field=models.BooleanField(default=True),
        ),
    ]
