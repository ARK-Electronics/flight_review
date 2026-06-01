"""
Tornado handler for the admin users panel
"""

import json
import os
import sqlite3
import sys
import traceback

import tornado.web
from tornado.ioloop import IOLoop

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), '../plot_app'))
from config import get_db_filename
from .common import TornadoRequestHandlerBase, get_jinja_env
from .upload import process_pending_logs_for_user

ADMIN_TEMPLATE = 'admin_users.html'


def _require_admin(handler):
    """Check that the current user is an admin. Returns True if admin, False otherwise."""
    if not handler.current_user:
        handler.redirect("/login")
        return False

    con = sqlite3.connect(get_db_filename())
    cur = con.cursor()
    cur.execute("SELECT IsAdmin FROM Users WHERE Username=?", (handler.current_user,))
    row = cur.fetchone()
    con.close()

    if not row or not row[0]:
        handler.set_status(403)
        handler.write("Forbidden: admin access required")
        return False
    return True


class AdminUsersHandler(TornadoRequestHandlerBase):
    """Render the admin users management page"""

    @tornado.web.authenticated
    def get(self):
        if not _require_admin(self):
            return

        con = sqlite3.connect(get_db_filename())
        cur = con.cursor()
        cur.execute(
            "SELECT Username, Email, Approved, IsAdmin FROM Users ORDER BY Username"
        )
        rows = cur.fetchall()
        con.close()

        users = []
        for row in rows:
            users.append({
                'username': row[0],
                'email': row[1],
                'approved': bool(row[2]),
                'is_admin': bool(row[3]),
            })

        self.render_jinja(ADMIN_TEMPLATE, users=users, is_admin_page=True)


class AdminUsersAPIHandler(TornadoRequestHandlerBase):
    """JSON API for admin user management actions (approve / delete)"""

    def write_error(self, status_code, **kwargs):
        self.set_header('Content-Type', 'application/json')
        error_msg = "Unknown error"
        if "exc_info" in kwargs:
            error_msg = str(kwargs["exc_info"][1])
        self.finish(json.dumps({"error": error_msg}))

    @tornado.web.authenticated
    def post(self):
        if not _require_admin(self):
            return

        self.set_header('Content-Type', 'application/json')
        con = None
        try:
            action = self.get_argument("action")
            username = self.get_argument("username")

            # Prevent self-deletion
            if action == "delete" and username == self.current_user:
                self.set_status(400)
                self.write(json.dumps({"error": "Cannot delete your own account"}))
                return

            con = sqlite3.connect(get_db_filename())
            cur = con.cursor()

            # Verify user exists
            cur.execute("SELECT Username FROM Users WHERE Username=?", (username,))
            if not cur.fetchone():
                self.set_status(404)
                self.write(json.dumps({"error": "User not found"}))
                return

            if action == "approve":
                cur.execute("UPDATE Users SET Approved=1 WHERE Username=?", (username,))
                con.commit()
                # Parse any logs the user uploaded while pending approval
                IOLoop.current().run_in_executor(
                    None, process_pending_logs_for_user, username)
                self.write(json.dumps({"success": True, "message": f"User '{username}' approved"}))

            elif action == "delete":
                cur.execute("DELETE FROM Users WHERE Username=?", (username,))
                con.commit()
                self.write(json.dumps({"success": True, "message": f"User '{username}' deleted"}))

            else:
                self.set_status(400)
                self.write(json.dumps({"error": f"Unknown action: {action}"}))

        except Exception as e:
            traceback.print_exc()
            self.set_status(500)
            self.write(json.dumps({"error": str(e)}))
        finally:
            if con:
                con.close()
