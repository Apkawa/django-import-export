from __future__ import with_statement
import hashlib
import json
import warnings

import tempfile
from datetime import datetime

from django.contrib import admin
from django.utils.encoding import smart_str
from django.utils.translation import ugettext_lazy as _
from django.conf.urls import patterns, url
from django.template.response import TemplateResponse
from django.contrib import messages
from django.http import (HttpResponseRedirect,
                         HttpResponse,
                         HttpResponseForbidden)
from django.core.urlresolvers import reverse

from .forms import (
    ImportForm,
    ConfirmImportForm,
    ExportForm,
    PreImportForm)
from .resources import (
    modelresource_factory,
    )
from .formats import base_formats

try:
    from django.utils.encoding import force_text
except ImportError:
    from django.utils.encoding import force_unicode as force_text


#: import / export formats
DEFAULT_FORMATS = (
    base_formats.CSV,
    base_formats.XLS,
    base_formats.TSV,
    base_formats.ODS,
    base_formats.JSON,
    base_formats.YAML,
    base_formats.HTML,
)


class ImportMixin(object):
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

    pattern_prefix = ''

    def get_urls(self):
        urls = super(ImportMixin, self).get_urls()
        info = self.model._meta.app_label, self.model._meta.module_name
        my_urls = patterns(
            '',
            url(r'^process_import/$'.format(self.pattern_prefix),
                self.admin_site.admin_view(self.process_import),
                name='%s_%s_process_import' % info),
            url(r'^{}import/$'.format(self.pattern_prefix),
                self.admin_site.admin_view(self.import_action),
                name='%s_%s_import' % info),
        )
        return my_urls + urls

    def get_resource_class(self):
        warnings.warn("Deprecation", DeprecationWarning)
        if not self.resource_class:
            return modelresource_factory(self.model)
        else:
            return self.resource_class

    def get_import_resource_class(self):
        if not self.import_resource_class:
            return modelresource_factory(self.model)
        else:
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
            import_file = open(confirm_form.cleaned_data['import_file_name'],
                input_format.get_read_mode())
            data = import_file.read()
            if not input_format.is_binary() and self.from_encoding:
                data = force_text(data, self.from_encoding)
            dataset = input_format.create_dataset(data)

            resource.import_data(dataset, dry_run=False,
                raise_errors=True)

            success_message = _('Import finished')
            messages.success(request, success_message)
            import_file.close()

            url = reverse('admin:%s_%s_changelist' %
                          (opts.app_label, opts.module_name),
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
            with tempfile.NamedTemporaryFile(delete=False) as uploaded_file:
                for chunk in import_file.chunks():
                    uploaded_file.write(chunk)

            # then read the file, using the proper format-specific mode
            with open(uploaded_file.name,
                    input_format.get_read_mode()) as uploaded_import_file:
                # warning, big files may exceed memory
                data = uploaded_import_file.read()
                if not input_format.is_binary() and self.from_encoding:
                    data = force_text(data, self.from_encoding)
                dataset = input_format.create_dataset(data)
                result = resource.import_data(dataset, dry_run=True,
                    raise_errors=False)

            context['result'] = result

            if not result.has_errors():
                context['confirm_form'] = ConfirmImportForm(initial={
                    'import_file_name': uploaded_file.name,
                    'input_format': form.cleaned_data['input_format'],
                })

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
        return hashlib.sha1('|'.join(headers)).hexdigest()

    def get_urls(self):
        info = self.model._meta.app_label, self.model._meta.module_name
        urls = ImportMixin.get_urls(self)
        my_urls = patterns(
            '',
            url(r'^{}pre_import/$'.format(self.pattern_prefix),
                self.admin_site.admin_view(self.pre_import_action),
                name='%s_%s_pre_import' % info),
        )
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
        result in errors.  If there where no error, save the the user
        uploaded file to a local temp file that will be used by
        'process_import' for the actual import.
        '''
        resource = self.get_import_resource_class()()
        context = {}
        confirm_form = PreImportForm(request.POST)

        if confirm_form.is_valid():
            import_formats = self.get_import_formats()
            input_format = import_formats[
                int(confirm_form.cleaned_data['input_format'])
            ]()
            import_file = open(confirm_form.cleaned_data['import_file_name'],
                input_format.get_read_mode())
            import_rule = confirm_form.cleaned_data['import_rule']

            data = import_file.read()
            if not input_format.is_binary() and self.from_encoding:
                data = smart_str(data, self.from_encoding)
            dataset = input_format.create_dataset(data)
            self.pre_convert_dataset(dataset, import_rule, **kwargs)
            dataset = self.convert_dataset_by_rule(dataset, import_rule,
                **kwargs)
            self.post_convert_dataset(dataset, import_rule, **kwargs)

            with tempfile.NamedTemporaryFile(delete=False) as uploaded_file:
                uploaded_file.write(
                    getattr(dataset, input_format.get_format().title))

            result = resource.import_data(dataset, dry_run=True,
                raise_errors=False)

            context['result'] = result

            if not result.has_errors():
                context['confirm_form'] = ConfirmImportForm(initial={
                    'import_file_name': uploaded_file.name,
                    'input_format': confirm_form.cleaned_data['input_format'],
                })

            context['opts'] = self.model._meta
            context['fields'] = [f.column_name for f in resource.get_fields()]
            context['predefined_field_rules'] = self.predefined_field_rules

            return TemplateResponse(request, [self.import_template_name],
                context, current_app=self.admin_site.name)

        return HttpResponseForbidden()

    def pre_import_action(self, request, *args, **kwargs):
        """

        :return:
        """
        resource = self.get_import_resource_class()()

        context = {}

        import_formats = self.get_import_formats()
        form = ImportForm(import_formats,
            request.POST or None,
            request.FILES or None,
        )

        if request.POST and form.is_valid():
            input_format = import_formats[
                int(form.cleaned_data['input_format'])
            ]()
            import_file = form.cleaned_data['import_file']
            # first always write the uploaded file to disk as it may be a
            # memory file or else based on settings upload handlers
            with tempfile.NamedTemporaryFile(delete=False) as uploaded_file:
                for chunk in import_file.chunks():
                    uploaded_file.write(chunk)

            # then read the file, using the proper format-specific mode
            with open(uploaded_file.name,
                    input_format.get_read_mode()) as uploaded_import_file:
                # warning, big files may exceed memory
                data = uploaded_import_file.read()

            if not input_format.is_binary() and self.from_encoding:
                data = smart_str(data, self.from_encoding)

            dataset = input_format.create_dataset(data)
            dataset.headers = map(smart_str, dataset.headers)

            context['dataset'] = dataset
            context['header_hash'] = self.header_hash(dataset.headers)

            context['confirm_form'] = PreImportForm(initial={
                'import_file_name': uploaded_file.name,
                'input_format': form.cleaned_data['input_format'],
            })

        predefined_field_rules = self.get_predefined_field_rules_json_map()
        context["choice_fields"] = resource.get_fields_display_map()
        context['predefined_field_rules'] = predefined_field_rules

        context['form'] = form
        context['opts'] = self.model._meta
        context['fields'] = [f.column_name for f in resource.get_fields()]
        context['media'] = self.media + form.media

        return TemplateResponse(request, [self.pre_import_template_name],
            context, current_app=self.admin_site.name)


class ExportMixin(object):
    """
    Export mixin.
    """
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
        info = self.model._meta.app_label, self.model._meta.module_name
        my_urls = patterns(
            '',
            url(r'^export/$',
                self.admin_site.admin_view(self.export_action),
                name='%s_%s_export' % info),
        )
        return my_urls + urls

    def get_resource_class(self):
        warnings.warn("Deprecation", DeprecationWarning)
        if not self.resource_class:
            return modelresource_factory(self.model)
        else:
            return self.resource_class

    def get_export_resource_class(self):
        """
        Returns ResourceClass to use for export.
        """
        return self.get_resource_class()

    def get_export_resource_class(self):
        if not self.export_resource_class:
            return modelresource_factory(self.model)
        else:
            return self.export_resource_class

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

        return cl.query_set

    def export_action(self, request, *args, **kwargs):
        formats = self.get_export_formats()
        form = ExportForm(formats, request.POST or None)
        if form.is_valid():
            file_format = formats[
                int(form.cleaned_data['file_format'])
            ]()

            resource_class = self.get_export_resource_class()
            resource_class = self.get_export_resource_class()
            queryset = self.get_export_queryset(request)
            data = resource_class().export(queryset)
            response = HttpResponse(
                file_format.export_data(data),
                mimetype='application/octet-stream',
            )
            response['Content-Disposition'] = 'attachment; filename=%s' % (
                self.get_export_filename(file_format),
            )
            return response

        context = {}
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


class ImportExportModelAdmin(ImportExportMixin, admin.ModelAdmin):
    """
    Subclass of ModelAdmin with import/export functionality.
    """
