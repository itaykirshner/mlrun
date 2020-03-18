# Copyright 2018 Iguazio
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import os
import pathlib
from io import StringIO
from tempfile import mktemp

from pandas.io.json import build_table_schema

from .base import Artifact

preview_lines = 20
max_csv = 10000

class TableArtifact(Artifact):
    _dict_fields = Artifact._dict_fields + ['schema', 'header']
    kind = 'table'

    def __init__(self, key, body=None, df=None, viewer=None, visible=False,
                 inline=False, format=None, header=None, schema=None):

        key_suffix = pathlib.Path(key).suffix
        if not format and key_suffix:
            format = key_suffix[1:]
        super().__init__(
            key, body, viewer=viewer, inline=inline, format=format)

        if df is not None:
            self._is_df = True
            self.header = df.columns.values.tolist()
            self.format = 'csv' # todo other formats
            # if visible and not key_suffix:
            #     key += '.csv'
            self._body = df
        else:
            self._is_df = False
            self.header = header

        self.schema = schema
        if not viewer:
            viewer = 'table' if visible else None
        self.viewer = viewer

    def get_body(self):
        if not self._is_df:
            return self._body
        csv_buffer = StringIO()
        self._body.to_csv(
            csv_buffer, index=False, line_terminator='\n', encoding='utf-8')
        return csv_buffer.getvalue()


supported_formats = ['csv', 'parquet', 'pq', 'tsdb', 'kv']


class DatasetArtifact(Artifact):
    _dict_fields = Artifact._dict_fields + ['schema', 'header', 'length']
    kind = 'dataset'

    def __init__(self, key, df=None, preview=None, format='',
                 stats=None, **kwargs):

        format = format.lower()
        super().__init__(key, None, format=format)
        if df is None:
            raise ValueError('empty dataframe (df=)')
        if format and format not in supported_formats:
            raise ValueError('unsupported format {} use one of {}'.format(
                format, '|'.join(supported_formats)))

        self.header = df.columns.values.tolist()
        self.length = df.shape[0]
        if not format:
            format = 'csv' if self.length < max_csv else 'parquet'
        elif format == 'pq':
            format = 'parquet'

        self.format = format
        self._df = df

        preview = preview or preview_lines
        shortdf = df
        if self.length > preview:
            shortdf = df.head(preview)
        self.preview = shortdf.values.tolist()
        self.schema = build_table_schema(df)
        self.stats = None
        if stats:
            self.stats = df.describe().to_dict()
        self._kw = kwargs

    def get_body(self):
        csv_buffer = StringIO()
        self._body.to_csv(
            csv_buffer, index=False, line_terminator='\n', encoding='utf-8')
        return csv_buffer.getvalue()

    def upload(self, data_stores):
        src_path = self.src_path
        if src_path and os.path.isfile(src_path):
            self._upload_file(src_path, data_stores)
            return

        if self.format in ['csv', 'parquet']:
            writer_string = 'to_{}'.format(self.format)
            saving_func = getattr(self._df, writer_string, None)
            target = self.target_path
            to_upload = False
            if '://' in target:
                target = mktemp()
                to_upload = True

            saving_func(target, **self._kw)
            if to_upload:
                self._upload_file(target, data_stores)
                os.remove(target)
            else:
                self._set_meta(target)
            return

        raise ValueError('not implemented')