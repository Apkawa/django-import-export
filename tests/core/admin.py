from __future__ import unicode_literals

from django.contrib import admin

from import_export.admin import ImportExportMixin, GenericImportMixin

from .models import Book, Category, Author


class BookAdmin(ImportExportMixin, admin.ModelAdmin):
    list_filter = ['categories', 'author']


class SomeBook(Book):
    class Meta:
        proxy = True


class GenericImportBookAdmin(GenericImportMixin, admin.ModelAdmin):
    list_filter = ['categories', 'author']



admin.site.register(Book, BookAdmin)
admin.site.register(SomeBook, GenericImportBookAdmin)
admin.site.register(Category)
admin.site.register(Author)
