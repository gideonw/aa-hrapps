# Handwritten: makemigrations needs the full Alliance Auth stack configured.
# Named distinctively rather than generically so a future upstream 0003_*
# collides visibly on rebase.
#
# Operation order matters in both directions. Django reverses operations in
# reverse list order, so on the way down: the registry is dropped, the new
# constraints go, use_recruitment_threads is restored as a column, the old
# constraint is restored, and only then does RunPython repopulate the boolean
# from the mode. That ordering is what makes the reverse work at all, and
# Task 4 proves it rather than assuming it.

from django.db import migrations, models


def threads_boolean_to_mode(apps, schema_editor):
    HRAppDiscordSettings = apps.get_model("hrapps", "HRAppDiscordSettings")
    HRAppDiscordSettings.objects.filter(use_recruitment_threads=True).update(
        recruitment_mode="threads"
    )


def mode_to_threads_boolean(apps, schema_editor):
    HRAppDiscordSettings = apps.get_model("hrapps", "HRAppDiscordSettings")
    HRAppDiscordSettings.objects.filter(recruitment_mode="threads").update(
        use_recruitment_threads=True
    )


class Migration(migrations.Migration):

    dependencies = [
        ("hrapps", "0002_alter_attachment_options"),
    ]

    operations = [
        migrations.AddField(
            model_name="hrappdiscordsettings",
            name="recruitment_mode",
            field=models.CharField(
                choices=[
                    ("off", "Disabled"),
                    ("threads", "Public threads"),
                    ("channels", "Private channels"),
                ],
                default="off",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="hrappdiscordsettings",
            name="recruitment_category",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="hrappdiscordsettings",
            name="recruitment_archive_category",
            field=models.BigIntegerField(blank=True, null=True),
        ),
        migrations.RunPython(threads_boolean_to_mode, mode_to_threads_boolean),
        migrations.RemoveConstraint(
            model_name="hrappdiscordsettings",
            name="recruitment_thread_channel_required_if_threads_enabled",
        ),
        migrations.RemoveField(
            model_name="hrappdiscordsettings",
            name="use_recruitment_threads",
        ),
        migrations.AddConstraint(
            model_name="hrappdiscordsettings",
            constraint=models.CheckConstraint(
                condition=~models.Q(recruitment_mode="threads")
                | models.Q(recruitment_thread_channel__isnull=False),
                name="thread_channel_required_in_threads_mode",
            ),
        ),
        migrations.AddConstraint(
            model_name="hrappdiscordsettings",
            constraint=models.CheckConstraint(
                condition=~models.Q(recruitment_mode="channels")
                | (
                    models.Q(recruitment_category__isnull=False)
                    & models.Q(recruitment_archive_category__isnull=False)
                    & models.Q(recruiter_role__isnull=False)
                ),
                name="categories_and_role_required_in_channels_mode",
            ),
        ),
        migrations.CreateModel(
            name="RecruitmentChannel",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("discord_user_id", models.BigIntegerField(db_index=True)),
                ("channel_id", models.BigIntegerField(unique=True)),
                ("created", models.DateTimeField(auto_now_add=True)),
                ("archived", models.DateTimeField(blank=True, null=True)),
                ("open_for_user", models.BigIntegerField(blank=True, null=True, unique=True)),
            ],
            options={
                "default_permissions": (),
            },
        ),
    ]
