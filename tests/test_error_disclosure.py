"""What a query failure is allowed to say — especially over sudo.

The hardened path runs as the service user, so anything it prints may land in
front of an agent. The rule these lock in:

- a SERVER-side error (it carries a SQLSTATE) only ever describes the caller's
  own SQL, so it is disclosed;
- a CONNECTION-level failure carries no SQLSTATE and its text can echo
  host/user/dbname, so it stays opaque.

The psycopg2 fakes these use (`ServerError` / `ConnError`) live in
tests/conftest.py, alongside the note recording which real shapes they were
measured from.
"""

from db_core.core import query

from .conftest import ConnError as _ConnError
from .conftest import ServerError as _ServerError


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


# --- a server error masked by the rollback that tried to clean up after it ----
#
# Both callers (query.run_query and core.schema.introspect) end their
# transaction in an except/finally, so a backend the server terminated raises
# TWICE: the server's OperationalError from execute(), then an InterfaceError
# from the rollback. Python chains the first onto the second as __context__ and
# propagates the SECOND — which has no SQLSTATE. Reading only the top of the
# chain therefore withholds a complaint the server made in plain words, which is
# the one thing this split exists NOT to do.

def _rollback_failure(context):
    """The InterfaceError psycopg2 raises rolling back an already-dead
    connection: no SQLSTATE of its own, carrying the real error as __context__."""
    masking = _ConnError("connection already closed")
    masking.__context__ = context
    return masking


def test_a_server_error_masked_by_a_failed_rollback_is_still_disclosed():
    masked = _rollback_failure(
        _ServerError("57P01", "terminating connection due to administrator command"))
    assert query.server_error(masked) == (
        "terminating connection due to administrator command"
    )


def test_walking_the_chain_cannot_disclose_a_connection_error():
    # The widening the walk must not cause. Looking DEEPER must not lower the
    # BAR: every link faces the same "did the server answer?" test, so a
    # connection error stays opaque wherever in the chain it sits.
    masked = _rollback_failure(
        _ConnError('could not translate host name "db-internal.example" to address'))
    assert query.server_error(masked) is None


def test_the_top_of_the_chain_wins_over_its_context():
    # The chain is consulted only when the top has nothing to say. The top IS
    # the error being reported; an older one underneath must not replace it.
    top = _ServerError("42601", 'syntax error at or near "SELEKT"')
    top.__context__ = _ServerError("42P01", 'relation "old" does not exist')
    assert query.server_error(top) == 'syntax error at or near "SELEKT"'


def test_a_context_cycle_does_not_hang():
    # A cycle needs MANUAL __context__ assignment, as below: CPython breaks
    # cycles itself when it chains implicitly, and psycopg2 never assigns
    # __context__ by hand. So this shape is not reachable from production today,
    # and this test cannot claim the guard is load-bearing for a real caller.
    #
    # It is kept because the guard is: server_error is the disclosure gate, and
    # a gate that can hang on a malformed input is worth two lines to rule out
    # for good, rather than re-deriving CPython's chaining rules the day someone
    # constructs an exception by hand.
    a, b = _ConnError("first"), _ConnError("second")
    a.__context__, b.__context__ = b, a
    assert query.server_error(a) is None
