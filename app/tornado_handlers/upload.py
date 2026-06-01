"""
Tornado handler for the upload page
"""

from __future__ import print_function
import datetime
import hashlib
import json
import logging
import os
from html import escape
import sys
import traceback
import uuid
import binascii
import tornado.web
from tornado.ioloop import IOLoop

from pyulog import ULog
from pyulog.px4 import PX4ULog

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from db_entry import DBVehicleData, DBData
from config import get_db_connection, get_http_protocol, get_domain_name, \
    email_notifications_config, get_ulge_private_key_path
from helper import get_total_flight_time, validate_url, get_log_filename, \
    get_log_filename_with_ext, load_log_file, get_airframe_name, ULogException, decrypt_ulge_payload
from overview_generator import generate_overview_img_from_id

from logs.px4_ulog_compat import PX4ULogCompat
from logs.loader import UnsupportedLogFormat


#pylint: disable=relative-beyond-top-level
from .common import get_jinja_env, CustomHTTPError, generate_db_data_from_log_file, \
    TornadoRequestHandlerBase
from .send_email import send_notification_email, send_flightreport_email, send_admin_notification_email
from .multipart_streamer import MultiPartStreamer
from .security import (
    parse_log_bounded, ParserTimeout, ParserCrashed,
    get_rate_limiter, client_ip,
)


UPLOAD_TEMPLATE = 'upload.html'

# Minimum plausible log size (smaller than this is rejected without parsing).
MIN_UPLOAD_SIZE_BYTES = int(os.environ.get('FLIGHT_REVIEW_MIN_UPLOAD_BYTES', '1024'))

# Per-IP upload rate limits (requests per window). Edge limits (nginx) should
# also be configured.
UPLOAD_RATE_LIMIT_PER_MINUTE = int(os.environ.get(
    'FLIGHT_REVIEW_UPLOAD_PER_MINUTE', '10'))
UPLOAD_RATE_LIMIT_PER_HOUR = int(os.environ.get(
    'FLIGHT_REVIEW_UPLOAD_PER_HOUR', '60'))

# ArduPilot .bin log magic bytes
_ARDUPILOT_BIN_MAGIC = b'\xa3\x95'

_upload_log = logging.getLogger('flight_review.upload')
if not _upload_log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s upload %(message)s'))
    _upload_log.addHandler(_h)
    _upload_log.setLevel(logging.INFO)


def _sha256_of_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _find_existing_log_by_hash(content_hash: str):
    """Return existing log_id with this content hash, or None."""
    if not content_hash:
        return None
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute('select Id from Logs where ContentHash = ? limit 1', [content_hash])
        row = cur.fetchone()
        return row[0] if row else None
    finally:
        con.close()


#pylint: disable=attribute-defined-outside-init,too-many-statements, unused-argument


def update_vehicle_db_entry(cur, ulog, log_id, vehicle_name):
    """
    Update the Vehicle DB entry
    :param cur: DB cursor
    :param ulog: ULog object
    :param vehicle_name: new vehicle name or '' if not updated
    :return vehicle_data: DBVehicleData object
    """

    vehicle_data = DBVehicleData()
    if 'sys_uuid' in ulog.msg_info_dict:
        vehicle_data.uuid = escape(ulog.msg_info_dict['sys_uuid'])

        if vehicle_name == '':
            cur.execute('select Name '
                        'from Vehicle where UUID = ?', [vehicle_data.uuid])
            db_tuple = cur.fetchone()
            if db_tuple is not None:
                vehicle_data.name = db_tuple[0]
            print('reading vehicle name from db:'+vehicle_data.name)
        else:
            vehicle_data.name = vehicle_name
            print('vehicle name from uploader:'+vehicle_data.name)

        vehicle_data.log_id = log_id
        flight_time = get_total_flight_time(ulog)
        if flight_time is not None:
            vehicle_data.flight_time = flight_time

        # update or insert the DB entry
        cur.execute('insert or replace into Vehicle (UUID, LatestLogId, Name, FlightTime)'
                    'values (?, ?, ?, ?)',
                    [vehicle_data.uuid, vehicle_data.log_id, vehicle_data.name,
                     vehicle_data.flight_time])
    return vehicle_data


def _is_uploader_approved(username: str) -> bool:
    """Return True if the given username corresponds to an approved user."""
    if not username:
        return False
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute("SELECT Approved FROM Users WHERE Username=?", [username])
        row = cur.fetchone()
        return bool(row and row[0])
    finally:
        con.close()


def process_pending_log(log_id: str) -> bool:
    """Parse a previously-deferred log and populate vehicle / generated data.

    Clears the Pending flag on success. Returns True if processed, False if the
    log no longer exists or parsing failed.
    """
    ulog_file_name = get_log_filename(log_id)
    if not os.path.exists(ulog_file_name):
        return False
    try:
        ulog = load_log_file(ulog_file_name)
    except Exception as e:
        print(f"process_pending_log: failed to parse {log_id}: {e}")
        return False

    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute('select Type, Public, Source from Logs where Id = ?', [log_id])
        row = cur.fetchone()
        if row is None:
            return False
        upload_type, is_public, source = row[0], row[1], row[2]
        update_vehicle_db_entry(cur, ulog, log_id, '')
        cur.execute('update Logs set Pending = 0 where Id = ?', [log_id])
        con.commit()
        cur.close()
    finally:
        con.close()

    if upload_type == 'flightreport' and is_public and source != 'CI':
        try:
            generate_db_data_from_log_file(log_id, None)
        except Exception as e:
            print(f"process_pending_log: generate_db_data_from_log_file failed: {e}")
        try:
            generate_overview_img_from_id(log_id)
        except Exception as e:
            print(f"process_pending_log: generate_overview_img_from_id failed: {e}")
    return True


def process_pending_logs_for_user(username: str) -> int:
    """Process all pending logs uploaded by the given user. Returns count processed."""
    if not username:
        return 0
    con = get_db_connection()
    try:
        cur = con.cursor()
        cur.execute('select Id from Logs where Uploader = ? and Pending = 1', [username])
        log_ids = [r[0] for r in cur.fetchall()]
        cur.close()
    finally:
        con.close()
    count = 0
    for log_id in log_ids:
        if process_pending_log(log_id):
            count += 1
    return count


@tornado.web.stream_request_body
class UploadHandler(TornadoRequestHandlerBase):
    """ Upload log file Tornado request handler: handles page requests and POST
    data """

    def initialize(self):
        """ initialize the instance """
        self.multipart_streamer = None

    def prepare(self):
        """ called before a new request """
        if self.request.method.upper() == 'POST':
            try:
                total = int(self.request.headers.get("Content-Length", "0"))
            except KeyError:
                total = 0
            
            self.multipart_streamer = MultiPartStreamer(total)

    def data_received(self, chunk):
        """ called whenever new data is received """
        if self.multipart_streamer:
            self.multipart_streamer.data_received(chunk)

    def get(self, *args, **kwargs):
        """ GET request callback """
        # check if the user wants to load a log directly
        log_id = self.get_argument('log', default='')
        if log_id != '':
            # we need to redirect to the bokeh app
            url = "/plot_app?log="+log_id
            self.redirect(url)
            return

        initial_email = ''
        # try to get the email from the cookie
        try:
            initial_email = self.get_cookie('email')
            if initial_email is None: initial_email = ''
        except: #pylint: disable=bare-except
            pass

        self.render_jinja(UPLOAD_TEMPLATE, error_message='',
                                   initial_email=initial_email,
                                   is_plot_page=False)

    def _generate_unique_log_filename(self, ext: str):
        """Generate a unique log filename (with extension) that does not exist yet."""
        while True:
            log_id = str(uuid.uuid4())
            new_file_name = get_log_filename_with_ext(log_id, ext)
            if not os.path.exists(new_file_name):
                return log_id, new_file_name


    async def post(self, *args, **kwargs):
        """ POST request callback """
        # Per-IP rate limit (in-process; also configure edge limits in nginx)
        ip = client_ip(self)
        limiter = get_rate_limiter()
        if not limiter.check('upload_min', ip, UPLOAD_RATE_LIMIT_PER_MINUTE, 60):
            _upload_log.warning('rate_limited ip=%s window=1m', ip)
            raise CustomHTTPError(429, 'Too many uploads, please slow down.')
        if not limiter.check('upload_hr', ip, UPLOAD_RATE_LIMIT_PER_HOUR, 3600):
            _upload_log.warning('rate_limited ip=%s window=1h', ip)
            raise CustomHTTPError(429, 'Hourly upload quota exceeded.')

        if self.multipart_streamer:
            try:
                self.multipart_streamer.data_complete()
                form_data = self.multipart_streamer.get_values(
                    ['description', 'email',
                     'allowForAnalysis', 'obfuscated', 'source', 'type',
                     'feedback', 'windSpeed', 'rating', 'videoUrl', 'public',
                     'vehicleName', 'redirect'])
                description = escape(form_data['description'].decode("utf-8"))
                email = form_data['email'].decode("utf-8")
                print(f"UploadHandler: extracted email '{email}'", flush=True)
                upload_type = 'personal'
                if 'type' in form_data:
                    upload_type = form_data['type'].decode("utf-8")
                source = 'webui'
                title = '' # may be used in future...
                if 'source' in form_data:
                    source = form_data['source'].decode("utf-8")
                obfuscated = 0
                if 'obfuscated' in form_data:
                    if form_data['obfuscated'].decode("utf-8") == 'true':
                        obfuscated = 1
                allow_for_analysis = 0
                if 'allowForAnalysis' in form_data:
                    if form_data['allowForAnalysis'].decode("utf-8") == 'true':
                        allow_for_analysis = 1
                feedback = ''
                if 'feedback' in form_data:
                    feedback = escape(form_data['feedback'].decode("utf-8"))
                should_redirect = source != 'QGroundControl'
                if 'redirect' in form_data:
                    should_redirect = form_data['redirect'].decode("utf-8") == 'true'
                wind_speed = -1
                rating = ''
                stored_email = email
                video_url = ''
                is_public = 0
                vehicle_name = ''
                error_labels = ''
                
                uploader_username = ''
                if self.current_user:
                    uploader_username = self.current_user

                if upload_type == 'flightreport':
                    if 'windSpeed' in form_data:
                        try:
                            wind_speed = int(escape(form_data['windSpeed'].decode("utf-8")))
                        except ValueError:
                            wind_speed = -1
                    if 'rating' in form_data:
                        rating = escape(form_data['rating'].decode("utf-8"))
                        if rating == 'notset': rating = ''
                    # get video url & check if valid
                    if 'videoUrl' in form_data:
                        video_url = escape(form_data['videoUrl'].decode("utf-8"), quote=True)
                        if not validate_url(video_url):
                            video_url = ''
                    if 'vehicleName' in form_data:
                        vehicle_name = escape(form_data['vehicleName'].decode("utf-8"))

                    # always allow for statistical analysis
                    allow_for_analysis = 1
                    if 'public' in form_data:
                        if form_data['public'].decode("utf-8") == 'true':
                            is_public = 1

                parts = self.multipart_streamer.get_parts_by_name('filearg')
                if not parts:
                    # Log available parts for debugging
                    all_parts = [p.get_name() for p in self.multipart_streamer.parts]
                    print(f"Upload failed: 'filearg' not found. Available parts: {all_parts}")
                    raise CustomHTTPError(400, "No file uploaded")
                file_obj = parts[0]
                upload_file_name = file_obj.get_filename()
                upload_file_name_lower = (upload_file_name or '').lower()

                # Minimum size check (cheap rejection before any parsing)
                upload_size = file_obj.get_size()
                if upload_size < MIN_UPLOAD_SIZE_BYTES:
                    raise CustomHTTPError(400,
                        f'File too small ({upload_size} bytes); not a valid log.')

                # check if the file is encrypted
                ulge_key_path = get_ulge_private_key_path()
                if ulge_key_path and upload_file_name_lower.endswith('.ulge'):
                    file_payload = file_obj.get_payload()  # full content as bytes
                    try:
                        decrypted_data = decrypt_ulge_payload(
                        file_payload,
                        get_ulge_private_key_path()
                    )

                    except Exception as e:
                        raise CustomHTTPError(400, f"Decryption failed: {str(e)}") from e

                    if decrypted_data[:len(ULog.HEADER_BYTES)] != ULog.HEADER_BYTES:
                        raise CustomHTTPError(400, "Decrypted file is not a valid ULog")

                    # Write decrypted .ulg to disk
                    log_id, new_file_name = self._generate_unique_log_filename('.ulg')

                    with open(new_file_name, 'wb') as output_file:
                        output_file.write(decrypted_data)

                    print(f"Decryption successful for {upload_file_name}, saved to {new_file_name}")

                else:
                    # Regular file: support .ulg, ArduPilot .bin, Betaflight CSV
                    if upload_file_name_lower.endswith('.bin'):
                        ext = '.bin'
                    elif upload_file_name_lower.endswith('.csv'):
                        ext = '.csv'
                    elif upload_file_name_lower.endswith('.bbl') or upload_file_name_lower.endswith('.txt'):
                        # Accept upload but provide a clear error message.
                        raise CustomHTTPError(
                            400,
                            'Betaflight Blackbox binary logs (.bbl/.txt) are not directly supported yet. '
                            'Please export to CSV using Blackbox Explorer and upload the CSV.'
                        )
                    else:
                        ext = '.ulg'

                    log_id, new_file_name = self._generate_unique_log_filename(ext)

                    if ext == '.ulg':
                        header_len = len(ULog.HEADER_BYTES)
                        if file_obj.get_payload_partial(header_len) != ULog.HEADER_BYTES:
                            raise CustomHTTPError(400, 'Invalid File')
                    elif ext == '.bin':
                        # ArduPilot dataflash logs start with 0xA3 0x95
                        if file_obj.get_payload_partial(len(_ARDUPILOT_BIN_MAGIC)) \
                                != _ARDUPILOT_BIN_MAGIC:
                            raise CustomHTTPError(400,
                                'Invalid .bin file (missing ArduPilot magic bytes).')
                    elif ext == '.csv':
                        # Sniff: first bytes should be printable ASCII / CSV-like
                        sniff = file_obj.get_payload_partial(64)
                        if not sniff or any(b == 0 for b in sniff):
                            raise CustomHTTPError(400,
                                'Invalid .csv file (binary content detected).')

                    print('Moving uploaded file to', new_file_name)
                    file_obj.move(new_file_name)

                # Dedupe by content hash so the same file uploaded N times
                # only consumes one slot and one parse.
                content_hash = ''
                try:
                    content_hash = _sha256_of_file(new_file_name)
                except Exception as e:
                    print(f'Hashing failed for {new_file_name}: {e}')
                if content_hash:
                    existing_log_id = _find_existing_log_by_hash(content_hash)
                    if existing_log_id and existing_log_id != log_id:
                        try:
                            os.unlink(new_file_name)
                        except OSError:
                            pass
                        _upload_log.info(
                            'dedupe ip=%s uploader=%s existing_id=%s hash=%s',
                            ip, uploader_username or '-', existing_log_id, content_hash[:12])
                        url = '/plot_app?log=' + existing_log_id
                        if should_redirect:
                            self.redirect(url)
                        else:
                            self.write(json.dumps({'url': url}))
                        return

                if obfuscated == 1:
                    # TODO: randomize gps data, ...
                    pass

                # generate a token: secure random string (url-safe)
                token = str(binascii.hexlify(os.urandom(16)), 'ascii')

                # Load the ulog file but only if not uploaded via CI.
                # Also defer parsing entirely if the uploader is not an approved user
                # (anonymous uploads or pending registrations) — the log will be
                # parsed later when the user is approved.
                ulog = None
                is_pending = 0
                if source != 'CI':
                    if uploader_username and _is_uploader_approved(uploader_username):
                        ulog_file_name = get_log_filename(log_id)
                        try:
                            ulog = await parse_log_bounded(ulog_file_name)
                        except UnsupportedLogFormat as e:
                            raise CustomHTTPError(400, str(e)) from e
                        except ParserTimeout as e:
                            _upload_log.warning(
                                'parse_timeout ip=%s uploader=%s id=%s size=%s',
                                ip, uploader_username or '-', log_id, upload_size)
                            raise CustomHTTPError(400,
                                'Log parsing took too long; the file may be corrupt or unsupported.') from e
                        except ParserCrashed as e:
                            _upload_log.error(
                                'parse_crashed ip=%s uploader=%s id=%s size=%s',
                                ip, uploader_username or '-', log_id, upload_size)
                            raise CustomHTTPError(400,
                                'Log parser failed unexpectedly on this file.') from e
                    else:
                        is_pending = 1
                        print(f"Deferring parsing for log {log_id} "
                              f"(uploader='{uploader_username}' not approved)")

                # put additional data into a DB
                con = get_db_connection()
                try:
                    cur = con.cursor()
                    cur.execute(
                        'insert into Logs (Id, Title, Description, '
                        'OriginalFilename, Date, AllowForAnalysis, Obfuscated, '
                        'Source, Email, WindSpeed, Rating, Feedback, Type, '
                        'videoUrl, ErrorLabels, Public, Token, Uploader, Pending, ContentHash) values '
                        '(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        [log_id, title, description, upload_file_name,
                         datetime.datetime.now(), allow_for_analysis,
                         obfuscated, source, stored_email, wind_speed, rating,
                         feedback, upload_type, video_url, error_labels, is_public,
                         token, uploader_username, is_pending, content_hash])

                    if ulog is not None:
                        vehicle_data = update_vehicle_db_entry(cur, ulog, log_id, vehicle_name)
                        vehicle_name = vehicle_data.name

                    con.commit()
                    cur.close()
                finally:
                    con.close()

                url = '/plot_app?log='+log_id
                full_plot_url = get_http_protocol()+'://'+get_domain_name()+url
                print(full_plot_url)

                delete_url = get_http_protocol()+'://'+get_domain_name()+ \
                    '/edit_entry?action=delete&log='+log_id+'&token='+token

                edit_url = get_http_protocol()+'://'+get_domain_name()+ \
                    '/edit_entry?action=edit_notes&log='+log_id+'&token='+token

                # information for the notification email
                info = {}
                info['description'] = description
                info['feedback'] = feedback
                info['upload_filename'] = upload_file_name
                info['type'] = ''
                info['airframe'] = ''
                info['hardware'] = ''
                info['uuid'] = ''
                info['software'] = ''
                info['rating'] = rating
                if len(vehicle_name) > 0:
                    info['vehicle_name'] = vehicle_name

                if ulog is not None:
                    try:
                        px4_ulog = PX4ULog(ulog)
                    except Exception:
                        px4_ulog = PX4ULogCompat(ulog, source_name=str(ulog.msg_info_dict.get('sys_name', 'Log')))
                    info['type'] = px4_ulog.get_mav_type()
                    airframe_name_tuple = get_airframe_name(ulog)
                    if airframe_name_tuple is not None:
                        airframe_name, airframe_id = airframe_name_tuple
                        if len(airframe_name) == 0:
                            info['airframe'] = airframe_id
                        else:
                            info['airframe'] = airframe_name
                    sys_hardware = ''
                    if 'ver_hw' in ulog.msg_info_dict:
                        sys_hardware = escape(ulog.msg_info_dict['ver_hw'])
                        info['hardware'] = sys_hardware
                    if 'sys_uuid' in ulog.msg_info_dict and sys_hardware != 'SITL':
                        info['uuid'] = escape(ulog.msg_info_dict['sys_uuid'])
                    branch_info = ''
                    if 'ver_sw_branch' in ulog.msg_info_dict:
                        branch_info = ' (branch: '+escape(ulog.msg_info_dict['ver_sw_branch'])+')'
                    if 'ver_sw' in ulog.msg_info_dict:
                        ver_sw = escape(ulog.msg_info_dict['ver_sw'])
                        info['software'] = ver_sw + branch_info


                if upload_type == 'flightreport' and is_public and source != 'CI':
                    destinations = set(email_notifications_config['public_flightreport'])
                    if rating in ['unsatisfactory', 'crash_sw_hw', 'crash_pilot']:
                        destinations = destinations | \
                            set(email_notifications_config['public_flightreport_bad'])
                    send_flightreport_email(
                        list(destinations),
                        full_plot_url,
                        DBData.rating_str_static(rating),
                        DBData.wind_speed_str_static(wind_speed), delete_url, edit_url,
                        email, info)

                    # also generate the additional DB entry
                    # (we may have the log already loaded in 'ulog', however the
                    # lru cache will make it very quick to load it again)
                    # Run in executor to avoid blocking. Pass None for connection
                    # to create a new one in the thread.
                    await IOLoop.current().run_in_executor(
                        None, generate_db_data_from_log_file, log_id, None)
                    # also generate the preview image
                    IOLoop.instance().add_callback(generate_overview_img_from_id, log_id)

                # send notification emails
                send_notification_email(email, full_plot_url, delete_url, edit_url, info)

                admin_email = "logs@arkelectron.com"
                if email != admin_email:
                    send_admin_notification_email(admin_email, email, full_plot_url, delete_url, edit_url, info)

                _upload_log.info(
                    'accepted ip=%s uploader=%s id=%s size=%s pending=%s source=%s type=%s hash=%s',
                    ip, uploader_username or '-', log_id, upload_size,
                    is_pending, source, upload_type,
                    content_hash[:12] if content_hash else '-')

                if should_redirect:
                    self.redirect(url)
                else:
                    # Return plot url as json
                    self.write(json.dumps({"url": url}))

            except CustomHTTPError:
                raise

            except ULogException as e:
                raise CustomHTTPError(
                    400,
                    'Failed to parse the file. It is most likely corrupt.') from e
            except Exception as e:
                print('Error when handling POST data', sys.exc_info()[0],
                      sys.exc_info()[1])
                traceback.print_exc()
                raise CustomHTTPError(500) from e

            finally:
                self.multipart_streamer.release_parts()

