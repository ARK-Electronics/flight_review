#! /usr/bin/env python3

# Maintenance script: clean up logs left stuck in the Pending state.
#
# Uploads now require a registered & approved account and are parsed
# immediately, so no new log ever enters the Pending state. This script
# disposes of the historical backlog:
#
# - Anonymous pending logs (empty Uploader) predate the anonymous-upload ban.
#   They are never parsed by policy; --delete removes their files and DB rows.
# - Pending logs with a named uploader are parsed automatically when that
#   account is approved (process_pending_logs_for_user). They are listed here
#   for visibility but never touched.
#
# Idempotent and list-only by default. Run from the app/ directory after the
# DB migration (setup_db.py):
#     cd app && python cleanup_pending_logs.py           # list only
#     cd app && python cleanup_pending_logs.py --delete  # remove anonymous logs

import sys
import os
import argparse

# this is needed for the following imports
sys.path.append(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'plot_app'))
from plot_app.config import get_db_connection, get_kml_filepath, \
    get_overview_img_filepath, get_log_filepath

# Extensions a stored log file may have (see helper.get_log_filename)
LOG_FILE_EXTENSIONS = ('.ulg', '.bin', '.csv', '.bbl', '.txt')


def _pending_column_exists(cur) -> bool:
    cur.execute("PRAGMA table_info('Logs')")
    return any(row[1] == 'Pending' for row in cur.fetchall())


def _unlink_if_exists(file_name):
    if os.path.exists(file_name):
        os.unlink(file_name)
        return True
    return False


def delete_log(cur, log_id):
    """Remove a log's on-disk files and DB rows (mirrors EditEntryHandler)."""
    kml_file_name = os.path.join(get_kml_filepath(), log_id.replace('/', '.') + '.kml')
    _unlink_if_exists(kml_file_name)
    preview_image = os.path.join(get_overview_img_filepath(), log_id + '.png')
    _unlink_if_exists(preview_image)
    for ext in LOG_FILE_EXTENSIONS:
        _unlink_if_exists(os.path.join(get_log_filepath(), log_id + ext))
    cur.execute('DELETE FROM LogsGenerated WHERE Id = ?', (log_id,))
    cur.execute('DELETE FROM Logs WHERE Id = ?', (log_id,))


def main():
    parser = argparse.ArgumentParser(
        description='Clean up logs stuck in the Pending state. Lists by '
                    'default; --delete removes anonymous pending logs.')
    parser.add_argument('--delete', action='store_true',
                        help='Delete anonymous pending logs (files and DB rows).')
    args = parser.parse_args()

    con = get_db_connection()
    try:
        cur = con.cursor()
        if not _pending_column_exists(cur):
            print("The 'Pending' column does not exist yet — run setup_db.py first.")
            return
        cur.execute('select Id, Uploader, Date from Logs where Pending = 1')
        rows = cur.fetchall()

        anonymous = [(log_id, date) for log_id, uploader, date in rows if not uploader]
        named = [(log_id, uploader, date) for log_id, uploader, date in rows if uploader]

        print('Found {} pending log(s): {} anonymous, {} with a named uploader.'
              .format(len(rows), len(anonymous), len(named)))

        for log_id, uploader, date in named:
            print('  keeping {} (uploader={}, date={}) — parsed when the '
                  'account is approved'.format(log_id, uploader, date))

        if not args.delete:
            for log_id, date in anonymous:
                print('  would delete {} (anonymous, date={})'.format(log_id, date))
            if anonymous:
                print('Re-run with --delete to remove the anonymous log(s).')
            return

        deleted = 0
        failed = 0
        for log_id, date in anonymous:
            print('Deleting {} (anonymous, date={}) ... '.format(log_id, date),
                  end='', flush=True)
            try:
                delete_log(cur, log_id)
                con.commit()
            except Exception as e:  # pylint: disable=broad-except
                failed += 1
                print('error: {}'.format(e))
                continue
            deleted += 1
            print('done')

        print('Deleted {}, failed {}, kept {} named.'.format(
            deleted, failed, len(named)))
    finally:
        con.close()


if __name__ == '__main__':
    main()
