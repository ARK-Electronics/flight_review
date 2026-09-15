"""
Tornado handler to edit/delete a log upload entry
"""
from __future__ import print_function
import os
from html import escape
import sys
import secrets
from urllib.parse import urlencode
import tornado.web

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from config import get_db_connection, get_kml_filepath, get_overview_img_filepath
from helper import clear_ulog_cache, get_log_filename, validate_log_id

#pylint: disable=relative-beyond-top-level
from .common import get_jinja_env, TornadoRequestHandlerBase

EDIT_TEMPLATE = 'edit.html'

#pylint: disable=abstract-method
#pylint: disable=too-many-return-statements


class EditEntryHandler(TornadoRequestHandlerBase):
    """ Edit a log entry, with confirmation (currently only delete) """

    @staticmethod
    def _is_authorized(cur, log_id, token, user=None):
        """Return True if the request is authorized to modify the log."""
        cur.execute('select Token, Uploader from Logs where Id = ?', (log_id,))
        db_tuple = cur.fetchone()
        if db_tuple is None:
            return False

        db_token = db_tuple[0]
        db_uploader = db_tuple[1]
        if token and db_token and secrets.compare_digest(token.encode(), db_token.encode()):
            return True

        if not user:
            return False

        # Check if user is admin or owner
        cur.execute("SELECT IsAdmin, Approved FROM Users WHERE Username=?", (user,))
        user_row = cur.fetchone()
        if not user_row or not user_row[1]:
            return False

        is_admin = user_row[0]

        if is_admin:
            return True
        if db_uploader and user == db_uploader:
            return True
        return False

    def get(self, *args, **kwargs):
        """ GET request """
        log_id = self.get_query_argument('log')
        if not validate_log_id(log_id):
            raise tornado.web.HTTPError(400, 'Invalid Parameter')
        action = self.get_query_argument('action')
        token = self.get_query_argument('token', default='')
        self.set_header('Cache-Control', 'no-store')
        self.set_header('Referrer-Policy', 'no-referrer')

        if action == 'delete':
            # Emailed links and legacy confirm=1 links only display this form.
            # Mutations require a POST protected by Tornado's XSRF check.
            delete_url = self.request.path + '?' + urlencode(
                {'action': 'delete', 'log': log_id, 'token': token})
            content = """
<h3>Delete Log File</h3>
<p>Confirm deletion of log {log_id}.</p>
<form method="post" action="{delete_url}">
  {xsrf}
  <button type="submit" class="btn btn-danger">Delete log</button>
</form>
""".format(delete_url=escape(delete_url), log_id=escape(log_id),
           xsrf=self.xsrf_form_html())
        elif action == 'edit_notes':
            # Render a form to edit the flight notes/description
            con = get_db_connection()
            cur = con.cursor()
            try:
                if not self._is_authorized(cur, log_id, token, self.current_user):
                    raise tornado.web.HTTPError(403, 'Unauthorized')

                cur.execute('select Description from Logs where Id = ?', (log_id,))
                db_tuple = cur.fetchone()
                current_description = ''
                if db_tuple is not None and db_tuple[0] is not None:
                    current_description = db_tuple[0]

                form_action = escape(self.request.path + '?' + urlencode(
                    {'action': 'update_notes', 'log': log_id, 'token': token}))
                content = f"""
<h3>Edit Flight Notes</h3>
<form method=\"post\" action=\"{form_action}\" class=\"mt-3\">
  {self.xsrf_form_html()}
  <div class=\"mb-3\">
    <label for=\"description\" class=\"form-label\">Flight notes</label>
    <textarea class=\"form-control\" id=\"description\" name=\"description\"
      rows=\"6\" maxlength=\"5000\">{escape(current_description)}</textarea>
    <div class=\"form-text\">Up to 5000 characters.</div>
  </div>
  <button type=\"submit\" class=\"btn btn-primary\">Save</button>
  <a class=\"btn btn-link\" href=\"/plot_app?log={log_id}\">Cancel</a>
</form>
"""
            finally:
                cur.close()
                con.close()
        else:
            raise tornado.web.HTTPError(400, 'Invalid Parameter')

        template = get_jinja_env().get_template(EDIT_TEMPLATE)
        self.write(template.render(content=content))


    def post(self, *args, **kwargs):
        """ POST request """
        log_id = self.get_query_argument('log')
        if not validate_log_id(log_id):
            raise tornado.web.HTTPError(400, 'Invalid Parameter')
        action = self.get_query_argument('action')
        token = self.get_query_argument('token', default='')

        if action == 'delete':
            if not self.delete_log_entry(log_id, token, self.current_user):
                raise tornado.web.HTTPError(403, 'Unauthorized')
            self.render_jinja(EDIT_TEMPLATE, content='<h3>Log File deleted</h3>')
            return

        if action != 'update_notes':
            raise tornado.web.HTTPError(400, 'Invalid Parameter')

        new_description = escape(self.get_body_argument('description', default=''))
        if len(new_description) > 5000:
            new_description = new_description[:5000]

        con = get_db_connection()
        cur = con.cursor()
        try:
            if not self._is_authorized(cur, log_id, token, self.current_user):
                raise tornado.web.HTTPError(403, 'Unauthorized')

            cur.execute('update Logs set Description = ? where Id = ?', (new_description, log_id))
            con.commit()
        finally:
            cur.close()
            con.close()

        self.redirect('/plot_app?log=' + log_id)


    @staticmethod
    def delete_log_entry(log_id, token, user=None):
        """
        delete a log entry (DB & file), validate token first

        :return: True on success
        """
        if not validate_log_id(log_id):
            return False
        con = get_db_connection()
        try:
            cur = con.cursor()
            if not EditEntryHandler._is_authorized(cur, log_id, token, user):
                return False

            # kml file
            kml_path = get_kml_filepath()
            kml_file_name = os.path.join(kml_path, log_id.replace('/', '.')+'.kml')
            if os.path.exists(kml_file_name):
                os.unlink(kml_file_name)

            #preview image
            preview_image_filename = os.path.join(get_overview_img_filepath(), log_id+'.png')
            if os.path.exists(preview_image_filename):
                os.unlink(preview_image_filename)

            log_file_name = get_log_filename(log_id)
            print('deleting log entry {} and file {}'.format(log_id, log_file_name))
            os.unlink(log_file_name)
            cur.execute("DELETE FROM LogsGenerated WHERE Id = ?", (log_id,))
            cur.execute("DELETE FROM Logs WHERE Id = ?", (log_id,))
            con.commit()
        finally:
            con.close()

        # need to clear the cache as well
        clear_ulog_cache()

        return True
