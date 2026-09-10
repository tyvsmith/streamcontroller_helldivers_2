"""Atomic, versioned scan state for restoration and external readers."""

from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import tempfile

from .bounded_json import read_bounded_json
from .scan_session import ScanSession


MAX_STATE_BYTES = 1024 * 1024


class ScanStateStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def path(self, context):
        identity = json.dumps(context, sort_keys=True, separators=(',', ':')).encode()
        return self.directory / (sha256(identity).hexdigest() + '.json')

    def save(self, context, session, catalog, colors, names):
        data = session.checkpoint()
        for row in data['slots'].values():
            key = row['id']
            row.update(name=names.get(key, key), color=colors.get(key),
                       sequence=list(catalog.get(key, [])))
        data.update(schema_version=1, context=context, updated_at=datetime.now(timezone.utc).isoformat())
        destination = self.path(context)
        self.directory.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.directory,
                                             prefix='.scan-', delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(data, stream, indent=2, ensure_ascii=False)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return destination

    def load(self, context, catalog):
        session = ScanSession()
        try:
            data = read_bounded_json(self.path(context), max_bytes=MAX_STATE_BYTES)
        except FileNotFoundError:
            return session
        self._validate(data, context)
        try:
            session.restore(data, catalog)
        except RecursionError as error:
            raise ValueError('Invalid or unsupported scan state') from error
        return session

    @staticmethod
    def _validate(data, context):
        def require(condition):
            if not condition:
                raise ValueError('Invalid or unsupported scan state')

        require(isinstance(data, dict) and type(data.get('schema_version')) is int
                and data['schema_version'] == 1)
        require(data.get('context') == context)
        require(data.get('status') in ('idle', 'scanning', 'ready', 'partial', 'failed'))
        require(isinstance(data.get('message'), str))
        require('last_scan_at' in data and (data['last_scan_at'] is None or isinstance(data['last_scan_at'], str)))
        for field in ('scan_number', 'recognized', 'unknown', 'overflow'):
            require(type(data.get(field)) is int and data[field] >= 0)
        report = data.get('last_report')
        if report is not None:
            require(isinstance(report, dict) and report.get('status') in ('matched', 'partial'))
            require(isinstance(report.get('rows'), list))
            for row in report['rows']:
                require(isinstance(row, dict) and (row.get('id') is None or isinstance(row.get('id'), str)))
        require(isinstance(data.get('slots'), dict))
        for slot, row in data['slots'].items():
            require(isinstance(slot, str) and slot.isascii() and slot.isdecimal()
                    and 0 < int(slot) and str(int(slot)) == slot)
            require(isinstance(row, dict))
            require('id' in row and (row['id'] is None or isinstance(row['id'], str)))
            require(row.get('filter') in ('any', 'red', 'blue', 'green', 'yellow'))
            require(type(row.get('unknown')) is bool and type(row.get('unconfirmed')) is bool)
            age = row.get('unconfirmed_since_scan')
            require(not row['unconfirmed'] or (row['id'] is not None and type(age) is int
                                               and 0 < age <= data['scan_number']))
