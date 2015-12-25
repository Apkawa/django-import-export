from __future__ import unicode_literals

from django.contrib import admin

from import_export.admin import ImportExportMixin, GenericImportMixin, ExportActionModelAdmin
from import_export.resources import ModelResource

from .models import Book, Category, Author


class BookAdmin(ImportExportMixin, admin.ModelAdmin):
    list_filter = ['categories', 'author']


class CategoryAdmin(ExportActionModelAdmin):
    pass


class SomeBook(Book):
    class Meta:
        proxy = True

class BookImportResource(ModelResource):
    class Meta:
        model = Book

        fields = ['id', 'author_email', 'name']
        fields_display = {
            'author_email': 'Author email',
            'name': 'Book name',
        }

class GenericImportBookAdmin(GenericImportMixin, admin.ModelAdmin):
    list_filter = ['categories', 'author']

    import_resource_class = BookImportResource




admin.site.register(Book, BookAdmin)
admin.site.register(Category, CategoryAdmin)
admin.site.register(SomeBook, GenericImportBookAdmin)
admin.site.register(Author)
