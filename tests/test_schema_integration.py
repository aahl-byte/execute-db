"""Exercises the catalog SQL against a real server.

Skipped unless EXECUTE_DB_TEST_URL is set, e.g.:

    EXECUTE_DB_TEST_URL="postgresql://..." python -m pytest \
        tests/test_schema_integration.py -v

The rest of the suite fakes psycopg2, so this file is the only thing that
proves INTROSPECT_SQL parses at all and that a real server returns the
documented shape. The unit tests can only assert against the SQL's *text* --
and text assertions have already been fooled once: a substring check for the
`::text` cast passed happily after the cast was deleted, because it matched
`contype::text` further up the query.

So the rule here is: assert only what a real database can show. Anything the
unit tests already fence from disk -- the %-doubling trap, the `-> bytes`
contract against a dict, the presence of each top-level key in the SQL source
-- is deliberately NOT repeated. Duplicating them here would buy nothing and
cost a connection.

Portability: EXECUTE_DB_TEST_URL may point at anything -- the 2,000-table dev
database, or an empty one. Assertions that need a particular feature to exist
(a table, a foreign key, a view) skip with a reason rather than fail, so a
green run against a small database means "nothing contradicted" and never
"everything was covered".
"""

import json
import os

import pytest

from db_core.core import schema

URL = os.environ.get("EXECUTE_DB_TEST_URL")

pytestmark = pytest.mark.skipif(not URL, reason="EXECUTE_DB_TEST_URL not set")


# Introspection is one ~3s query returning ~11.7MB against the dev database.
# Session-scoped, so the file costs one connection rather than one per test.
#
# Speed is the lesser reason. The real one is that four calls would be four
# different snapshots, and tests that disagree about which document they are
# describing can contradict each other for reasons that have nothing to do with
# the code. One snapshot is one subject. The usual argument against a shared
# fixture -- that tests leak state into each other -- does not apply: every
# test below only reads, and nothing here can mutate what the server sent.
#
# Deliberately NOT dependent on conftest's autouse `_app` fixture: schema
# .introspect() never reads app.current() (Task 5 made introspection
# unconditionally read-only rather than trusting the AppSpec flag), so this
# fixture needs no app configured -- which is what lets it be session-scoped at
# all, since a session fixture cannot depend on a function-scoped one.


@pytest.fixture(scope="session")
def raw():
    """The document exactly as the server sent it."""
    return schema.introspect(URL)


@pytest.fixture(scope="session")
def doc(raw):
    return json.loads(raw)


def _first(items, predicate, what):
    """The first match, or skip saying what this database lacks.

    `next(...)` would raise StopIteration against an empty database, which
    reads as a broken test rather than an absent feature.
    """
    for item in items:
        if predicate(item):
            return item
    pytest.skip(f"this database has no {what}")


def test_the_sql_parses_and_returns_text_not_jsonb(raw):
    # The whole point of the file. If INTROSPECT_SQL does not parse, this is
    # the only test in the suite that notices.
    #
    # bytes, not dict, is the other half of the `::text` contract -- the half
    # no text assertion can reach. Drop the cast and psycopg2 parses the column
    # into a dict, whose .encode() blows up inside introspect(); the unit test
    # pins that failure against a fake, this pins that the real column really
    # is text (oid 25) and not jsonb.
    assert isinstance(raw, bytes)
    assert raw


def test_the_document_is_the_documented_shape(doc):
    # The unit test greps the SQL source for each key. That proves the key was
    # typed, not that the server built it -- a CTE that silently returns no row
    # would still match the grep.
    assert doc["schema_version"] == schema.SCHEMA_VERSION  # the bound param round-tripped
    assert isinstance(doc["generated_at"], str) and doc["generated_at"]
    assert isinstance(doc["database"], str) and doc["database"]
    assert isinstance(doc["server_version"], str) and doc["server_version"]
    for key in ("schemas", "tables", "enums", "domains", "functions",
                "sequences", "extensions"):
        assert key in doc, f"missing top-level key: {key}"
        assert isinstance(doc[key], list), f"{key} should be a list, got {type(doc[key])}"


def test_tables_carry_columns_and_keys(doc):
    table = _first(doc["tables"], lambda t: t["kind"] == "table" and t["columns"],
                   "ordinary tables with columns")
    for key in ("schema", "name", "kind", "comment", "columns", "constraints",
                "indexes", "triggers", "view_definition"):
        assert key in table, f"missing table key: {key}"
    col = table["columns"][0]
    for key in ("name", "type", "not_null", "default", "identity", "generated",
                "position", "comment"):
        assert key in col, f"missing column key: {key}"
    assert isinstance(col["not_null"], bool)
    assert isinstance(col["position"], int)


def test_nulls_are_kept_rather_than_stripped(doc):
    # A deliberate design choice (no jsonb_strip_nulls) that the strict typed
    # loader depends on: it reads every key, so a key that vanishes because its
    # value happened to be null is a break. Only a real document can show this
    # -- key *presence* alone would pass on a table where nothing is null.
    for table in doc["tables"]:
        for col in table["columns"]:
            missing = [k for k in ("comment", "default") if k not in col]
            if missing:
                # Say what happened. Bare KeyErrors from the lookups below
                # would be the same failure, told worse.
                pytest.fail(
                    f"null-valued keys {missing} were stripped from column "
                    f"{table['schema']}.{table['name']}.{col['name']} -- "
                    "the document must keep nulls (no jsonb_strip_nulls)")
            if col["comment"] is None or col["default"] is None:
                return  # a null survived serialization, with its key intact
    pytest.skip("no nullable column attributes in this database to observe")


def test_empty_collections_are_lists_not_null(doc):
    # Each COALESCE(..., '[]'::jsonb) in the tables CTE. A table with no
    # triggers must carry [], not null, or every consumer needs an `or []`.
    for table in doc["tables"]:
        for key in ("columns", "constraints", "indexes", "triggers"):
            assert isinstance(table[key], list), \
                f"{table['schema']}.{table['name']}.{key} is {table[key]!r}, want a list"


def test_foreign_keys_are_structured_not_just_text(doc):
    # `definition` is the DDL; `references` is the part a consumer can act on
    # without parsing DDL. If the CASE arm broke, references would be null.
    fks = [c for t in doc["tables"] for c in t["constraints"]
           if c["type"] == "foreign_key"]
    if not fks:
        pytest.skip("no foreign keys in this database")
    fk = fks[0]
    assert fk["references"]["table"]        # schema-qualified join target
    assert fk["references"]["columns"]      # join hints without parsing DDL
    assert fk["columns"], "the referencing side should name its columns too"
    # Non-foreign constraints must NOT carry a references object.
    others = [c for t in doc["tables"] for c in t["constraints"]
              if c["type"] != "foreign_key"]
    assert all(c["references"] is None for c in others)


def test_views_carry_their_definition(doc):
    view = _first(doc["tables"], lambda t: t["kind"] in ("view", "materialized_view"),
                  "views")
    assert view["view_definition"], "a view without its definition is not much of a view"


def test_functions_carry_arg_count_and_body(doc):
    # v2 added both so `list` can stay compact and `show` can print the body
    # without a second connection. A plpgsql function has both; aggregates carry
    # a null definition by design (pg_get_functiondef refuses them), so pick a
    # real function/procedure to observe the body.
    fn = _first(doc["functions"], lambda f: f["kind"] in ("function", "procedure"),
                "ordinary functions or procedures")
    for key in ("arg_count", "definition", "arguments", "identity_arguments"):
        assert key in fn, f"missing function key: {key}"
    assert isinstance(fn["arg_count"], int)
    assert fn["definition"] and fn["definition"].upper().startswith("CREATE")


def test_system_schemas_are_excluded(doc):
    # The nspname filters in `rels` and in the `schemas` key. Against a real
    # catalog these are load-bearing -- pg_catalog alone would swamp the
    # document -- and a fake cursor cannot tell whether they work.
    for name in doc["schemas"]:
        assert name not in ("pg_catalog", "information_schema")
        assert not name.startswith("pg_")
    for table in doc["tables"]:
        assert table["schema"] not in ("pg_catalog", "information_schema"), \
            f"system schema leaked: {table['schema']}.{table['name']}"
        assert not table["schema"].startswith(("pg_toast", "pg_temp"))


def test_the_document_is_compact(raw):
    # jsonb::text is already compact; jsonb_pretty would inflate an 11.7MB
    # document for nobody's benefit, since the cache exists to be a byte copy.
    #
    # `b"\n" not in raw` over the WHOLE document, rather than sniffing for an
    # indent in the first 200 bytes: jsonb::text can never emit a raw newline
    # -- a newline inside a string value is escaped to a literal backslash-n --
    # so a bare 0x0A anywhere is proof that something reformatted the result.
    # That is an exact property of the encoding, not a guess about what a
    # reintroduced jsonb_pretty would look like in a prefix.
    assert b"\n" not in raw
