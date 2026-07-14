"""
Tornado handler for the browse page
"""
from __future__ import print_function
import sys
import os
import re
from datetime import datetime
import json
import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from config import get_db_connection, get_overview_img_filepath
from db_entry import DBData, DBDataGenerated
from helper import flight_modes_table, get_airframe_data

#pylint: disable=relative-beyond-top-level,too-many-statements
from .common import get_generated_db_data_from_log, TornadoRequestHandlerBase

BROWSE_TEMPLATE = 'browse.html'

# DataTables column index -> SQL column for per-column filtering.
# Columns not present here (e.g. Airframe, Flight Modes) are not filterable
# directly in SQL because they are derived in Python.
_COLUMN_FILTER_SQL = {
    1: 'Logs.Date',
    3: 'LogsGenerated.MavType',
    5: 'LogsGenerated.Hardware',
    6: 'LogsGenerated.Software',
    10: 'Logs.Description',
}
# Admin-only column filters (column indices appended after the base 11 columns).
_ADMIN_COLUMN_FILTER_SQL = {
    11: 'Logs.Email',
    # 12 is Public/Private and handled specially below.
}

_TAG_PREFIX_RE = re.compile(
    r"""^v[0-9]+(?:\.[0-9]+){0,2}                   # vMAJOR[.MINOR[.PATCH]]
        (?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?     # optional -prerelease
        (?:\+[0-9A-Za-z.-]+)?                       # optional +build
     """,
    re.IGNORECASE | re.VERBOSE,
)

# PX4 firmware release type enum → display suffix
# type 0 means untagged dev build — no suffix, we show the git hash instead
_RELEASE_TYPE_SUFFIX = {
    64: '-alpha',
    128: '-beta',
    192: '-rc',
    255: '',   # stable release, no suffix
}

# reverse map: search keyword → release type integer(s)
_RELEASE_KEYWORD_MAP = {
    'alpha': [64],
    'beta': [128],
    'rc': [192],
    'release': [255],
}


def _format_sw_version(ver_sw_release, ver_sw_git_hash):
    """Format a human-readable software version string.

    Args:
        ver_sw_release: stored as 'vMAJOR.MINOR.PATCH TYPE', e.g. 'v1.16.0 128'
        ver_sw_git_hash: git hash string, e.g. 'abc123def456...'

    Returns:
        Human-readable version like 'v1.16.0-beta (abc123)' or 'v1.16.0'
    """
    if not ver_sw_release:
        # fall back to truncated git hash
        if len(ver_sw_git_hash) > 10:
            return ver_sw_git_hash[:6]
        return ver_sw_git_hash

    try:
        parts = ver_sw_release.split()
        version_tag = parts[0]
        release_type = int(parts[1])
    except (IndexError, ValueError):
        # malformed ver_sw_release, fall back
        if len(ver_sw_git_hash) > 10:
            return ver_sw_git_hash[:6]
        return ver_sw_git_hash

    suffix = _RELEASE_TYPE_SUFFIX.get(release_type, '')
    display = version_tag + suffix

    # for untagged builds (type 0), append short git hash for identification
    if release_type not in _RELEASE_TYPE_SUFFIX and ver_sw_git_hash:
        short_hash = ver_sw_git_hash[:6] if len(ver_sw_git_hash) > 6 else ver_sw_git_hash
        display += ' (' + short_hash + ')'

    return display

# columns searchable via SQL LIKE
_SEARCH_COLUMNS = [
    'Logs.Description',
    'LogsGenerated.MavType',
    'LogsGenerated.Hardware',
    'LogsGenerated.Software',
    'LogsGenerated.SoftwareVersion',
    'LogsGenerated.UUID',
]

# Non-admin users only see public logs; admins see all (non-CI) logs.
_BASE_WHERE_PUBLIC = 'Logs.Public = 1 AND NOT Logs.Source = ?'
_BASE_WHERE_ADMIN = 'NOT Logs.Source = ?'
_BASE_WHERE = _BASE_WHERE_PUBLIC
_BASE_PARAMS = ['CI']

_SELECT_COLS = ('SELECT Logs.Id, Logs.Date, '
                '       Logs.Description, Logs.WindSpeed, '
                '       Logs.Rating, Logs.VideoUrl, '
                '       LogsGenerated.* '
                'FROM Logs '
                '   LEFT JOIN LogsGenerated on Logs.Id=LogsGenerated.Id ')

def format_duration(seconds: int) -> str:
    """ Format duration in seconds to HhMmSs string """
    try:
        seconds = int(seconds)
    except Exception:
        return ""
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)

    if h: return f"{h}h{m}m{s}s"
    if m: return f"{m}m{s}s"
    return f"{s}s"


def _is_hashish(q: str) -> bool:
    """ return true if the string looks like a git hash """
    if len(q) < 4:
        return False
    hex_chars = set('0123456789abcdef')
    return all(c in hex_chars for c in q)

def _is_tagish(q: str) -> bool:
    """ return true if the string looks like a git tag """
    if not q or q[0] not in ('v', 'V'):
        return False
    return bool(_TAG_PREFIX_RE.match(q))


def _escape_like(s):
    """Escape LIKE wildcards so %, _ are matched literally."""
    return s.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')

_MAX_PAGE_SIZE = 500

def _parse_version_search(search_str):
    """Check if search_str contains a release type keyword and extract it.

    Handles searches like 'beta', 'v1.16.0-beta', 'v1.16-rc'.
    Returns (version_prefix_or_None, release_types_or_None).
    """
    lower = search_str.lower().strip()

    # check for 'vX.Y.Z-keyword' pattern
    match = re.match(r'^(v[\d]+(?:\.[\d]+){0,2})-(alpha|beta|rc|release)$', lower)
    if match:
        version_prefix = match.group(1)
        keyword = match.group(2)
        return version_prefix, _RELEASE_KEYWORD_MAP.get(keyword)

    # check for bare keyword
    if lower in _RELEASE_KEYWORD_MAP:
        return None, _RELEASE_KEYWORD_MAP[lower]

    return None, None


def _build_search_clause(search_str):
    """Build SQL WHERE clause and params for a search string.

    Returns (sql_fragment, params) where sql_fragment is like
    '(col1 LIKE ? ESCAPE '\\' OR col2 LIKE ? ESCAPE '\\' ...)'
    and params is a list of bound values.
    """
    if not search_str:
        return '', []

    escaped = _escape_like(search_str)
    like_expr = "LIKE ? ESCAPE '\\'"

    hash_mode = _is_hashish(search_str)
    tag_mode = _is_tagish(search_str)

    # check for release type keyword searches (e.g. 'beta', 'v1.16.0-beta')
    version_prefix, release_types = _parse_version_search(search_str)

    if release_types is not None:
        # build clauses that match release type in SoftwareVersion column
        # SoftwareVersion stores 'v1.16.0 128' where 128 is the type
        clauses = []
        params = []
        for rtype in release_types:
            if version_prefix:
                # match 'v1.16.0 128' pattern with specific version prefix
                pattern = _escape_like(version_prefix) + '% ' + str(rtype)
            else:
                # match any version with this release type, e.g. '% 128'
                pattern = '% ' + str(rtype)
            clauses.append(f'LogsGenerated.SoftwareVersion {like_expr}')
            params.append(pattern)

        # also do standard substring search on other columns as fallback
        sub_pattern = '%' + escaped + '%'
        for col in _SEARCH_COLUMNS:
            clauses.append(f'{col} {like_expr}')
            params.append(sub_pattern)

        return '(' + ' OR '.join(clauses) + ')', params

    if hash_mode or tag_mode:
        # prefix match on software version columns only
        pattern = escaped + '%'
        prefix_cols = ['LogsGenerated.Software', 'LogsGenerated.SoftwareVersion']
        clauses = [f'{col} {like_expr}' for col in prefix_cols]
        # also allow substring match on remaining columns as fallback
        sub_pattern = '%' + escaped + '%'
        fallback_cols = [col for col in _SEARCH_COLUMNS if col not in prefix_cols]
        clauses += [f'{col} {like_expr}' for col in fallback_cols]
        params = [pattern] * len(prefix_cols) + [sub_pattern] * len(fallback_cols)
    else:
        pattern = '%' + escaped + '%'
        clauses = [f'{col} {like_expr}' for col in _SEARCH_COLUMNS]
        params = [pattern] * len(_SEARCH_COLUMNS)

    return '(' + ' OR '.join(clauses) + ')', params


def _get_columns_from_tuple(db_tuple, counter, all_overview_imgs, con, cur,
                            is_admin=False):
    """ load the display columns from a db_tuple """

    db_data = DBDataJoin()
    log_id = db_tuple[0]
    log_date = db_tuple[1].strftime('%Y-%m-%d')
    db_data.description = db_tuple[2]
    db_data.feedback = ''
    db_data.type = ''
    db_data.wind_speed = db_tuple[3]
    db_data.rating = db_tuple[4]
    db_data.video_url = db_tuple[5]
    generateddata_log_id = db_tuple[6]
    if log_id != generateddata_log_id:
        print('Join failed, loading and updating data')
        db_data_gen = get_generated_db_data_from_log(log_id, con, cur)
        if db_data_gen is None:
            return None
        db_data.add_generated_db_data_from_log(db_data_gen)
    else:
        db_data.duration_s = db_tuple[7]
        db_data.mav_type = db_tuple[8]
        db_data.estimator = db_tuple[9]
        db_data.sys_autostart_id = db_tuple[10]
        db_data.sys_hw = db_tuple[11]
        db_data.ver_sw = db_tuple[12]
        db_data.num_logged_errors = db_tuple[13]
        db_data.num_logged_warnings = db_tuple[14]
        db_data.flight_modes = \
            {int(x) for x in db_tuple[15].split(',') if len(x) > 0}
        db_data.ver_sw_release = db_tuple[16]
        db_data.vehicle_uuid = db_tuple[17]
        db_data.flight_mode_durations = \
           [tuple(map(int, x.split(':'))) for x in db_tuple[18].split(',') if len(x) > 0]
        db_data.start_time_utc = db_tuple[19]

    # bring it into displayable form
    ver_sw = _format_sw_version(db_data.ver_sw_release, db_data.ver_sw)
    airframe_data = get_airframe_data(db_data.sys_autostart_id)
    if airframe_data is None:
        airframe = db_data.sys_autostart_id
    else:
        airframe = airframe_data['name']

    flight_modes = ', '.join([flight_modes_table[x][0]
                              for x in db_data.flight_modes if x in
                              flight_modes_table])

    duration_str = format_duration(db_data.duration_s)

    start_time_str = 'N/A'
    if db_data.start_time_utc != 0:
        try:
            start_datetime = datetime.fromtimestamp(db_data.start_time_utc)
            start_time_str = start_datetime.strftime("%Y-%m-%d %H:%M")
        except ValueError as value_error:
            # bogus date
            print(value_error)

    rounded_div_class = "h-100 w-100 bg-body-secondary rounded overflow-hidden"
    image_col_class = "object-fit-cover d-block"
    overview_image_filename = f"{log_id}.png"
    if overview_image_filename in all_overview_imgs:
        image_col = f"""
            <div class="">
                <img class="map_overview {image_col_class}"
                    src="/overview_img/{overview_image_filename}"
                    loading="lazy" decoding="async" />
            </div>
        """
    else:
        image_col = f"""
            <div class="{rounded_div_class}" style="width:60px;">
                <div class="no_map_overview text-warning">
                    No Image Preview
                </div>
            </div>
        """

    description = db_data.description if db_data.description else ''

    columns = [
        counter,
        f'<a href="plot_app?log={log_id}">{log_date}</a>',
        image_col,
        db_data.mav_type,
        airframe,
        db_data.sys_hw,
        ver_sw,
        duration_str,
        start_time_str,
        flight_modes,
        description,
    ]

    if is_admin:
        # Admin-only columns: Email, Public/Private. These are appended after
        # the LogsGenerated columns by the SELECT when the requester is admin.
        uploader_email = ''
        is_public = 1
        if len(db_tuple) > 21:
            uploader_email = db_tuple[20] or ''
            is_public = db_tuple[21]
        columns.append(uploader_email)
        columns.append('Public' if is_public else 'Private')

    return columns


#pylint: disable=abstract-method
class BrowseDataRetrievalHandler(TornadoRequestHandlerBase):
    """ Ajax data retrieval handler """

    def write_error(self, status_code, **kwargs):
        """Return JSON error payloads for API failures."""
        self.set_header('Content-Type', 'application/json')
        error_msg = 'Unknown error'
        if 'exc_info' in kwargs:
            error_msg = str(kwargs['exc_info'][1])
        self.finish(json.dumps({'error': error_msg}))

    def get(self, *args, **kwargs):
        """ GET request """
        if not self.current_user:
            self.set_status(401)
            self.set_header('Content-Type', 'application/json')
            self.finish(json.dumps({'error': 'Unauthorized'}))
            return

        search_str = self.get_argument('search[value]', '').lower()
        try:
            order_ind = int(self.get_argument('order[0][column]'))
        except (ValueError, tornado.web.MissingArgumentError):
            order_ind = 1
        order_dir = self.get_argument('order[0][dir]', '').lower()
        data_start = int(self.get_argument('start'))
        data_length = int(self.get_argument('length'))
        draw_counter = int(self.get_argument('draw'))

        json_output = {'draw': draw_counter, 'data': []}

        con = get_db_connection()
        cur = con.cursor()

        # check if user is admin
        is_admin = False
        cur.execute('SELECT IsAdmin FROM Users WHERE Username=?', (self.current_user,))
        row = cur.fetchone()
        if row and row[0]:
            is_admin = True

        # admins see private logs and extra columns
        base_where = _BASE_WHERE_ADMIN if is_admin else _BASE_WHERE_PUBLIC
        select_cols = _SELECT_COLS
        if is_admin:
            select_cols = select_cols.replace(
                'LogsGenerated.* ',
                'LogsGenerated.*, Logs.Email, Logs.Public ')

        # build ORDER BY — indices must match the DataTables columns config
        ordering_col = ['',                          # 0: row number
                        'Logs.Date',                 # 1: Uploaded
                        '',                          # 2: Overview (image)
                        'LogsGenerated.MavType',     # 3: Type
                        '',                          # 4: Airframe (not orderable)
                        'LogsGenerated.Hardware',    # 5: Hardware
                        'LogsGenerated.Software',    # 6: Software
                        'LogsGenerated.Duration',    # 7: Duration
                        'LogsGenerated.StartTime',   # 8: Start Time
                        '',                          # 9: Flight Modes (not orderable)
                        'Logs.Description',          # 10: Description
                        ]
        if is_admin:
            ordering_col += ['Logs.Email', 'Logs.Public']
        sql_order = ' ORDER BY Logs.Date DESC'
        if 0 <= order_ind < len(ordering_col) and ordering_col[order_ind] != '':
            col = ordering_col[order_ind]
            direction = ' DESC' if order_dir == 'desc' else ''
            # push NULLs to the end regardless of sort direction
            sql_order = f' ORDER BY {col} IS NULL, {col}{direction}'

        # build WHERE with optional search
        where = 'WHERE ' + base_where
        params = list(_BASE_PARAMS)

        search_clause, search_params = _build_search_clause(search_str)
        if search_clause:
            where += ' AND ' + search_clause
            params += search_params

        # per-column filters from DataTables (server-side)
        column_filter_map = dict(_COLUMN_FILTER_SQL)
        if is_admin:
            column_filter_map.update(_ADMIN_COLUMN_FILTER_SQL)
        for col_idx, sql_col in column_filter_map.items():
            value = self.get_argument(
                f'columns[{col_idx}][search][value]', '').strip()
            if not value:
                continue
            where += f" AND {sql_col} LIKE ? ESCAPE '\\'"
            params.append('%' + _escape_like(value) + '%')

        if is_admin:
            # Public/Private filter (column index 12) -> Logs.Public 1/0
            vis_value = self.get_argument(
                'columns[12][search][value]', '').strip().lower()
            if vis_value:
                if 'public'.startswith(vis_value):
                    where += ' AND Logs.Public = 1'
                elif 'private'.startswith(vis_value):
                    where += ' AND Logs.Public = 0'

        # total records (unfiltered)
        cur.execute('SELECT COUNT(*) FROM Logs WHERE ' + base_where, _BASE_PARAMS)
        json_output['recordsTotal'] = cur.fetchone()[0]

        # filtered count
        cur.execute('SELECT COUNT(*) FROM Logs '
                    'LEFT JOIN LogsGenerated on Logs.Id=LogsGenerated.Id '
                    + where, params)
        records_filtered = cur.fetchone()[0]
        json_output['recordsFiltered'] = records_filtered

        # fetch only the page we need, enforce a hard max to prevent
        # unbounded queries from reintroducing the performance problem
        if data_length <= 0 or data_length > _MAX_PAGE_SIZE:
            data_length = _MAX_PAGE_SIZE
        limit_clause = ' LIMIT ? OFFSET ?'
        params += [data_length, data_start]

        cur.execute(select_cols + where + sql_order + limit_clause, params)
        db_tuples = cur.fetchall()

        all_overview_imgs = set(os.listdir(get_overview_img_filepath()))
        for i, db_tuple in enumerate(db_tuples):
            counter = data_start + i + 1
            columns = _get_columns_from_tuple(
                db_tuple, counter, all_overview_imgs, con, cur, is_admin=is_admin)
            if columns is not None:
                json_output['data'].append(columns)

        cur.close()
        con.close()

        self.set_header('Content-Type', 'application/json')
        self.write(json.dumps(json_output))

class DBDataJoin(DBData, DBDataGenerated):
    """Class for joined Data"""

    def add_generated_db_data_from_log(self, source):
        """Update joined data by parent data"""
        self.__dict__.update(source.__dict__)


class BrowseHandler(TornadoRequestHandlerBase):
    """ Browse public log file Tornado request handler """

    @tornado.web.authenticated
    def get(self, *args, **kwargs):
        """ GET request """
        template_args = {}

        search_str = self.get_argument('search', '').lower()
        if len(search_str) > 0:
            template_args['initial_search'] = json.dumps(search_str)

        # check if user is admin
        is_admin = False
        if self.current_user:
            con = get_db_connection()
            try:
                cur = con.cursor()
                cur.execute('SELECT IsAdmin FROM Users WHERE Username=?',
                            (self.current_user,))
                row = cur.fetchone()
                if row and row[0]:
                    is_admin = True
            finally:
                con.close()
        template_args['is_admin'] = is_admin

        self.render_jinja(BROWSE_TEMPLATE, **template_args)
