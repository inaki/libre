from __future__ import absolute_import

import datetime
import hashlib
from itertools import islice
import logging
import re
import string
import struct
import urllib2

from django.contrib.auth.models import Group
from django.core.exceptions import FieldError, ValidationError
from django.db import models, transaction
from django.http import Http404
from django.utils.translation import ugettext_lazy as _
from django.utils.timezone import now
from django.template.defaultfilters import slugify, truncatechars

import fiona
from model_utils.managers import InheritanceManager
from picklefield.fields import PickledObjectField
from pyproj import Proj, transform
import requests
from shapely import geometry
from suds.client import Client
import xlrd

from icons.models import Icon
from lock_manager import Lock, LockError
from origins.models import Origin

from .exceptions import LIBREAPIError, SourceFileError
from .job_processing import Job
from .literals import (DEFAULT_LIMIT, DEFAULT_SHEET, DATA_TYPE_CHOICES,
    RENDERER_BROWSEABLE_API, RENDERER_JSON, RENDERER_XML, RENDERER_YAML, RENDERER_LEAFLET)
from .managers import SourceAccessManager
from .query import Query
from .utils import DATA_TYPE_FUNCTIONS, UnicodeReader, parse_range

HASH_FUNCTION = lambda x: hashlib.sha256(x).hexdigest()
logger = logging.getLogger(__name__)


class Source(models.Model):
    source_type = _('Base source class')
    renderers = (RENDERER_JSON, RENDERER_BROWSEABLE_API, RENDERER_XML, RENDERER_YAML)
    supports_versioning = False

    name = models.CharField(max_length=128, verbose_name=_('name'), help_text=('Human readable name for this source.'))
    slug = models.SlugField(unique=True, blank=True, max_length=48, verbose_name=_('slug'), help_text=('URL friendly description of this source. If none is specified the name will be used.'))
    description = models.TextField(blank=True, verbose_name=_('description'))
    published = models.BooleanField(default=False, verbose_name=_('published'))
    allowed_groups = models.ManyToManyField(Group, verbose_name=_('allowed groups'), blank=True, null=True)
    limit = models.PositiveIntegerField(default=DEFAULT_LIMIT, verbose_name=_('limit'), help_text=_('Maximum number of items to show when all items are requested.'))
    origin = models.ForeignKey(Origin, verbose_name=_('origin'))

    objects = InheritanceManager()
    allowed = SourceAccessManager()

    def check_source_data(self):
        try:
            lock_id = u'check_source_data-%d' % self.pk
            logger.debug('trying to acquire lock: %s' % lock_id)
            lock = Lock.acquire_lock(lock_id, 60)
            logger.debug('acquired lock: %s' % lock_id)
            try:
                self.check_origin_data()
            except Exception as exception:
                logger.debug('unhandled exception: %s' % exception)
                raise
            finally:
                lock.release()
        except LockError:
            logger.debug('unable to obtain lock')
            pass

    def check_origin_data(self):
        self.origin_subclass_instance = Origin.objects.get_subclass(pk=self.origin.pk)
        self.origin_subclass_instance.copy_data()

        logger.debug('new_hash: %s' % self.origin_subclass_instance.new_hash)

        try:
            source_data_version = self.versions.get(checksum=self.origin_subclass_instance.new_hash)
        except SourceDataVersion.DoesNotExist:
            source_data_version = SourceDataVersion.objects.create(source=self, checksum=self.origin_subclass_instance.new_hash)
            job = Job(target=self.import_origin_data, args=[source_data_version])
            job.submit()
            logger.debug('launching import job: %s' % job)
        else:
            source_data_version.active = True
            source_data_version.save()

    @transaction.commit_on_success
    def import_origin_data(self, source_data_version):
        source_data_version = SourceDataVersion.objects.get(pk=source_data_version.pk)

        self.get_regex_maps()

        logger.debug('importing rows')

        for row_id, row in enumerate(self._get_rows(), 1):
            SourceData.objects.create(source_data_version=source_data_version, row_id=row_id, row=dict(row, **{'_id': row_id}))

        logger.debug('finished importing rows')

        source_data_version.ready = True
        source_data_version.active = True
        source_data_version.save()

        self.origin_subclass_instance.discard_copy()
        logger.debug('exiting')

    class AlwaysFalseSearch(object):
        def search(self, string):
            return False

    class AlwaysTrueSearch(object):
        def search(self, string):
            return True

    def get_regex_maps(self):
        self.skip_regex_map = {}
        for name, skip_regex in self.columns.values_list('name', 'skip_regex'):
            if skip_regex:
                self.skip_regex_map[name] = re.compile(skip_regex)
            else:
                self.skip_regex_map[name] = self.__class__.AlwaysFalseSearch()

        self.import_regex_map = {}
        for name, import_regex in self.columns.values_list('name', 'import_regex'):
            if import_regex:
                self.import_regex_map[name] = re.compile(import_regex)
            else:
                self.import_regex_map[name] = self.__class__.AlwaysTrueSearch()

    def process_regex(self, row):
        skip_result = [True if self.skip_regex_map[name].search(unicode(value)) else False for name, value in row.items() if name in self.skip_regex_map]
        import_result = [True if self.import_regex_map[name].search(unicode(value)) else False for name, value in row.items() if name in self.import_regex_map]

        return all(cell_skip is False for cell_skip in skip_result) and all(import_result)

    def get_column_names(self):
        if self.columns.count():
            return self.columns.all().values_list('name', flat=True)
        else:
            return string.ascii_uppercase

    def __unicode__(self):
        return self.name

    def clean(self):
        """Validation method, to avoid adding a source without a slug value"""
        if not self.slug:
            self.slug = slugify(self.name)

    @models.permalink
    def get_absolute_url(self):
        return ('source-detail', [self.pk])

    class Meta:
        verbose_name = _('source')
        verbose_name_plural = _('sources')
        ordering = ['name', 'slug']

    #########

    def analyze_request(self, parameters=None):
        kwargs = {}
        if not parameters:
            parameters = {}
        else:
            for i in parameters:
                if not i.startswith('_'):
                    kwargs[i] = parameters[i]

        timestamp = parameters.get('_timestamp', None)

        return timestamp, parameters

    #def import_data(self, source_data_version):
    #    self.compute_new_name_map()

    #def get_type(self):
    #    return self.__class__.source_type

    def get_one(self, id, parameters=None):
        # ID are all base 1
        if id == 0:
            raise LIBREAPIError('Invalid ID; IDs are base 1')

        # TODO: return a proper response when no sourcedataversion is found
        timestamp, parameters = self.analyze_request(parameters)
        if timestamp:
            source_data_version = self.versions.get(timestamp=timestamp)
        else:
            source_data_version = self.versions.get(active=True)

        try:
            return SourceData.objects.get(source_data_version=source_data_version, row_id=id).row
        except SourceData.DoesNotExist:
            raise Http404

    #def get_all(self, timestamp=None, parameters=None, get_id=None):
    #    if get_id:
    #        return islice(self._get_all(timestamp=timestamp, parameters=parameters, get_id=get_id), get_id - 1, get_id)
    #    else:
    #        return islice(self._get_all(timestamp=timestamp, parameters=parameters, get_id=get_id), 0, self.limit)

    #def get_one(self, id, timestamp=None, parameters=None):
    #    # ID are all base 1
    #    if id == 0:
    #        raise LIBREAPIError('Invalid ID; IDs are base 1')
    #    return self.get_all(timestamp, parameters)[id - 1]

    def get_base_data(self, source_data_version):
        return (item.row for item in SourceData.objects.filter(source_data_version=source_data_version).iterator())

    def get_all(self, parameters=None):
        initial_datetime = datetime.datetime.now()
        timestamp, parameters = self.analyze_request(parameters)

        if self.supports_versioning:
            try:
                if timestamp:
                    source_data_version = self.versions.get(timestamp=timestamp)
                else:
                    source_data_version = self.versions.get(active=True)
            except SourceDataVersion.DoesNotExist:
                return []
        else:
            source_data_version = None

        self.queryset = self.get_base_data(source_data_version)

        results = Query(self).execute(parameters)
        logger.debug('Elapsed time: %s' % (datetime.datetime.now() - initial_datetime))

        return results

    def get_functions_map(self):
        return dict([(column, DATA_TYPE_FUNCTIONS[data_type]) for column, data_type in self.columns.values_list('name', 'data_type')])

    def compute_new_name_map(self):
        try:
            self.new_name_map = dict(self.columns.values_list('name', 'new_name'))
        except FieldError:
            # This source doesn't support field renaming
            self.new_name_map = {}

    def apply_datatypes(self, properties, functions_map):
        result = {}

        for key, value in properties.items():
            new_name = self.new_name_map.get(key, key)
            try:
                result[new_name] = functions_map[key](value)
            except KeyError:
                # Is not to be converted
                result[new_name] = value
            except ValueError:
                # Fallback for failed conversion
                result[new_name] = value

        return result

    def clear_versions(self):
        for version in self.versions.all():
            version.delete()


class SourceCSV(Source):
    source_type = _('CSV file')

    delimiter = models.CharField(blank=True, max_length=1, default=',', verbose_name=_('delimiter'))
    quote_character = models.CharField(blank=True, max_length=1, verbose_name=_('quote character'))

    def _get_rows(self):
        column_names = self.get_column_names()
        functions_map = self.get_functions_map()

        kwargs = {}
        if self.delimiter:
            kwargs['delimiter'] = str(self.delimiter)
        if self.quote_character:
            kwargs['quotechar'] = str(self.quote_character)

        reader = UnicodeReader(self.origin_subclass_instance.copy_file, **kwargs)

        logger.debug('column_names: %s' % column_names)

        for row_id, row in enumerate(reader, 1):
            row_dict = dict(zip(column_names, row))
            if self.process_regex(row_dict):
                yield self.apply_datatypes(row_dict, functions_map)

    class Meta:
        verbose_name = _('CSV source')
        verbose_name_plural = _('CSV sources')


class SourceFixedWidth(Source):
    source_type = _('Fixed width column file')

    def _get_rows(self):
        column_names = self.get_column_names()
        column_widths = self.columns.all().values_list('size', flat=True)

        fmtstring = ''.join('%ds' % f for f in map(int, column_widths))
        parse = struct.Struct(fmtstring).unpack_from

        functions_map = self.get_functions_map()

        for row_id, row in enumerate(self._file_handle):
            row_dict = dict(zip(column_names, parse(row)))
            if self.process_regex(row_dict):
                yield self.apply_datatypes(row_dict, functions_map)

    class Meta:
        verbose_name = _('Fixed width source')
        verbose_name_plural = _('Fixed width sources')


class SourceSpreadsheet(Source):
    source_type = _('Spreadsheet file')

    sheet = models.CharField(max_length=32, default=DEFAULT_SHEET, verbose_name=_('sheet'), help_text=('Worksheet of the spreadsheet file to use.'))

    def _convert_value(self, item):
        """
        Handle different value types for XLS. Item is a cell object.
        """
        # Types:
        # 0 = empty u''
        # 1 = unicode text
        # 2 = float (convert to int if possible, then convert to string)
        # 3 = date (convert to unambiguous date/time string)
        # 4 = boolean (convert to string "0" or "1")
        # 5 = error (convert from code to error text)
        # 6 = blank u''

        # Thx to Augusto C Men to point fast solution for XLS/XLSX dates
        if item.ctype == 3:  # XL_CELL_DATE:
            try:
                return datetime.datetime(*xlrd.xldate_as_tuple(item.value, self._book.datemode))
            except ValueError:
                # TODO: make toggable
                # Invalid date
                return item.value

        if item.ctype == 2:  # XL_CELL_NUMBER:
            if item.value % 1 == 0:  # integers
                return int(item.value)
            else:
                return item.value

        return item.value

    def _get_rows(self):
        logger.debug('opening workbook')

        self._book = xlrd.open_workbook(file_contents=self.origin_subclass_instance.copy_file.read())

        logger.debug('opening sheet: %s' % self.sheet)

        column_names = self.get_column_names()

        logger.debug('column_names: %s' % column_names)

        try:
            self._sheet = self._book.sheet_by_name(self.sheet)
        except xlrd.XLRDError:
            self._sheet = self._book.sheet_by_index(int(self.sheet))

        parsed_range = xrange(0, self._sheet.nrows)

        for i in parsed_range:
            converted_row = dict(zip(column_names, [self._convert_value(cell) for cell in self._sheet.row(i)]))
            if self.process_regex(converted_row):
                yield converted_row

    class Meta:
        verbose_name = _('spreadsheet source')
        verbose_name_plural = _('spreadsheet sources')


class LeafletMarker(models.Model):
    slug = models.SlugField(blank=True, verbose_name=_(u'slug'), unique=True)
    label = models.CharField(max_length=48, verbose_name=_(u'label'), blank=True)
    icon = models.ForeignKey(Icon, verbose_name=_('icon'), related_name='leafletmarker-icon')
    shadow = models.ForeignKey(Icon, null=True, blank=True, verbose_name=_('shadow'), related_name='leafletmarker-shadow')
    icon_anchor_x = models.IntegerField(verbose_name=_('icon anchor (horizontal)'), default=0)
    icon_anchor_y = models.IntegerField(verbose_name=_('icon anchor (vertical)'), default=0)
    shadow_anchor_x = models.IntegerField(verbose_name=_('shadow anchor (horizontal)'), default=0)
    shadow_anchor_y = models.IntegerField(verbose_name=_('shadow anchor (vertical)'), default=0)
    popup_anchor_x = models.IntegerField(verbose_name=_('popup anchor (horizontal)'), default=0)
    popup_anchor_y = models.IntegerField(verbose_name=_('popup anchor (vertical)'), default=0)

    def __unicode__(self):
        return '%s%s' % (self.slug, ' (%s)' % self.label if self.label else '')

    def clean(self):
        """Validation method, to avoid adding a new marker without a slug value"""
        if not self.slug:
            self.slug = slugify(self.label)

    class Meta:
        verbose_name = _('leaflet marker')
        verbose_name_plural = _('leaflet marker')
        ordering = ['label', 'slug']


class SourceShape(Source):
    source_type = _('Shapefile')
    renderers = (RENDERER_JSON, RENDERER_BROWSEABLE_API, RENDERER_XML, RENDERER_YAML, RENDERER_LEAFLET)

    popup_template = models.TextField(blank=True, verbose_name=_('popup template'), help_text=_('Template for rendering the features when displaying them on a map.'))
    new_projection = models.CharField(max_length=32, blank=True, verbose_name=_('new projection'), help_text=_('Specify the EPSG number of the new projection to transform the geometries, leave blank otherwise.'))
    markers = models.ManyToManyField(LeafletMarker, blank=True, null=True)
    marker_template = models.TextField(blank=True, verbose_name=_('marker template'), help_text=_('Template to determine what marker each respective feature will use.'))
    template_header = models.TextField(blank=True, verbose_name=_('template header'), help_text=_('Place here custom styles, javascript or asset loading.'))

    @staticmethod
    def transform(old_projection, new_projection, geometry, geometry_type=None):
        # TODO: Support all types
        # Point (A single (x, y) tuple) - DONE
        # LineString (A list of (x, y) tuple vertices) - DONE
        # Polygon (A list of rings (each a list of (x, y) tuples)) - DONE
        # MultiPoint (A list of points (each a single (x, y) tuple))- DONE
        # MultiLineString (A list of lines (each a list of (x, y) tuples)) - DONE
        # MultiPolygon (A list of polygons (see above)) - DONE
        # GeometryCollection
        # 3D Point
        # 3D LineString
        # 3D Polygon
        # 3D MultiPoint
        # 3D MultiLineString
        # 3D MultiPolygon
        # 3D GeometryCollection
        if geometry_type:
            coordinates = geometry
        else:
            coordinates = geometry['coordinates']

        if geometry_type == 'Point' or (not geometry_type and geometry['type'] == 'Point'):
            return transform(old_projection, new_projection, *coordinates)
        elif geometry_type == 'LineString' or (not geometry_type and geometry['type'] == 'LineString'):
            result = []
            for x, y in coordinates:
                result.append(transform(old_projection, new_projection, x, y))
            return result
        elif geometry_type == 'Polygon' or (not geometry_type and geometry['type'] == 'Polygon'):
            result = []
            for ring in coordinates:
                element_result = []
                for x, y in ring:
                    element_result.append(transform(old_projection, new_projection, x, y))
                result.append(element_result)
            return result
        elif geometry['type'] == 'MultiPolygon':
            result = []
            for polygon in coordinates:
                result.append(SourceShape.transform(old_projection, new_projection, polygon, geometry_type='Polygon'))
            return result
        elif geometry['type'] == 'MultiPoint':
            result = []
            for point in coordinates:
                result.append(SourceShape.transform(old_projection, new_projection, point, geometry_type='Point'))
            return result
        elif geometry['type'] == 'MultiLineString':
            result = []
            for line in coordinates:
                result.append(SourceShape.transform(old_projection, new_projection, line, geometry_type='LineString'))
            return result
        else:
            # Unsuported geometry type, return coordinates as is
            return geometry['coordinates']

    @transaction.commit_on_success
    def _get_rows(self, source_data_version):
        with fiona.open('/test_uk.shp', vfs='zip://docs/data/test_uk.zip') as source:
        #with fiona.collection(self.path, 'r') as source:
            source_data_version.metadata = source.crs
            if self.new_projection:
                new_projection = Proj(init='epsg:%s' % self.new_projection)
                old_projection = Proj(**source.crs)
            else:
                new_projection = False

            functions_map = self.get_functions_map()

            for feature in source:
                if feature['geometry']:
                    feature['properties'] = Source.add_row_id(self.apply_datatypes(feature.get('properties', {}), functions_map), row_id)

                    if new_projection:
                        feature['geometry']['coordinates'] = SourceShape.transform(old_projection, new_projection, feature['geometry'])

                    feature['geometry'] = geometry.shape(feature['geometry'])
                    yield feature

    class Meta:
        verbose_name = _('shape source')
        verbose_name_plural = _('shape sources')


class SourceDatabase(Source):
    source_type = _('Database')

    def _get_rows(self):
        column_names = self.get_column_names()

        for row in self.origin_subclass_instance.data_iterator:
            yield dict(zip(column_names, row))

    class Meta:
        verbose_name = _('database source')
        verbose_name_plural = _('database sources')


class SourceRESTAPI(Source):
    source_type = _('REST API')

    def _get_rows(self):
        column_names = self.get_column_names()

        for row in self.origin_subclass_instance.data_iterator:
            print 'row', row
            finished_row = dict(zip(column_names, row))
            print 'finished_row', finished_row
            if self.process_regex(finished_row):
                yield finished_row

    class Meta:
        verbose_name = _('REST API source')
        verbose_name_plural = _('REST API sources')


class SourceWS(Source):
    source_type = _('SOAP web service')

    def _get_rows(self):
        for row in self.origin_subclass_instance.data_iterator:
            fields = {}
            for field in self.columns.all():
                fields[field.name] = getattr(row, field.name, field.default)

            yield fields

    class Meta:
        verbose_name = _('web service source')
        verbose_name_plural = _('web service sources')


# Version and data models


class SourceDataVersion(models.Model):
    renderers = (RENDERER_JSON, RENDERER_BROWSEABLE_API, RENDERER_XML, RENDERER_YAML)

    source = models.ForeignKey(Source, verbose_name=_('source'), related_name='versions')
    datetime = models.DateTimeField(default=lambda: now())
    timestamp = models.CharField(blank=True, max_length=20, verbose_name=_('timestamp'))
    # MySQL doesn't like BLOB/TEXT columns used in key specification without a key length; DatabaseError 1170
    checksum = models.CharField(max_length=64, verbose_name=_('checksum'))
    ready = models.BooleanField(default=False, verbose_name=_('ready'))
    active = models.BooleanField(default=False, verbose_name=_('active'))
    metadata = PickledObjectField(blank=True, verbose_name=_('metadata'))

    def save(self, *args, **kwargs):
        self.timestamp = datetime.datetime.strftime(self.datetime, '%Y%m%d%H%M%S%f')
        if self.active:
            SourceDataVersion.objects.filter(source=self.source).update(active=False)
        super(self.__class__, self).save(*args, **kwargs)

    def truncated_checksum(self):
        return truncatechars(self.checksum, 10)

    class Meta:
        verbose_name = _('source data version')
        verbose_name_plural = _('sources data versions')
        unique_together = (('source', 'datetime'), ('source', 'timestamp'), ('source', 'checksum'))


class SourceData(models.Model):
    source_data_version = models.ForeignKey(SourceDataVersion, verbose_name=_('source data version'), related_name='data')
    row = PickledObjectField(verbose_name=_('row'))
    row_id = models.PositiveIntegerField(verbose_name=_('row id'), db_index=True)

    def __unicode__(self):
        return unicode(self.row)

    class Meta:
        verbose_name = _('source data')
        verbose_name_plural = _('sources data')


# Column models


class ColumnBase(models.Model):
    import_column = models.BooleanField(default=True, verbose_name=_('import'))
    name = models.CharField(max_length=32, verbose_name=_('name'))
    default = models.CharField(max_length=32, blank=True, verbose_name=_('default'))

    class Meta:
        abstract = True


class CSVColumn(ColumnBase):
    source = models.ForeignKey(SourceCSV, verbose_name=_('CSV source'), related_name='columns')
    data_type = models.PositiveIntegerField(choices=DATA_TYPE_CHOICES, verbose_name=_('data type'))
    skip_regex = models.TextField(blank=True, verbose_name=_('skip expression'))
    import_regex = models.TextField(blank=True, verbose_name=_('import expression'))

    class Meta:
        verbose_name = _('CSV column')
        verbose_name_plural = _('CSV columns')


class FixedWidthColumn(ColumnBase):
    source = models.ForeignKey(SourceFixedWidth, verbose_name=_('fixed width source'), related_name='columns')
    size = models.PositiveIntegerField(verbose_name=_('size'))
    data_type = models.PositiveIntegerField(choices=DATA_TYPE_CHOICES, verbose_name=_('data type'))

    class Meta:
        verbose_name = _('fixed width column')
        verbose_name_plural = _('fixed width columns')


class SpreadsheetColumn(ColumnBase):
    source = models.ForeignKey(SourceSpreadsheet, verbose_name=_('spreadsheet source'), related_name='columns')
    skip_regex = models.TextField(blank=True, verbose_name=_('skip expression'))
    import_regex = models.TextField(blank=True, verbose_name=_('import expression'))

    class Meta:
        verbose_name = _('spreadsheet column')
        verbose_name_plural = _('spreadsheet columns')


class ShapefileColumn(ColumnBase):
    source = models.ForeignKey(SourceShape, verbose_name=_('shapefile source'), related_name='columns')
    new_name = models.CharField(max_length=32, verbose_name=_('new name'), blank=True)
    data_type = models.PositiveIntegerField(choices=DATA_TYPE_CHOICES, verbose_name=_('data type'))

    class Meta:
        verbose_name = _('shapefile column')
        verbose_name_plural = _('shapefile columns')


class WebServieResultColumn(ColumnBase):
    source = models.ForeignKey(SourceWS, verbose_name=_('web service source'), related_name='columns')
    # data_type = models.PositiveIntegerField(choices=DATA_TYPE_CHOICES, verbose_name=_('data type'))
    # Web service returns the correct data type?

    class Meta:
        verbose_name = _('web service column')
        verbose_name_plural = _('web service columns')


class DatabaseResultColumn(ColumnBase):
    source = models.ForeignKey(SourceDatabase, verbose_name=_('Database source'), related_name='columns')
    data_type = models.PositiveIntegerField(choices=DATA_TYPE_CHOICES, verbose_name=_('data type'))

    class Meta:
        verbose_name = _('database column')
        verbose_name_plural = _('database columns')


class RESTResultColumn(ColumnBase):
    source = models.ForeignKey(SourceRESTAPI, verbose_name=_('REST API source'), related_name='columns')
    data_type = models.PositiveIntegerField(choices=DATA_TYPE_CHOICES, verbose_name=_('data type'))
    skip_regex = models.TextField(blank=True, verbose_name=_('skip expression'))
    import_regex = models.TextField(blank=True, verbose_name=_('import expression'))

    class Meta:
        verbose_name = _('REST API column')
        verbose_name_plural = _('REST API columns')
