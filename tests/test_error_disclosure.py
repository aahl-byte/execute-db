"""What a query failure is allowed to say — especially over sudo.

The hardened path runs as the service user, so anything it prints may land in
front of an agent. The rule these lock in:

- a SERVER-side error (it carries a SQLSTATE) only ever describes the caller's
  own SQL, so it is disclosed;
- a CONNECTION-level failure carries no SQLSTATE and its text can echo
  host/user/dbname, so it stays opaque.

The fakes below mirror shapes measured from psycopg2 2.9 against a real
PostgreSQL 16, not invented ones:

    SELEKT 1              -> pgcode '42601', diag.message_primary
                             'syntax error at or near "SELEKT"'
    bad host              -> pgcode None, diag.message_primary None,
                             str(e) 'could not translate host name "…" to address'
"""

import pytest

from db_core.core import query


class _Diag:
    def __init__(self, message_primary=None, message_hint=None):
        self.message_primary = message_primary
        self.message_hint = message_hint


class _ServerError(Exception):
    """A psycopg2 error raised BY the server: it has a SQLSTATE."""

    def __init__(self, pgcode, primary, hint=None, text=None):
        super().__init__(text or primary)
        self.pgcode = pgcode
        self.diag = _Diag(primary, hint)


class _ConnError(Exception):
    """A psycopg2 OperationalError from connecting: no SQLSTATE, leaky text."""

    def __init__(self, text):
        super().__init__(text)
        self.pgcode = None
        self.diag = _Diag()


# --- disclosed: the server complaining about your SQL -------------------------

def test_syntax_error_is_disclosed():
    e = _ServerError("42601", 'syntax error at or near "SELEKT"',
                     text='syntax error at or near "SELEKT"\nLINE 1: SELEKT 1\n        ^\n')
    assert query.server_error(e) == 'syntax error at or near "SELEKT"'


def test_undefined_table_is_disclosed():
    e = _ServerError("42P01", 'relation "no_such_table" does not exist')
    assert query.server_error(e) == 'relation "no_such_table" does not exist'


def test_server_hint_is_appended_when_present():
    e = _ServerError("42703", 'column "nam" does not exist',
                     hint='Perhaps you meant to reference the column "users.name".')
    assert query.server_error(e) == (
        'column "nam" does not exist (Perhaps you meant to reference the column "users.name".)'
    )


def test_disclosure_uses_diag_not_str_so_line_caret_context_stays_out():
    # str(e) carries LINE/caret; we return only the server's primary message.
    e = _ServerError("42601", 'syntax error at or near "SELEKT"',
                     text='syntax error at or near "SELEKT"\nLINE 1: SELEKT 1\n        ^\n')
    assert "LINE 1" not in query.server_error(e)


# --- withheld: anything that could name the connection ------------------------

def test_connection_failure_is_withheld():
    # The leak this whole split exists to prevent.
    e = _ConnError('could not translate host name "db-internal.example" to address: '
                   "Name or service not known\n")
    assert query.server_error(e) is None


def test_password_failure_is_withheld():
    e = _ConnError('connection to server at "10.0.0.5", port 5432 failed: '
                   'FATAL:  password authentication failed for user "svc_admin"\n')
    assert query.server_error(e) is None


def test_non_psycopg_exception_is_withheld():
    # A bug in our own code must not become a disclosure channel.
    assert query.server_error(ValueError("/home/exploredb/.explore-db/.env.dev")) is None


def test_sqlstate_without_a_primary_message_is_withheld():
    # Defensive: pgcode set but diag empty — disclose nothing rather than "None".
    assert query.server_error(_ServerError("42601", None)) is None
