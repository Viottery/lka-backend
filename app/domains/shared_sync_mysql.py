"""Versioned native MySQL triggers for ordered shared-sync journal writes.

The journal sequence is an externally visible cursor. InnoDB allocates
AUTO_INCREMENT values before commit, so triggers take the existing hub identity
row lock before appending. This serializes only writes that publish shared-sync
changes and makes sequence order agree with commit order.

The installer is called only from the privileged schema migration. Runtime
startup uses :func:`verify_shared_sync_mysql` and never creates or replaces DDL.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

from app.storage.mysql import MySQLConnection
from app.storage.mysql_schema import MySQLSchema, MySQLSchemaError

_ASSET = Path(__file__).parents[1] / "storage" / "mysql_schema_v4.json"
_MIGRATION = 4
_MIGRATION_NAME = "ordered_shared_sync_journal_triggers"
_MIGRATION_V5 = 5
_MIGRATION_V5_NAME = "shared_sync_identity_update_guard"
_MIGRATION_V6 = 6
_MIGRATION_V6_NAME = "shared_sync_identity_binary_comparison"
_MIGRATION_V7 = 7
_MIGRATION_V7_NAME = "shared_sync_null_safe_entity_keys"


@dataclass(frozen=True)
class Trigger:
    name: str
    native_name: str
    table_name: str
    timing: str
    event: str
    source_sql: str
    mysql_sql: str


def _dependencies(projections):
    dependencies = [
        ("memory_sources", "memory_entries",
         "json_extract(entity_key,'$[0]') IN (SELECT memory_id FROM memory_entry_sources WHERE source_id={ref}.source_id)"),
        ("memory_entry_sources", "memory_entries",
         "json_extract(entity_key,'$[0]')={ref}.memory_id"),
        ("matters", "manager_goals",
         "json_extract(entity_key,'$[0]') IN (SELECT goal_id FROM manager_goal_matter_links WHERE matter_id={ref}.matter_id)"),
        ("manager_goal_matter_links", "manager_goals",
         "json_extract(entity_key,'$[0]')={ref}.goal_id"),
    ]
    for target in (
        "matter_management", "matter_source_links",
        "manager_goal_matter_links", "manager_work_relations",
    ):
        fields = [field for field in ("matter_id", "source_matter_id", "target_matter_id")
                  if field in projections[target][1]]
        condition = " OR ".join(
            f"json_extract(entity_key,'$[0]') IN (SELECT {projections[target][0][0]} FROM {target} WHERE {field}={{ref}}.matter_id)"
            for field in fields
        )
        dependencies.append(("matters", target, condition))
    for dependent in (
        "manager_controllers", "manager_goals", "manager_delegates",
        "manager_deliveries", "manager_defaults", "matter_management",
        "manager_work_relations", "manager_goal_matter_links",
    ):
        key_fields = [field for field in ("session_id", "manager_session_id", "parent_session_id")
                      if field in projections[dependent][1]]
        condition = " OR ".join(
            f"json_extract(entity_key,'$[0]') IN (SELECT {projections[dependent][0][0]} FROM {dependent} WHERE {field}={{ref}}.session_id)"
            for field in key_fields
        )
        if condition:
            dependencies.append(("agent_sessions", dependent, condition))
    return dependencies


def _trigger(name, table, timing, event, source_body, mysql_body, *, when=""):
    when_clause = f" WHEN {when}" if when else ""
    source = (f"CREATE TRIGGER IF NOT EXISTS {name} {timing} {event} ON {table}"
              f"{when_clause} BEGIN {source_body} END")
    native_name = name if len(name) <= 64 else (
        "shared_sync_" + hashlib.sha256(name.encode()).hexdigest()[:48]
    )
    native = (f"CREATE TRIGGER `{native_name}` {timing} {event} ON `{table}` "
              f"FOR EACH ROW BEGIN {mysql_body} END")
    return Trigger(name, native_name, table, timing, event, source, native)


def _mysql_condition(sql):
    sql = sql.replace("json_extract(", "JSON_UNQUOTE(JSON_EXTRACT(")
    # Close the extra JSON_UNQUOTE parenthesis immediately after JSON_EXTRACT's
    # closing parenthesis, before a comparison/operator.
    sql = re.sub(r"JSON_UNQUOTE\(JSON_EXTRACT\(([^()]*(?:\([^()]*\)[^()]*)*)\)",
                 r"JSON_UNQUOTE(JSON_EXTRACT(\1))", sql, flags=re.IGNORECASE)
    return sql


def mysql_entity_key(kind, fields, *, qualifier="", null_safe=False):
    """Render SQLite-compatible compact JSON array text for an entity key.

    MySQL JSON values stringify with spaces between members. The source hub
    stores SQLite ``json_array`` output as compact text, so native trigger keys
    must explicitly quote each text component and concatenate the delimiters.
    """
    expressions = []
    numeric = {("manager_defaults", "slot")}
    for field in fields:
        value = f"{qualifier}`{field}`" if qualifier else f"`{field}`"
        value = f"CAST({value} AS CHAR)"
        if (kind, field) not in numeric:
            value = f"JSON_QUOTE({value})"
        if null_safe:
            column = f"{qualifier}`{field}`" if qualifier else f"`{field}`"
            value = f"IF({column} IS NULL,'null',{value})"
        expressions.append(value)
    return "CONCAT('['," + ",',',".join(expressions) + ",']')"


def _specifications(*, identity_guard=False, binary_identity_guard=False, null_safe_keys=False):
    from app.domains.shared_sync import PROJECTIONS

    specs = []
    for kind, (keys, _fields) in PROJECTIONS.items():
        for event, prefix, deleted in (("INSERT", "NEW", 0), ("UPDATE", "NEW", 0),
                                       ("DELETE", "OLD", 1)):
            key = "json_array(" + ",".join(f"{prefix}.{key}" for key in keys) + ")"
            condition = f"{prefix}.kind='manager'" if kind == "agent_sessions" else ""
            source_body = (
                f"INSERT INTO shared_sync_heads VALUES('{kind}',{key},1,{deleted}) "
                f"ON CONFLICT(kind,entity_key) DO UPDATE SET version=version+1,deleted={deleted}; "
                f"INSERT INTO shared_sync_journal(kind,entity_key) VALUES('{kind}',{key});"
            )
            mysql_key = mysql_entity_key(
                kind, keys, qualifier=f"{prefix}.", null_safe=null_safe_keys,
            )
            mysql_body = (
                "UPDATE `shared_sync_meta` SET `value`=`value` WHERE `key`='hub_id'; "
                f"INSERT INTO `shared_sync_heads` (`kind`,`entity_key`,`version`,`deleted`) "
                f"VALUES('{kind}',{mysql_key},1,{deleted}) ON DUPLICATE KEY UPDATE "
                f"`version`=`version`+1,`deleted`={deleted}; "
                f"INSERT INTO `shared_sync_journal` (`kind`,`entity_key`) VALUES('{kind}',{mysql_key});"
            )
            if condition:
                mysql_body = f"IF {condition} THEN {mysql_body} END IF;"
            specs.append(_trigger(f"shared_sync_{kind}_{event.lower()}", kind,
                                  "AFTER", event, source_body, mysql_body, when=condition))

        old_key = "json_array(" + ",".join(f"OLD.{key}" for key in keys) + ")"
        new_key = "json_array(" + ",".join(f"NEW.{key}" for key in keys) + ")"
        extra = " AND OLD.kind='manager'" if kind == "agent_sessions" else ""
        condition = f"{old_key}!={new_key}{extra}"
        mysql_old = mysql_entity_key(kind, keys, qualifier="OLD.", null_safe=null_safe_keys)
        mysql_new = mysql_entity_key(kind, keys, qualifier="NEW.", null_safe=null_safe_keys)
        mysql_condition = f"{mysql_old}!={mysql_new}{extra}"
        if binary_identity_guard:
            mysql_condition = f"BINARY {mysql_old}<>BINARY {mysql_new}{extra}"
        if null_safe_keys:
            mysql_condition = (
                f"NOT (CAST({mysql_old} AS BINARY) <=> CAST({mysql_new} AS BINARY)){extra}"
            )
        tombstone = (
            f"INSERT INTO `shared_sync_heads` (`kind`,`entity_key`,`version`,`deleted`) "
            f"VALUES('{kind}',{mysql_old},1,1) ON DUPLICATE KEY UPDATE `version`=`version`+1,`deleted`=1; "
            f"INSERT INTO `shared_sync_journal` (`kind`,`entity_key`) VALUES('{kind}',{mysql_old});"
        )
        identity_body = f"UPDATE `shared_sync_meta` SET `value`=`value` WHERE `key`='hub_id'; {tombstone}"
        if identity_guard:
            identity_body = f"IF {mysql_condition} THEN {identity_body} END IF;"
        specs.append(_trigger(
            f"shared_sync_{kind}_identity_update", kind, "AFTER", "UPDATE",
            f"INSERT INTO shared_sync_heads VALUES('{kind}',{old_key},1,1) "
            f"ON CONFLICT(kind,entity_key) DO UPDATE SET version=version+1,deleted=1; "
            f"INSERT INTO shared_sync_journal(kind,entity_key) VALUES('{kind}',{old_key});",
            identity_body,
            when=condition,
        ))

    for source, target, condition_template in _dependencies(PROJECTIONS):
        # Optional schemas were checked by SQLite startup. MySQL's V1 schema is
        # complete, so the reviewed manifest installs every declared dependency.
        for event, ref in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            condition = condition_template.format(ref=ref)
            if event == "UPDATE":
                condition = ("(" + condition + ") OR ("
                             + condition_template.format(ref="OLD") + ")")
            mysql_condition = _mysql_condition(condition)
            source_body = (
                f"UPDATE shared_sync_heads SET version=version+1 WHERE kind='{target}' AND ({condition}); "
                f"INSERT INTO shared_sync_journal(kind,entity_key) SELECT kind,entity_key FROM shared_sync_heads "
                f"WHERE kind='{target}' AND ({condition});"
            )
            mysql_body = (
                "UPDATE `shared_sync_meta` SET `value`=`value` WHERE `key`='hub_id'; "
                f"UPDATE `shared_sync_heads` SET `version`=`version`+1 WHERE `kind`='{target}' AND ({mysql_condition}); "
                f"INSERT INTO `shared_sync_journal` (`kind`,`entity_key`) SELECT `kind`,`entity_key` "
                f"FROM `shared_sync_heads` WHERE `kind`='{target}' AND ({mysql_condition});"
            )
            specs.append(_trigger(
                f"shared_sync_invalidate_{source}_{target}_{event.lower()}",
                source, "AFTER", event, source_body, mysql_body,
            ))
    names = [item.name for item in specs]
    native_names = [item.native_name for item in specs]
    if len(names) != len(set(names)) or len(native_names) != len(set(native_names)):
        raise MySQLSchemaError("shared sync trigger manifest contains duplicate names")
    return specs


def manifest_data():
    return {
        "format": 1,
        "version": _MIGRATION,
        "migration_name": _MIGRATION_NAME,
        "purpose": "serialize shared-sync journal event visibility by commit order",
        "anchor": {"table": "shared_sync_meta", "key": "hub_id"},
        "triggers": [item.__dict__ if hasattr(item, "__dict__") else {
            "name": item.name, "native_name": item.native_name,
            "table_name": item.table_name,
            "timing": item.timing, "event": item.event,
            "source_sql": item.source_sql, "mysql_sql": item.mysql_sql,
        } for item in _specifications()],
    }


def manifest_v5_data():
    identities = [item for item in _specifications(identity_guard=True)
                  if item.name.endswith("_identity_update")]
    return {
        "format": 1,
        "version": _MIGRATION_V5,
        "migration_name": _MIGRATION_V5_NAME,
        "purpose": "preserve SQLite identity-update WHEN semantics in native MySQL triggers",
        "triggers": [{
            "name": item.name,
            "native_name": item.native_name,
            "table_name": item.table_name,
            "timing": item.timing,
            "event": item.event,
            "source_sql": item.source_sql,
            "mysql_sql": item.mysql_sql,
        } for item in identities],
    }


def manifest_v6_data():
    identities = [item for item in _specifications(
        identity_guard=True, binary_identity_guard=True,
    ) if item.name.endswith("_identity_update")]
    return {
        "format": 1,
        "version": _MIGRATION_V6,
        "migration_name": _MIGRATION_V6_NAME,
        "purpose": "use bytewise identity-key comparison in native MySQL trigger predicates",
        "triggers": [{
            "name": item.name,
            "native_name": item.native_name,
            "table_name": item.table_name,
            "timing": item.timing,
            "event": item.event,
            "source_sql": item.source_sql,
            "mysql_sql": item.mysql_sql,
        } for item in identities],
    }


def manifest_v7_data():
    specs = _specifications(
        identity_guard=True, binary_identity_guard=True, null_safe_keys=True,
    )
    selected = [item for item in specs if not item.name.startswith("shared_sync_invalidate_")]
    return {
        "format": 1,
        "version": _MIGRATION_V7,
        "migration_name": _MIGRATION_V7_NAME,
        "purpose": "preserve SQLite compact JSON key representation for nullable identity components",
        "triggers": [{
            "name": item.name,
            "native_name": item.native_name,
            "table_name": item.table_name,
            "timing": item.timing,
            "event": item.event,
            "source_sql": item.source_sql,
            "mysql_sql": item.mysql_sql,
        } for item in selected],
    }


def _load_manifest():
    try:
        raw = _ASSET.read_bytes()
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("shared sync MySQL schema V4 manifest is invalid") from exc
    expected = manifest_data()
    if data != expected:
        raise MySQLSchemaError("shared sync MySQL schema V4 manifest differs from reviewed definitions")
    return data, raw


def _load_v5_manifest():
    path = Path(__file__).parents[1] / "storage" / "mysql_schema_v5.json"
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("shared sync MySQL schema V5 manifest is invalid") from exc
    if data != manifest_v5_data():
        raise MySQLSchemaError("shared sync MySQL schema V5 manifest differs from reviewed definitions")
    return data, raw


def _load_v6_manifest():
    path = Path(__file__).parents[1] / "storage" / "mysql_schema_v6.json"
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("shared sync MySQL schema V6 manifest is invalid") from exc
    if data != manifest_v6_data():
        raise MySQLSchemaError("shared sync MySQL schema V6 manifest differs from reviewed definitions")
    return data, raw


def _load_v7_manifest():
    path = Path(__file__).parents[1] / "storage" / "mysql_schema_v7.json"
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MySQLSchemaError("shared sync MySQL schema V7 manifest is invalid") from exc
    if data != manifest_v7_data():
        raise MySQLSchemaError("shared sync MySQL schema V7 manifest differs from reviewed definitions")
    return data, raw


def _schema(conn):
    database = getattr(getattr(getattr(conn, "_pool", None), "config", None), "database", None)
    if not database:
        raise MySQLSchemaError("shared sync MySQL migration requires configured database identity")
    return MySQLSchema(str(database))


def _normal(value):
    text = (value or "").replace("`", "").strip()
    parts = re.split(r"('(?:''|\\.|[^'])*')", text)
    return "".join(
        part if index % 2 else re.sub(r"\s+", " ", part).casefold()
        for index, part in enumerate(parts)
    )


def _trigger_matches(actual, item):
    if actual is None:
        return False
    table_name, timing, event, action_statement = actual
    expected_body = item["mysql_sql"].split(" FOR EACH ROW BEGIN ", 1)[1].rsplit(" END", 1)[0]
    actual_body = (action_statement or "").strip()
    wrapped = re.fullmatch(r"BEGIN\s+(.*)\s+END", actual_body, flags=re.IGNORECASE | re.DOTALL)
    if wrapped:
        actual_body = wrapped.group(1).strip()
    return (
        table_name.casefold() == item["table_name"].casefold()
        and timing.casefold() == item["timing"].casefold()
        and event.casefold() == item["event"].casefold()
        and _normal(actual_body) == _normal(expected_body)
    )


def _verify_triggers(conn, schema, data):
    names = [item["native_name"] for item in data["triggers"]]
    actual_rows = conn.execute_native(
        "SELECT TRIGGER_NAME,EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
        "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
        "AND TRIGGER_NAME IN (" + ",".join("%s" for _ in names) + ")",
        tuple(names),
    ).fetchall()
    actual_by_name = {row[0]: row[1:] for row in actual_rows}
    metadata_by_name = dict(schema.master_objects(
        conn, object_type="trigger", names=[item["name"] for item in data["triggers"]],
    ))
    for item in data["triggers"]:
        actual = actual_by_name.get(item["native_name"])
        if not _trigger_matches(actual, item):
            raise MySQLSchemaError(f"native shared sync trigger is missing or differs: {item['name']}")
        if metadata_by_name.get(item["name"]) != item["source_sql"]:
            raise MySQLSchemaError(f"shared sync trigger source metadata differs: {item['name']}")


def verify_shared_sync_mysql(conn: MySQLConnection) -> None:
    data, raw = _load_manifest()
    _v5, raw_v5 = _load_v5_manifest()
    _v6, raw_v6 = _load_v6_manifest()
    v7, raw_v7 = _load_v7_manifest()
    schema = _schema(conn)
    row = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION,),
    ).fetchone()
    if row is None or row[0] != _MIGRATION_NAME or row[1] != hashlib.sha256(raw).hexdigest():
        raise MySQLSchemaError("shared sync schema V4 is not installed; run scripts/migrate_mysql.py")
    row_v5 = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V5,),
    ).fetchone()
    if (row_v5 is None or row_v5[0] != _MIGRATION_V5_NAME
            or row_v5[1] != hashlib.sha256(raw_v5).hexdigest()):
        raise MySQLSchemaError("shared sync schema V5 is not installed; run scripts/migrate_mysql.py")
    row_v6 = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V6,),
    ).fetchone()
    if (row_v6 is None or row_v6[0] != _MIGRATION_V6_NAME
            or row_v6[1] != hashlib.sha256(raw_v6).hexdigest()):
        raise MySQLSchemaError("shared sync schema V6 is not installed; run scripts/migrate_mysql.py")
    row_v7 = conn.execute_native(
        "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V7,),
    ).fetchone()
    if (row_v7 is None or row_v7[0] != _MIGRATION_V7_NAME
            or row_v7[1] != hashlib.sha256(raw_v7).hexdigest()):
        raise MySQLSchemaError("shared sync schema V7 is not installed; run scripts/migrate_mysql.py")
    overrides = {item["name"]: item for item in v7["triggers"]}
    effective = {
        **data,
        "triggers": [overrides.get(item["name"], item) for item in data["triggers"]],
    }
    _verify_triggers(conn, schema, effective)


def install_shared_sync_mysql(conn: MySQLConnection) -> None:
    """Install V4 only from the privileged, explicit MySQL migration path."""
    if conn.in_transaction:
        raise MySQLSchemaError("shared sync schema migration cannot run in a business transaction")
    data, raw = _load_manifest()
    schema = _schema(conn)
    schema.ensure_metadata(conn)
    checksum = hashlib.sha256(raw).hexdigest()
    lock_name = "lka-schema-shared-sync-" + hashlib.sha256(schema.database.encode()).hexdigest()[:32]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring shared sync migration lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION,),
        ).fetchone()
        if current is not None and (current[0] != _MIGRATION_NAME or current[1] != checksum):
            raise MySQLSchemaError("shared sync schema V4 checksum mismatch")
        v5_current = conn.execute_native(
            "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V5,),
        ).fetchone()
        if current is not None and v5_current is not None:
            v6_current = conn.execute_native(
                "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V6,),
            ).fetchone()
            v7_current = conn.execute_native(
                "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V7,),
            ).fetchone()
            if v7_current is not None:
                verify_shared_sync_mysql(conn)
            elif v6_current is not None:
                v6, _ = _load_v6_manifest()
                overrides = {item["name"]: item for item in v6["triggers"]}
                _verify_triggers(conn, schema, {
                    **data,
                    "triggers": [overrides.get(item["name"], item) for item in data["triggers"]],
                })
            else:
                v5, _ = _load_v5_manifest()
                overrides = {item["name"]: item for item in v5["triggers"]}
                _verify_triggers(conn, schema, {
                    **data,
                    "triggers": [overrides.get(item["name"], item) for item in data["triggers"]],
                })
            return
        for item in data["triggers"]:
            exists = conn.execute_native(
                "SELECT EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
                "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND TRIGGER_NAME=%s LIMIT 1", (item["native_name"],),
            ).fetchone()
            if exists is None:
                if current is not None:
                    raise MySQLSchemaError(f"installed shared sync trigger is missing: {item['name']}")
                conn.execute_native(item["mysql_sql"])
            elif not _trigger_matches(exists, item):
                raise MySQLSchemaError(f"pre-existing shared sync trigger differs: {item['name']}")
        for item in data["triggers"]:
            schema.remember_object(
                conn, object_type="trigger", object_name=item["name"],
                table_name=item["table_name"], source_sql=item["source_sql"],
            )
        conn.commit()
        _verify_triggers(conn, schema, data)
        if current is None:
            conn.execute_native(
                "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
                (_MIGRATION, _MIGRATION_NAME, checksum),
            )
            conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def install_shared_sync_mysql_v5(conn: MySQLConnection) -> None:
    """Replace only V4 identity-update triggers with their V5 guarded form."""
    if conn.in_transaction:
        raise MySQLSchemaError("shared sync schema migration cannot run in a business transaction")
    v4, _raw_v4 = _load_manifest()
    data, raw = _load_v5_manifest()
    schema = _schema(conn)
    checksum = hashlib.sha256(raw).hexdigest()
    lock_name = "lka-schema-shared-sync-v5-" + hashlib.sha256(schema.database.encode()).hexdigest()[:28]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring shared sync V5 migration lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V5,),
        ).fetchone()
        if current is not None and (current[0] != _MIGRATION_V5_NAME or current[1] != checksum):
            raise MySQLSchemaError("shared sync schema V5 checksum mismatch")
        v6_current = conn.execute_native(
            "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V6,),
        ).fetchone()
        if current is not None and v6_current is not None:
            v7_current = conn.execute_native(
                "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V7,),
            ).fetchone()
            if v7_current is not None:
                verify_shared_sync_mysql(conn)
            else:
                v6, _ = _load_v6_manifest()
                overrides = {item["name"]: item for item in v6["triggers"]}
                _verify_triggers(conn, schema, {
                    **data,
                    "triggers": [overrides.get(item["name"], item) for item in data["triggers"]],
                })
            return
        old_v4 = {item["name"]: item for item in v4["triggers"]}
        for item in data["triggers"]:
            existing = conn.execute_native(
                "SELECT EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
                "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND TRIGGER_NAME=%s LIMIT 1", (item["native_name"],),
            ).fetchone()
            if _trigger_matches(existing, item):
                continue
            if current is not None:
                raise MySQLSchemaError(f"installed V5 shared sync trigger is missing or differs: {item['name']}")
            if not _trigger_matches(existing, old_v4[item["name"]]):
                raise MySQLSchemaError(f"refusing to replace unverified pre-V5 trigger: {item['name']}")
            conn.execute_native(f"DROP TRIGGER `{item['native_name']}`")
            conn.execute_native(item["mysql_sql"])
        _verify_triggers(conn, schema, {**v4, "triggers": data["triggers"]})
        if current is None:
            conn.execute_native(
                "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
                (_MIGRATION_V5, _MIGRATION_V5_NAME, checksum),
            )
            conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def install_shared_sync_mysql_v6(conn: MySQLConnection) -> None:
    """Correct V5 key comparison to be bytewise and collation independent."""
    if conn.in_transaction:
        raise MySQLSchemaError("shared sync schema migration cannot run in a business transaction")
    v5, _raw_v5 = _load_v5_manifest()
    data, raw = _load_v6_manifest()
    schema = _schema(conn)
    checksum = hashlib.sha256(raw).hexdigest()
    lock_name = "lka-schema-shared-sync-v6-" + hashlib.sha256(schema.database.encode()).hexdigest()[:28]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring shared sync V6 migration lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V6,),
        ).fetchone()
        if current is not None and (current[0] != _MIGRATION_V6_NAME or current[1] != checksum):
            raise MySQLSchemaError("shared sync schema V6 checksum mismatch")
        v7_current = conn.execute_native(
            "SELECT 1 FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V7,),
        ).fetchone()
        if current is not None and v7_current is not None:
            verify_shared_sync_mysql(conn)
            return
        old_v5 = {item["name"]: item for item in v5["triggers"]}
        for item in data["triggers"]:
            existing = conn.execute_native(
                "SELECT EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
                "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND TRIGGER_NAME=%s LIMIT 1", (item["native_name"],),
            ).fetchone()
            if _trigger_matches(existing, item):
                continue
            if current is not None:
                raise MySQLSchemaError(f"installed V6 shared sync trigger is missing or differs: {item['name']}")
            if not _trigger_matches(existing, old_v5[item["name"]]):
                raise MySQLSchemaError(f"refusing to replace unverified pre-V6 trigger: {item['name']}")
            conn.execute_native(f"DROP TRIGGER `{item['native_name']}`")
            conn.execute_native(item["mysql_sql"])
        _verify_triggers(conn, schema, {**v5, "triggers": data["triggers"]})
        if current is None:
            conn.execute_native(
                "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
                (_MIGRATION_V6, _MIGRATION_V6_NAME, checksum),
            )
            conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()


def install_shared_sync_mysql_v7(conn: MySQLConnection) -> None:
    """Use null-safe compact key encoding for all projection row triggers."""
    if conn.in_transaction:
        raise MySQLSchemaError("shared sync schema migration cannot run in a business transaction")
    v4, _raw_v4 = _load_manifest()
    v6, _raw_v6 = _load_v6_manifest()
    data, raw = _load_v7_manifest()
    schema = _schema(conn)
    checksum = hashlib.sha256(raw).hexdigest()
    lock_name = "lka-schema-shared-sync-v7-" + hashlib.sha256(schema.database.encode()).hexdigest()[:28]
    acquired = conn.execute_native("SELECT GET_LOCK(%s,%s)", (lock_name, 5)).fetchone()
    if not acquired or int(acquired[0] or 0) != 1:
        raise MySQLSchemaError("timed out acquiring shared sync V7 migration lock")
    try:
        current = conn.execute_native(
            "SELECT name,checksum FROM `lka_schema_migrations` WHERE version=%s", (_MIGRATION_V7,),
        ).fetchone()
        if current is not None and (current[0] != _MIGRATION_V7_NAME or current[1] != checksum):
            raise MySQLSchemaError("shared sync schema V7 checksum mismatch")
        old_v4 = {item["name"]: item for item in v4["triggers"]}
        old_v6 = {item["name"]: item for item in v6["triggers"]}
        for item in data["triggers"]:
            existing = conn.execute_native(
                "SELECT EVENT_OBJECT_TABLE,ACTION_TIMING,EVENT_MANIPULATION,ACTION_STATEMENT "
                "FROM INFORMATION_SCHEMA.TRIGGERS WHERE TRIGGER_SCHEMA=DATABASE() "
                "AND TRIGGER_NAME=%s LIMIT 1", (item["native_name"],),
            ).fetchone()
            if _trigger_matches(existing, item):
                continue
            if current is not None:
                raise MySQLSchemaError(f"installed V7 shared sync trigger is missing or differs: {item['name']}")
            previous = (old_v6[item["name"]] if item["name"].endswith("_identity_update")
                        else old_v4[item["name"]])
            if not _trigger_matches(existing, previous):
                raise MySQLSchemaError(f"refusing to replace unverified pre-V7 trigger: {item['name']}")
            conn.execute_native(f"DROP TRIGGER `{item['native_name']}`")
            conn.execute_native(item["mysql_sql"])
        overrides = {item["name"]: item for item in data["triggers"]}
        _verify_triggers(conn, schema, {
            **v4,
            "triggers": [overrides.get(item["name"], item) for item in v4["triggers"]],
        })
        if current is None:
            conn.execute_native(
                "INSERT INTO `lka_schema_migrations`(version,name,checksum) VALUES(%s,%s,%s)",
                (_MIGRATION_V7, _MIGRATION_V7_NAME, checksum),
            )
            conn.commit()
    finally:
        conn.execute_native("SELECT RELEASE_LOCK(%s)", (lock_name,)).fetchone()
