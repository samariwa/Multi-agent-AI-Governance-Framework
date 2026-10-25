"""The only database access in the whole system. The broker builds every SQL
statement itself from a structured request — no caller-supplied SQL, ever.

Identifiers are only ever names that already passed the policy checks, and
they are still quoted via psycopg.sql.Identifier. Values are bound parameters.
"""

import os
from datetime import date, datetime
from decimal import Decimal

import psycopg
from psycopg import sql


def _conninfo() -> str:
    return psycopg.conninfo.make_conninfo(
        host=os.environ["DB_HOST"],
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"],
    )


def build_select(table: str, columns: list[str], filter_: dict, limit: int) -> tuple[sql.Composed, list]:
    where = sql.SQL("")
    params: list = []
    if filter_:
        clauses = []
        for col, value in filter_.items():
            if value is None:
                clauses.append(sql.SQL("{} IS NULL").format(sql.Identifier(col)))
            else:
                clauses.append(sql.SQL("{} = %s").format(sql.Identifier(col)))
                params.append(value)
        where = sql.SQL(" WHERE ") + sql.SQL(" AND ").join(clauses)
    query = sql.SQL("SELECT {cols} FROM {table}{where} ORDER BY {order} LIMIT {limit}").format(
        cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
        table=sql.Identifier(table),
        where=where,
        order=sql.Identifier(columns[0]),
        limit=sql.Literal(int(limit)),
    )
    return query, params


def _jsonable(v):
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    return v


async def run_select(table: str, columns: list[str], filter_: dict, limit: int) -> list[dict]:
    query, params = build_select(table, columns, filter_, limit)
    async with await psycopg.AsyncConnection.connect(_conninfo()) as conn:
        await conn.set_read_only(True)  # defence in depth: the broker only ever reads
        async with conn.cursor() as cur:
            await cur.execute(query, params)
            names = [d.name for d in cur.description]
            return [{n: _jsonable(v) for n, v in zip(names, row)} for row in await cur.fetchall()]
