# -*- coding: utf-8 -*-
#
# Copyright (C) 2004-2022 Edgewall Software
# Copyright (C) 2004 Daniel Lundin <daniel@edgewall.com>
# Copyright (C) 2004-2006 Christopher Lenz <cmlenz@gmx.de>
# Copyright (C) 2006 Jonas Borgström <jonas@edgewall.com>
# Copyright (C) 2008 Matt Good <matt@matt-good.net>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution. The terms
# are also available at https://trac.edgewall.org/wiki/TracLicense.
#
# This software consists of voluntary contributions made by many
# individuals. For the exact contribution history, see the revision
# history and logs, available at https://trac.edgewall.org/log/.
#
# Author: Daniel Lundin <daniel@edgewall.com>
#         Christopher Lenz <cmlenz@gmx.de>

import re

from trac.admin.api import AdminCommandError, IAdminCommandProvider, \
                           console_date_format, get_console_locale
from trac.core import Component, ExtensionPoint, TracError, TracValueError, \
                      implements
from trac.util import as_bool, as_float, as_int, hex_entropy, lazy
from trac.util.datefmt import get_datetime_format_hint, format_date, \
                              parse_date, time_now, to_datetime, to_timestamp
from trac.util.text import print_table
from trac.util.translation import _
from trac.web.api import IRequestHandler, is_valid_default_handler

UPDATE_INTERVAL = 3600 * 24 # Update session last_visit time stamp after 1 day
PURGE_AGE = 3600 * 24 * 90 # Expire cookie after 90 days
COOKIE_KEY = 'trac_session'


# Note: as we often manipulate both the `session` and the
#       `session_attribute` tables, there's a possibility of table
#       deadlocks (#9705). We try to prevent them to happen by always
#       accessing the tables in the same order within the transaction,
#       first `session`, then `session_attribute`.

class SessionDict(dict):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.authenticated = False
        self.last_visit = 0
        self.sid = None

    def __setitem__(self, key, value):
        super().__setitem__(key, str(value))

    def as_bool(self, key, default=None):
        """Return the value as a boolean. Return `default` if
        if an exception is raised while converting the value to a
        boolean.

        :param key: the name of the session attribute
        :keyword default: the value to return if the parameter is not
                          specified or an exception occurs converting
                          the value to a boolean.

        :since: 1.2
        """
        if key not in self:
            return default
        return as_bool(self[key], default)

    def as_int(self, key, default=None, min=None, max=None):
        """Return the value as an integer. Return `default` if
        if an exception is raised while converting the value to an
        integer.

        :param key: the name of the session attribute
        :keyword default: the value to return if the parameter does
                          not exist or an exception occurs converting
                          the value to an integer.
        :keyword min: lower bound to which the value is limited
        :keyword max: upper bound to which the value is limited

        :since: 1.2
        """
        if key not in self:
            return default
        return as_int(self[key], default, min, max)

    def as_float(self, key, default=None, min=None, max=None):
        """Return the value as a float. Return `default` if
        if an exception is raised while converting the value to a
        float.

        :param key: the name of the session attribute
        :keyword default: the value to return if the parameter does
                          not exist or an exception occurs converting
                          the value to a float.
        :keyword min: lower bound to which the value is limited
        :keyword max: upper bound to which the value is limited

        :since: 1.3.6
        """
        if key not in self:
            return default
        return as_float(self[key], default, min, max)

    def set(self, key, value, default=None):
        """Set a variable in the session, or remove it if it's equal to the
        default value.
        """
        value = str(value)
        if default is not None:
            default = str(default)
            if value == default:
                self.pop(key, None)
                return
        super().__setitem__(key, value)


class DetachedSession(SessionDict):

    def __init__(self, env, sid):
        super().__init__()
        self.env = env
        self._new = True
        self._old = {}
        if sid:
            self.get_session(sid, authenticated=True)

    def get_session(self, sid, authenticated=False):
        self.env.log.debug("Retrieving session for ID %r", sid)

        with self.env.db_query as db:
            self.sid = sid
            self.authenticated = authenticated
            self.clear()

            for last_visit, in db("""
                    SELECT last_visit FROM session
                    WHERE sid=%s AND authenticated=%s
                    """, (sid, int(authenticated))):
                self._new = False
                self.last_visit = int(last_visit or 0)
                self.update(db("""
                    SELECT name, value FROM session_attribute
                    WHERE sid=%s and authenticated=%s
                    """, (sid, int(authenticated))))
                self._old = self.copy()
                break
            else:
                self.last_visit = 0
                self._new = True
                self._old = {}

    def save(self):
        authenticated = int(self.authenticated)
        now = int(time_now())
        items = list(self.items())
        if not authenticated and not self._old and not items:
            # The session for anonymous doesn't have associated data,
            # so there's no need to persist it
            return

        # We can't do the session management in one big transaction,
        # as the intertwined changes to both the session and
        # session_attribute tables are prone to deadlocks (#9705).
        # Therefore we first we save the current session, then we
        # eventually purge the tables.

        session_saved = False

        with self.env.db_transaction as db:
            # Try to save the session if it's a new one. A failure to
            # do so is not critical but we nevertheless skip the
            # following steps.

            new = self._new
            if new:
                self.last_visit = now
                self._new = False
                # The session might already exist even if _new is True since
                # it could have been created by a concurrent request (#3563).
                try:
                    db("""INSERT INTO session (sid, last_visit, authenticated)
                          VALUES (%s,%s,%s)
                          """, (self.sid, self.last_visit, authenticated))
                except self.env.db_exc.IntegrityError:
                    self.env.log.warning('Session %s already exists', self.sid)
                    db.rollback()
                    return

            if authenticated and \
                    (new or self._old.get('name') != self.get('name') or
                     self._old.get('email') != self.get('email')):
                self.env.invalidate_known_users_cache()

            # Remove former values for session_attribute and save the
            # new ones. The last concurrent request to do so "wins".

            if self._old != self:
                if not items and not authenticated:
                    # No need to keep around empty unauthenticated sessions
                    db("DELETE FROM session WHERE sid=%s AND authenticated=0",
                       (self.sid,))
                db("""DELETE FROM session_attribute
                      WHERE sid=%s AND authenticated=%s
                      """, (self.sid, authenticated))
                self._old = dict(self.items())
                # The session variables might already have been updated by a
                # concurrent request.
                try:
                    db.executemany("""
                        INSERT INTO session_attribute
                          (sid,authenticated,name,value)
                        VALUES (%s,%s,%s,%s)
                        """, [(self.sid, authenticated, k, v)
                              for k, v in items])
                except self.env.db_exc.IntegrityError:
                    self.env.log.warning('Attributes for session %s already '
                                         'updated', self.sid)
                    db.rollback()
                    return
                session_saved = True

        # Purge expired sessions. We do this only when the session was
        # changed as to minimize the purging.

        if session_saved and now - self.last_visit > UPDATE_INTERVAL:
            self.last_visit = now
            lifetime = self.env.anonymous_session_lifetime
            mintime = now - lifetime * 86400 if lifetime > 0 else None

            with self.env.db_transaction as db:
                # Update the session last visit time if it is over an
                # hour old, so that session doesn't get purged
                self.env.log.info("Refreshing session %s", self.sid)
                db("""UPDATE session SET last_visit=%s
                      WHERE sid=%s AND authenticated=%s
                      """, (self.last_visit, self.sid, authenticated))
                if mintime:
                    self.env.log.debug('Purging old, expired, sessions.')
                    db("""DELETE FROM session_attribute
                          WHERE authenticated=0 AND sid IN (
                              SELECT sid FROM session
                              WHERE authenticated=0 AND last_visit < %s
                          )
                          """, (mintime,))

            # Avoid holding locks on lot of rows on both session_attribute
            # and session tables
            if mintime:
                with self.env.db_transaction as db:
                    db("""
                        DELETE FROM session
                        WHERE authenticated=0 AND last_visit < %s
                        """, (mintime,))


class Session(DetachedSession):
    """Basic session handling and per-session storage."""

    def __init__(self, env, req):
        super().__init__(env, None)
        self.req = req
        if not req.is_authenticated:
            if COOKIE_KEY not in req.incookie:
                self.sid = hex_entropy(24)
                self.bake_cookie()
            else:
                sid = req.incookie[COOKIE_KEY].value
                self.get_session(sid)
        else:
            if COOKIE_KEY in req.incookie:
                sid = req.incookie[COOKIE_KEY].value
                self.promote_session(sid)
            self.get_session(req.authname, authenticated=True)

    def bake_cookie(self, expires=PURGE_AGE):
        assert self.sid, 'Session ID not set'
        self.req.outcookie[COOKIE_KEY] = self.sid
        self.req.outcookie[COOKIE_KEY]['path'] = self.req.base_path or '/'
        self.req.outcookie[COOKIE_KEY]['expires'] = expires
        if self.env.secure_cookies:
            self.req.outcookie[COOKIE_KEY]['secure'] = True
        self.req.outcookie[COOKIE_KEY]['httponly'] = True

    _valid_sid_re = re.compile(r'[_A-Za-z0-9]+\Z')

    def get_session(self, sid, authenticated=False):
        refresh_cookie = False

        if not authenticated and not self._valid_sid_re.match(sid):
            raise TracValueError(_("Session ID must be alphanumeric."))
        if self.sid and sid != self.sid:
            refresh_cookie = True

        super().get_session(sid, authenticated)
        if self.last_visit and time_now() - self.last_visit > UPDATE_INTERVAL:
            refresh_cookie = True

        # Refresh the session cookie if this is the first visit after a day
        if not authenticated and refresh_cookie:
            self.bake_cookie()

    def change_sid(self, new_sid):
        assert not self.req.is_authenticated, \
               'Cannot change ID of authenticated session'
        assert new_sid, 'Session ID cannot be empty'
        if new_sid == self.sid:
            return
        if not self._valid_sid_re.match(new_sid):
            raise TracValueError(_("Session ID must be alphanumeric."),
                                 _("Error renaming session"))
        with self.env.db_transaction as db:
            if db("SELECT sid FROM session WHERE sid=%s", (new_sid,)):
                raise TracError(_("Session '%(id)s' already exists. "
                                  "Please choose a different session ID.",
                                  id=new_sid),
                                _("Error renaming session"))
            self.env.log.debug("Changing session ID %s to %s", self.sid,
                               new_sid)
            db("UPDATE session SET sid=%s WHERE sid=%s AND authenticated=0",
               (new_sid, self.sid))
            db("""UPDATE session_attribute SET sid=%s
                  WHERE sid=%s and authenticated=0
                  """, (new_sid, self.sid))
        self.sid = new_sid
        self.bake_cookie()

    def promote_session(self, sid):
        """Promotes an anonymous session to an authenticated session, if there
        is no preexisting session data for that user name.
        """
        assert self.req.is_authenticated, \
               "Cannot promote session of anonymous user"

        with self.env.db_transaction as db:
            authenticated_flags = [authenticated for authenticated, in db(
                "SELECT authenticated FROM session WHERE sid=%s OR sid=%s",
                (sid, self.req.authname))]

            if len(authenticated_flags) == 2:
                # There's already an authenticated session for the user,
                # we simply delete the anonymous session
                db("DELETE FROM session WHERE sid=%s AND authenticated=0",
                   (sid,))
                db("""DELETE FROM session_attribute
                      WHERE sid=%s AND authenticated=0
                      """, (sid,))
            elif len(authenticated_flags) == 1:
                if not authenticated_flags[0]:
                    # Update the anonymous session records so the session ID
                    # becomes the user name, and set the authenticated flag.
                    self.env.log.debug("Promoting anonymous session %s to "
                                       "authenticated session for user %s",
                                       sid, self.req.authname)
                    db("""UPDATE session SET sid=%s, authenticated=1
                          WHERE sid=%s AND authenticated=0
                          """, (self.req.authname, sid))
                    db("""UPDATE session_attribute SET sid=%s, authenticated=1
                          WHERE sid=%s
                          """, (self.req.authname, sid))
            else:
                # We didn't have an anonymous session for this sid. The
                # authenticated session might have been inserted between the
                # SELECT above and here, so we catch the error.
                try:
                    db("""INSERT INTO session (sid, last_visit, authenticated)
                          VALUES (%s, %s, 1)
                          """, (self.req.authname, int(time_now())))
                except self.env.db_exc.IntegrityError:
                    self.env.log.warning('Authenticated session for %s '
                                         'already exists', self.req.authname)
                    db.rollback()
        self._new = False

        self.sid = sid
        self.bake_cookie(0)  # expire the cookie


class SessionAdmin(Component):
    """trac-admin command provider for session management"""

    implements(IAdminCommandProvider)

    request_handlers = ExtensionPoint(IRequestHandler)

    def get_admin_commands(self):
        hints = {
            'datetime': get_datetime_format_hint(get_console_locale(self.env)),
            'iso8601': get_datetime_format_hint('iso8601'),
        }
        yield ('session list', '[sid[:0|1]] [...]',
               """List the name and email for the given sids

               Specifying the sid 'anonymous' lists all unauthenticated
               sessions, and 'authenticated' all authenticated sessions.
               '*' lists all sessions, and is the default if no sids are
               given.

               An sid suffix ':0' operates on an unauthenticated session with
               the given sid, and a suffix ':1' on an authenticated session
               (the default).""",
               self._complete_list, self._do_list)

        yield ('session add', '<sid[:0|1]> [name] [email]',
               """Create a session for the given sid

               Populates the name and email attributes for the given session.
               Adding a suffix ':0' to the sid makes the session
               unauthenticated, and a suffix ':1' makes it authenticated (the
               default if no suffix is specified).""",
               None, self._do_add)

        yield ('session set', '<name|email|default_handler> '
                              '<sid[:0|1]> <value>',
               """Set the name or email attribute of the given sid

               An sid suffix ':0' operates on an unauthenticated session with
               the given sid, and a suffix ':1' on an authenticated session
               (the default).""",
               self._complete_set, self._do_set)

        yield ('session delete', '<sid[:0|1]> [...]',
               """Delete the session of the specified sid

               An sid suffix ':0' operates on an unauthenticated session with
               the given sid, and a suffix ':1' on an authenticated session
               (the default). Specifying the sid 'anonymous' will delete all
               anonymous sessions.""",
               self._complete_delete, self._do_delete)

        yield ('session purge', '<age>',
               """Purge anonymous sessions older than given age or date

               Age may be specified as a relative time like "90 days ago", or
               as a date in the "%(datetime)s" or "%(iso8601)s" (ISO 8601)
               format.""" % hints,
               None, self._do_purge)

    @lazy
    def _valid_default_handlers(self):
        return sorted(handler.__class__.__name__
                      for handler in self.request_handlers
                      if is_valid_default_handler(handler))

    def _split_sid(self, sid):
        if sid.endswith(':0'):
            return sid[:-2], 0
        elif sid.endswith(':1'):
            return sid[:-2], 1
        else:
            return sid, 1

    def _get_sids(self):
        rows = self.env.db_query("SELECT sid, authenticated FROM session")
        return ['%s:%d' % (sid, auth) for sid, auth in rows]

    def _get_list(self, sids):
        all_anon = 'anonymous' in sids or '*' in sids
        all_auth = 'authenticated' in sids or '*' in sids
        sids = {self._split_sid(sid)
                for sid in sids
                if sid not in ('anonymous', 'authenticated', '*')}
        rows = self.env.db_query("""
            SELECT DISTINCT s.sid, s.authenticated, s.last_visit,
                            n.value, e.value, h.value
            FROM session AS s
              LEFT JOIN session_attribute AS n
                ON (n.sid=s.sid AND n.authenticated=s.authenticated
                    AND n.name='name')
              LEFT JOIN session_attribute AS e
                ON (e.sid=s.sid AND e.authenticated=s.authenticated
                    AND e.name='email')
              LEFT JOIN session_attribute AS h
                ON (h.sid=s.sid AND h.authenticated=s.authenticated
                    AND h.name='default_handler')
            ORDER BY s.sid, s.authenticated
            """)
        for sid, authenticated, last_visit, name, email, handler in rows:
            if all_anon and not authenticated or all_auth and authenticated \
                    or (sid, authenticated) in sids:
                yield (sid, authenticated,
                       format_date(to_datetime(last_visit),
                                   console_date_format),
                       name, email, handler)

    def _complete_list(self, args):
        all_sids = self._get_sids() + ['*', 'anonymous', 'authenticated']
        return set(all_sids) - set(args)

    def _complete_set(self, args):
        if len(args) == 1:
            return ['name', 'email']
        elif len(args) == 2:
            return self._get_sids()

    def _complete_delete(self, args):
        all_sids = self._get_sids() + ['anonymous']
        return set(all_sids) - set(args)

    def _do_list(self, *sids):
        if not sids:
            sids = ['*']
        headers = (_("SID"), _("Auth"), _("Last Visit"), _("Name"),
                   _("Email"), _("Default Handler"))
        print_table(self._get_list(sids), headers)

    def _do_add(self, sid, name=None, email=None):
        sid, authenticated = self._split_sid(sid)
        with self.env.db_transaction as db:
            try:
                db("INSERT INTO session VALUES (%s, %s, %s)",
                   (sid, authenticated, int(time_now())))
            except Exception:
                raise AdminCommandError(_("Session '%(sid)s' already exists",
                                          sid=sid))
            if name:
                db("INSERT INTO session_attribute VALUES (%s,%s,'name',%s)",
                    (sid, authenticated, name))
            if email:
                db("INSERT INTO session_attribute VALUES (%s,%s,'email',%s)",
                    (sid, authenticated, email))
        self.env.invalidate_known_users_cache()

    def _do_set(self, attr, sid, val):
        if attr not in ('name', 'email', 'default_handler'):
            raise AdminCommandError(_("Invalid attribute '%(attr)s'",
                                      attr=attr))
        if attr == 'default_handler':
            if val and val not in self._valid_default_handlers:
                raise AdminCommandError(_("Invalid default_handler '%(val)s'",
                                          val=val))
        sid, authenticated = self._split_sid(sid)
        with self.env.db_transaction as db:
            if not db("""SELECT sid FROM session
                         WHERE sid=%s AND authenticated=%s""",
                         (sid, authenticated)):
                raise AdminCommandError(_("Session '%(sid)s' not found",
                                          sid=sid))
            db("""
                DELETE FROM session_attribute
                WHERE sid=%s AND authenticated=%s AND name=%s
                """, (sid, authenticated, attr))
            if val:
                db("INSERT INTO session_attribute VALUES (%s, %s, %s, %s)",
                   (sid, authenticated, attr, val))
        self.env.invalidate_known_users_cache()

    def _do_delete(self, *sids):
        with self.env.db_transaction as db:
            for sid in sids:
                sid, authenticated = self._split_sid(sid)
                if sid == 'anonymous':
                    db("DELETE FROM session WHERE authenticated=0")
                    db("DELETE FROM session_attribute WHERE authenticated=0")
                else:
                    db("""
                        DELETE FROM session
                        WHERE sid=%s AND authenticated=%s
                        """, (sid, authenticated))
                    db("""
                        DELETE FROM session_attribute
                        WHERE sid=%s AND authenticated=%s
                        """, (sid, authenticated))
        self.env.invalidate_known_users_cache()

    def _do_purge(self, age):
        when = parse_date(age, hint='datetime',
                          locale=get_console_locale(self.env))
        with self.env.db_transaction as db:
            ts = to_timestamp(when)
            db("""
                DELETE FROM session
                WHERE authenticated=0 AND last_visit<%s
                """, (ts,))
            db("""
                DELETE FROM session_attribute
                WHERE authenticated=0
                      AND NOT EXISTS (SELECT * FROM session AS s
                                      WHERE s.sid=session_attribute.sid
                                      AND s.authenticated=0)
                """)


def get_session_attribute(env, sid, authenticated, name, default=None):
    for row in env.db_query("""
            SELECT value FROM session_attribute
            WHERE sid=%s AND authenticated=%s AND name=%s
            """, (sid, 1 if authenticated else 0, name)):
        return row[0]
    else:
        return default
