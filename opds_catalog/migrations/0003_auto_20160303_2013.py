# -*- coding: utf-8 -*-
# Generated by Django 1.9.2 on 2016-03-03 17:13
from __future__ import unicode_literals

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('opds_catalog', '0002_auto_20160301_2042'),
    ]

    operations = [
        migrations.RenameField(
            model_name='bseries',
            old_name='series',
            new_name='ser',
        ),
    ]
