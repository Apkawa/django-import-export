from __future__ import with_statement

import hashlib
import importlib
import json
from datetime import datetime

import django
from django.conf import settings
from django.conf.urls import url
from django.contrib import admin
from django.contrib import messages
from django.contrib.admin.models import LogEntry, ADDITION, CHANGE, DELETION
from django.contrib.contenttypes.models import ContentType
from django.core.urlresolvers import reverse
from django.http import (HttpResponseRedirect,
                         HttpResponse,
                         HttpResponseForbidden)
from django.template.response import TemplateResponse
from django.utils import six
from django.utils.encoding import smart_str
from django.utils.translation import ugettext_lazy as _

from .formats import base_formats
from .forms import (
    ImportForm,
    ConfirmImportForm,
    ExportForm,
    export_action_form_factory,
    PreImportForm
)
from .resources import (
    modelresource_factory,
)
from .results import RowResult
from .tmp_storages import TempFolderStorage

try:
    from django.utils.encoding import force_text
except ImportError:
    from django.utils.encoding import force_unicode as force_text

SKIP_ADMIN_LOG = getattr(settings, 'IMPORT_EXPORT_SKIP_ADMIN_LOG', False)
TMP_STORAGE_CLASS = getattr(settings, 'IMPORT_EXPORT_TMP_STORAGE_CLASS',
        TempFolderStorage)
if isinstance(TMP_STORAGE_CLASS, six.string_types):
    try:
        # Nod to tastypie's use of importlib.
        parts = TMP_STORAGE_CLASS.split('.')
        module_path, class_name = '.'.join(parts[:-1]), parts[-1]
        module = importlib.import_module(module_path)
        TMP_STORAGE_CLASS = getattr(module, class_name)
    except ImportError as e:
        msg = "Could not import '%s' for import_export setting 'IMPORT_EXPORT_TMP_STORAGE_CLASS'" % TMP_STORAGE_CLASS
        raise ImportError(msg)

#: import / export formats
DEFAULT_FORMATS = (
    base_formats.CSV,
    base_formats.XLS,
    base_formats.XLSX,
    base_formats.TSV,
    base_formats.ODS,
    base_formats.JSON,
    base_formats.YAML,
    base_formats.HTML,
)


class ImportExportMixinBase(object):
    def get_model_info(self):
        # module_name is renamed to model_name in Django 1.8
        app_label = self.model._meta.app_label
        try:
            return (app_label, self.model._meta.model_name,)
        except AttributeError:
            return (app_label, self.model._meta.module_name,)


class ImportMixin(ImportExportMixinBase):
    """
    Import mixin.
    """

    #: template for change_list view
    change_list_template = 'admin/import_export/change_list_import.html'
    #: template for import view
    import_template_name = 'admin/import_export/import.html'

    #: resource class
    resource_class = None

    #: import resource_class
    import_resource_class = None

    #: available import formats
    formats = DEFAULT_FORMATS
    #: import data encoding
    from_encoding = "utf-8"
    skip_admin_log = None
    # storage class for saving temporary files
    tmp_storage_class = None

    def get_skip_admin_log(self):
        if self.skip_admin_log is None:
            return SKIP_ADMIN_LOG
        else:
            return self.skip_admin_log

    def get_tmp_storage_class(self):
        if self.tmp_storage_class is None:
            return TMP_STORAGE_CLASS
        else:
            return self.tmp_storage_class

    pattern_prefix = ''

    def get_urls(self):
        urls = super(ImportMixin, self).get_urls()
        info = self.get_model_info()
        my_urls = [
            url(r'^process_import/$',
                    self.admin_site.admin_view(self.process_import),
                    name='%s_%s_process_import' % info),
            url(r'^{}import/$'.format(self.pattern_prefix),
                    self.admin_site.admin_view(self.import_action),
                    name='%s_%s_import' % info),
        ]
        return my_urls + urls

    def get_resource_class(self):
        if not self.resource_class:
            return modelresource_factory(self.model)
        else:
            return self.resource_class

    def get_import_resource_class(self):
        """
        Returns ResourceClass to use for export.
        """
        if not self.import_resource_class:
            return self.get_resource_class()
        return self.import_resource_class

    def get_import_formats(self):
        """
        Returns available import formats.
        """
        return [f for f in self.formats if f().can_import()]

    def process_import(self, request, *args, **kwargs):
        '''
        Perform the actual import action (after the user has confirmed he
        wishes to import)
        '''
        opts = self.model._meta
        resource = self.get_import_resource_class()()

        confirm_form = ConfirmImportForm(request.POST)
        if confirm_form.is_valid():
            import_formats = self.get_import_formats()
            input_format = import_formats[
                int(confirm_form.cleaned_data['input_format'])
            ]()
            tmp_storage = self.get_tmp_storage_class()(name=confirm_form.cleaned_data['import_file_name'])
            data = tmp_storage.read(input_format.get_read_mode())
            if not input_format.is_binary() and self.from_encoding:
                data = force_text(data, self.from_encoding)
            dataset = input_format.create_dataset(data)

            result = resource.import_data(dataset, dry_run=False,
                    raise_errors=True,
                    file_name=confirm_form.cleaned_data['original_file_name'],
                    user=request.user)

            if not self.get_skip_admin_log():
                # Add imported objects to LogEntry
                logentry_map = {
                    RowResult.IMPORT_TYPE_NEW: ADDITION,
                    RowResult.IMPORT_TYPE_UPDATE: CHANGE,
                    RowResult.IMPORT_TYPE_DELETE: DELETION,
                }
                content_type_id = ContentType.objects.get_for_model(self.model).pk
                for row in result:
                    if row.import_type != row.IMPORT_TYPE_SKIP:
                        LogEntry.objects.log_action(
                                user_id=request.user.pk,
                                content_type_id=content_type_id,
                                object_id=row.object_id,
                                object_repr=row.object_repr,
                                action_flag=logentry_map[row.import_type],
                                change_message="%s through import_export" % row.import_type,
                        )

            success_message = _('Import finished')
            messages.success(request, success_message)
            tmp_storage.remove()

            url = reverse('admin:%s_%s_changelist' % self.get_model_info(),
                    current_app=self.admin_site.name)
            return HttpResponseRedirect(url)
        return HttpResponseForbidden()

    def import_action(self, request, *args, **kwargs):
        '''
        Perform a dry_run of the import to make sure the import will not
        result in errors.  If there where no error, save the user
        uploaded file to a local temp file that will be used by
        'process_import' for the actual import.
        '''
        resource = self.get_import_resource_class()()

        context = {}

        import_formats = self.get_import_formats()
        form = ImportForm(import_formats,
                request.POST or None,
                request.FILES or None)

        if request.POST and form.is_valid():
            input_format = import_formats[
                int(form.cleaned_data['input_format'])]()
            import_file = form.cleaned_data['import_file']
            # first always write the uploaded file to disk as it may be a
            # memory file or else based on settings upload handlers
            tmp_storage = self.get_tmp_storage_class()()
            data = bytes()
            for chunk in import_file.chunks():
                data += chunk

            tmp_storage.save(data, input_format.get_read_mode())

            # then read the file, using the proper format-specific mode
            # warning, big files may exceed memory
            try:
                data = tmp_storage.read(input_format.get_read_mode())
                if not input_format.is_binary() and self.from_encoding:
                    data = force_text(data, self.from_encoding)
                dataset = input_format.create_dataset(data)
            except UnicodeDecodeError as e:
                return HttpResponse(_(u"<h1>Imported file is not in unicode: %s</h1>" % e))
            except Exception as e:
                return HttpResponse(_(u"<h1>%s encountred while trying to read file: %s</h1>" % (type(e).__name__, e)))
            result = resource.import_data(dataset, dry_run=True,
                    raise_errors=False,
                    file_name=import_file.name,
                    user=request.user)

            context['result'] = result

            if not result.has_errors():
                context['confirm_form'] = ConfirmImportForm(initial={
                    'import_file_name': tmp_storage.name,
                    'original_file_name': import_file.name,
                    'input_format': form.cleaned_data['input_format'],
                })

        if django.VERSION >= (1, 8, 0):
            context.update(self.admin_site.each_context(request))
        elif django.VERSION >= (1, 7, 0):
            context.update(self.admin_site.each_context())

        context['form'] = form
        context['opts'] = self.model._meta
        context['fields'] = [f.column_name for f in resource.get_fields()]
        context['media'] = self.media + form.media

        return TemplateResponse(request, [self.import_template_name],
                context, current_app=self.admin_site.name)


class GenericImportMixin(ImportMixin):
    '''
    Add import mapping file step
    '''
    change_list_template = 'admin/import_export/generic/change_list.html'

    pre_import_template_name = 'admin/import_export/generic/pre_import.html'
    import_template_name = 'admin/import_export/generic/import.html'

    #: predefined field rules for generic format. as example [('Primary Key', 'id'), ('Book name', 'name'), ('author email', 'author_email')]
    predefined_field_rules = None

    @staticmethod
    def header_hash(headers):
        return hashlib.sha1('|'.join(map(smart_str, headers))).hexdigest()

    def get_urls(self):
        info = self.get_model_info()
        urls = ImportMixin.get_urls(self)
        my_urls = [
            url(r'^{}pre_import/$'.format(self.pattern_prefix),
                    self.admin_site.admin_view(self.pre_import_action),
                    name='%s_%s_pre_import' % info),
        ]
        return my_urls + urls

    def get_predefined_field_rules_json_map(self):
        '''
        return {'sha1hash of headers': 'jsob_string'}

        '''
        predefined_rules = {}
        if self.predefined_field_rules is None:
            return predefined_rules
        for rule in self.predefined_field_rules:
            rule_hash = self.header_hash(
                    headers=[header for header, field in rule])
            predefined_rules[rule_hash] = json.dumps(dict(rule))
        return predefined_rules

    def pre_convert_dataset(self, dataset, rule, **kwargs):
        """

        :param dataset: Dataset
        :param rule: {
            'Column name': 'resource_field',
        }
        :return:
        """

    def convert_dataset_by_rule(self, dataset, rule, **kwargs):
        """

        :param dataset: Dataset
        :param rule: {
            'Column name': 'resource_field',
        }
        :return:
        """
        rule = {smart_str(k): smart_str(v) for k, v in rule.items()}
        resource = self.get_import_resource_class()()
        resource_fields = resource.fields.keys()

        dataset.headers = map(smart_str, dataset.headers)
        delete_headers = [h for h in dataset.headers if h not in rule and h not in resource_fields]

        for header in delete_headers:
            del dataset[header]
        new_headers = []
        for h in dataset.headers:
            if h in rule:
                new_headers.append(rule[h])
            elif h in resource_fields:
                new_headers.append(h)
        dataset.headers = new_headers

        return dataset

    def post_convert_dataset(self, dataset, rule, **kwargs):
        """

        :param dataset: Dataset
        :param rule: {
            'Column name': 'resource_field',
        }
        :return:
        """
        resource = self.get_import_resource_class()()

        empty_fields = set(resource.fields.keys()) ^ set(dataset.headers)

        for f in empty_fields:
            dataset.insert_col(0, (lambda row: ''), header=f)

    def import_action(self, request, *args, **kwargs):
        '''
        Perform a dry_run of the import to make sure the import will not
        result in errors.  If there where no error, save the user
        uploaded file to a local temp file that will be used by
        'process_import' for the actual import.
        '''
        resource = self.get_import_resource_class()()

        context = {}

        import_formats = self.get_import_formats()
        form = PreImportForm(request.POST or None,
                             request.FILES or None)

        if request.POST and form.is_valid():
            input_format = import_formats[
                int(form.cleaned_data['input_format'])]()

            import_rule = form.cleaned_data['import_rule']
            # first always write the uploaded file to disk as it may be a
            # memory file or else based on settings upload handlers
            tmp_storage = self.get_tmp_storage_class()(name=form.cleaned_data['import_file_name'])

            # then read the file, using the proper format-specific mode
            # warning, big files may exceed memory
            try:
                data = tmp_storage.read(input_format.get_read_mode())
                if not input_format.is_binary() and self.from_encoding:
                    data = force_text(data, self.from_encoding)
                dataset = input_format.create_dataset(data)
            except UnicodeDecodeError as e:
                return HttpResponse(_(u"<h1>Imported file is not in unicode: %s</h1>" % e))
            except Exception as e:
                return HttpResponse(_(u"<h1>%s encountred while trying to read file: %s</h1>" % (type(e).__name__, e)))

            self.pre_convert_dataset(dataset, import_rule, **kwargs)
            dataset = self.convert_dataset_by_rule(dataset, import_rule,
                    **kwargs)
            self.post_convert_dataset(dataset, import_rule, **kwargs)

            result = resource.import_data(dataset, dry_run=True,
                    raise_errors=False,
                    file_name=form.cleaned_data['original_file_name'],
                    user=request.user)

            context['result'] = result

            if not result.has_errors():
                tmp_storage = self.get_tmp_storage_class()()
                tmp_storage.save(input_format.export_data(dataset), input_format.get_read_mode())

                context['confirm_form'] = ConfirmImportForm(initial={
                    'import_file_name': tmp_storage.name,
                    'original_file_name': form.cleaned_data['original_file_name'],
                    'input_format': form.cleaned_data['input_format'],
                })

        if django.VERSION >= (1, 8, 0):
            context.update(self.admin_site.each_context(request))
        elif django.VERSION >= (1, 7, 0):
            context.update(self.admin_site.each_context())

        context['form'] = form
        context['opts'] = self.model._meta
        context['fields'] = [f.column_name for f in resource.get_fields()]
        context['media'] = self.media + form.media

        return TemplateResponse(request, [self.import_template_name],
                context, current_app=self.admin_site.name)

    def pre_import_action(self, request, *args, **kwargs):
        '''
        '''
        resource = self.get_import_resource_class()()

        context = {}

        import_formats = self.get_import_formats()
        form = ImportForm(import_formats,
                request.POST or None,
                request.FILES or None)

        if request.POST and form.is_valid():
            input_format = import_formats[
                int(form.cleaned_data['input_format'])]()
            import_file = form.cleaned_data['import_file']
            # first always write the uploaded file to disk as it may be a
            # memory file or else based on settings upload handlers
            tmp_storage = self.get_tmp_storage_class()()
            data = bytes()
            for chunk in import_file.chunks():
                data += chunk

            tmp_storage.save(data, input_format.get_read_mode())

            # then read the file, using the proper format-specific mode
            # warning, big files may exceed memory
            try:
                data = tmp_storage.read(input_format.get_read_mode())
                if not input_format.is_binary() and self.from_encoding:
                    data = force_text(data, self.from_encoding)
                dataset = input_format.create_dataset(data)
            except UnicodeDecodeError as e:
                return HttpResponse(_(u"<h1>Imported file is not in unicode: %s</h1>" % e))
            except Exception as e:
                return HttpResponse(_(u"<h1>%s encountred while trying to read file: %s</h1>" % (type(e).__name__, e)))

            context['dataset'] = dataset
            context['header_hash'] = self.header_hash(dataset.headers)

            context['confirm_form'] = PreImportForm(initial={
                'import_file_name': tmp_storage.name,
                'original_file_name': import_file.name,
                'input_format': form.cleaned_data['input_format'],
            })

        if django.VERSION >= (1, 8, 0):
            context.update(self.admin_site.each_context(request))
        elif django.VERSION >= (1, 7, 0):
            context.update(self.admin_site.each_context())

        context["choice_fields"] = resource.get_fields_display()
        context['predefined_field_rules'] = self.get_predefined_field_rules_json_map()

        context['form'] = form
        context['opts'] = self.model._meta
        context['fields'] = [f.column_name for f in resource.get_fields()]
        context['media'] = self.media + form.media

        return TemplateResponse(request, [self.pre_import_template_name],
                context, current_app=self.admin_site.name)


class ExportMixin(ImportExportMixinBase):
    """
    Export mixin.
    """
    #: export resource class
    export_resource_class = None

    #: resource class
    resource_class = None
    #: template for change_list view
    change_list_template = 'admin/import_export/change_list_export.html'
    #: template for export view
    export_template_name = 'admin/import_export/export.html'
    #: available import formats
    formats = DEFAULT_FORMATS
    #: export data encoding
    to_encoding = "utf-8"

    def get_urls(self):
        urls = super(ExportMixin, self).get_urls()
        my_urls = [
            url(r'^export/$',
                    self.admin_site.admin_view(self.export_action),
                    name='%s_%s_export' % self.get_model_info()),
        ]
        return my_urls + urls

    def get_resource_class(self):
        if not self.resource_class:
            return modelresource_factory(self.model)
        else:
            return self.resource_class

    def get_export_resource_class(self):
        """
        Returns ResourceClass to use for export.
        """
        return self.export_resource_class or self.get_resource_class()

    def get_export_formats(self):
        """
        Returns available import formats.
        """
        return [f for f in self.formats if f().can_export()]

    def get_export_filename(self, file_format):
        date_str = datetime.now().strftime('%Y-%m-%d')
        filename = "%s-%s.%s" % (self.model.__name__,
                                 date_str,
                                 file_format.get_extension())
        return filename

    def get_export_queryset(self, request):
        """
        Returns export queryset.

        Default implementation respects applied search and filters.
        """
        # copied from django/contrib/admin/options.py
        list_display = self.get_list_display(request)
        list_display_links = self.get_list_display_links(request, list_display)

        ChangeList = self.get_changelist(request)
        cl = ChangeList(request, self.model, list_display,
                list_display_links, self.list_filter,
                self.date_hierarchy, self.search_fields,
                self.list_select_related, self.list_per_page,
                self.list_max_show_all, self.list_editable,
                self)

        # query_set has been renamed to queryset in Django 1.8
        try:
            return cl.queryset
        except AttributeError:
            return cl.query_set

    def get_export_data(self, file_format, queryset):
        """
        Returns file_format representation for given queryset.
        """
        resource_class = self.get_export_resource_class()
        data = resource_class().export(queryset)
        export_data = file_format.export_data(data)
        return export_data

    def export_action(self, request, *args, **kwargs):
        formats = self.get_export_formats()
        form = ExportForm(formats, request.POST or None)
        if form.is_valid():
            file_format = formats[
                int(form.cleaned_data['file_format'])
            ]()

            queryset = self.get_export_queryset(request)
            export_data = self.get_export_data(file_format, queryset)
            content_type = file_format.get_content_type()
            # Django 1.7 uses the content_type kwarg instead of mimetype
            try:
                response = HttpResponse(export_data, content_type=content_type)
            except TypeError:
                response = HttpResponse(export_data, mimetype=content_type)
            response['Content-Disposition'] = 'attachment; filename=%s' % (
                self.get_export_filename(file_format),
            )
            return response

        context = {}

        if django.VERSION >= (1, 8, 0):
            context.update(self.admin_site.each_context(request))
        elif django.VERSION >= (1, 7, 0):
            context.update(self.admin_site.each_context())

        context['form'] = form
        context['opts'] = self.model._meta
        context['media'] = self.media + form.media
        return TemplateResponse(request, [self.export_template_name],
                context, current_app=self.admin_site.name)


class ImportExportMixin(ImportMixin, ExportMixin):
    """
    Import and export mixin.
    """
    #: template for change_list view
    change_list_template = 'admin/import_export/change_list_import_export.html'


class GenericImportExportMixin(GenericImportMixin, ExportMixin):
    """
    Generic import and export mixin.
    """
    #: template for change_list view
    change_list_template = 'admin/import_export/generic/change_list_import_export.html'


class ImportExportModelAdmin(ImportExportMixin, admin.ModelAdmin):
    """
    Subclass of ModelAdmin with import/export functionality.
    """


class ExportActionModelAdmin(ExportMixin, admin.ModelAdmin):
    """
    Subclass of ModelAdmin with export functionality implemented as an
    admin action.
    """

    # Don't use custom change list template.
    change_list_template = None

    def __init__(self, *args, **kwargs):
        """
        Adds a custom action form initialized with the available export
        formats.
        """
        choices = []
        formats = self.get_export_formats()
        if formats:
            choices.append(('', '---'))
            for i, f in enumerate(formats):
                choices.append((str(i), f().get_title()))

        self.action_form = export_action_form_factory(choices)
        super(ExportActionModelAdmin, self).__init__(*args, **kwargs)

    def export_admin_action(self, request, queryset):
        """
        Exports the selected rows using file_format.
        """
        export_format = request.POST.get('file_format')

        if not export_format:
            messages.warning(request, _('You must select an export format.'))
        else:
            formats = self.get_export_formats()
            file_format = formats[int(export_format)]()

            export_data = self.get_export_data(file_format, queryset)
            content_type = file_format.get_content_type()
            # Django 1.7 uses the content_type kwarg instead of mimetype
            try:
                response = HttpResponse(export_data, content_type=content_type)
            except TypeError:
                response = HttpResponse(export_data, mimetype=content_type)
            response['Content-Disposition'] = 'attachment; filename=%s' % (
                self.get_export_filename(file_format),
            )
            return response

    export_admin_action.short_description = _(
            'Export selected %(verbose_name_plural)s')

    actions = [export_admin_action]


class ImportExportActionModelAdmin(ImportMixin, ExportActionModelAdmin):
    """
    Subclass of ExportActionModelAdmin with import/export functionality.
    Export functionality is implemented as an admin action.
    """
