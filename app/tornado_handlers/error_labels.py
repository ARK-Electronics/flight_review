"""
Tornado handler for updating the error label information in the database
"""
# pylint: disable=relative-beyond-top-level
from __future__ import print_function

import sys
import os
import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plot_app'))
from config import get_db_connection
from db_entry import *
from helper import validate_log_id, validate_error_ids
from .common import TornadoRequestHandlerBase

class UpdateErrorLabelHandler(TornadoRequestHandlerBase):
    """ Update the error label of a flight log."""

    @tornado.web.authenticated
    def post(self, *args, **kwargs):
        """ POST request """

        try:
            data = tornado.escape.json_decode(self.request.body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise tornado.web.HTTPError(400, 'Invalid Parameter') from exc
        if not isinstance(data, dict):
            raise tornado.web.HTTPError(400, 'Invalid Parameter')

        log_id = data.get('log')
        if not validate_log_id(log_id):
            raise tornado.web.HTTPError(400, 'Invalid Parameter')

        error_ids = data.get('labels')
        if not isinstance(error_ids, list) or not all(isinstance(item, int)
                and not isinstance(item, bool) for item in error_ids) \
                or not validate_error_ids(error_ids):
            raise tornado.web.HTTPError(400, 'Invalid Parameter')

        error_id_str = ""
        for error_ix, error_id in enumerate(error_ids):
            error_id_str += str(error_id)
            if error_ix < len(error_ids)-1:
                error_id_str += ","

        con = get_db_connection()
        cur = con.cursor()

        # Possession of a shared link does not grant permission to change a log.
        cur.execute('SELECT 1 FROM Logs l JOIN Users u ON u.Username=? '
                    'WHERE l.Id=? AND u.Approved=1 '
                    'AND (l.Uploader=u.Username OR u.IsAdmin=1)',
                    (self.current_user, log_id))
        if cur.fetchone() is None:
            cur.close()
            con.close()
            raise tornado.web.HTTPError(404)

        cur.execute(
            'UPDATE Logs SET ErrorLabels = ? WHERE Id = ?',
            (error_id_str, log_id))

        con.commit()
        cur.close()
        con.close()

        self.write('OK')

    def data_received(self, chunk):
        """ called whenever new data is received """
        pass
