"""
Supabase Database Migration Script

Directly migrates data from a source database to a target database.
No intermediate CSV files - data is copied directly between databases.

Features:
- Direct database-to-database migration
- Creates tables with proper schema in target
- Handles data type preservation (JSONB, arrays, dicts)
- Supports incremental migration (only missing tables)
- Detailed logging and progress tracking
- Configurable via .env file
"""

import os
import sys
import io
import logging
import json
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
import numpy as np
import psycopg2
import psycopg2.extensions
from sqlalchemy import create_engine, inspect, text

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------

SOURCE_DATABASE_URL = os.getenv("DATABASE_URL", "")
SOURCE_SCHEMA = os.getenv("SCHEMA", "public")

TARGET_DATABASE_URL = os.getenv("TARGET_DATABASE_URL", "")
TARGET_SCHEMA = os.getenv("TARGET_SCHEMA", "public")

SKIP_TABLES_STR = os.getenv("SKIP_TABLES", "")
SKIP_TABLES: List[str] = [t.strip() for t in SKIP_TABLES_STR.split(",") if t.strip()]

BATCH_SIZE = 10000
CREATE_TABLES = True
FORCE_OVERWRITE = False

LOG_FILE = "migration.log"
LOG_LEVEL = logging.INFO

# ------------------------------------------------------------------
# PSYCOPG2 ADAPTERS - make dict values safe for insert
# ------------------------------------------------------------------

def _adapt_jsonb(value):
    """Convert dict to a JSON string so psycopg2 can insert it.

    NOTE: Do NOT register adapters for `list` here. psycopg2 natively
    converts Python lists to PostgreSQL ARRAY literals ({...}), which
    array columns (TEXT[], uuid[], etc.) require.
    """
    if value is None:
        return psycopg2.extensions.AsIs("NULL")
    return psycopg2.extensions.QuotedString(json.dumps(value))

def register_adapters():
    psycopg2.extensions.register_adapter(dict, _adapt_jsonb)

register_adapters()

# ------------------------------------------------------------------
# LOGGING SETUP
# ------------------------------------------------------------------

def setup_logging(migration_dir: Path) -> logging.Logger:
    """Setup logging to both console and file."""
    logger = logging.getLogger("supabase_migration")
    logger.setLevel(LOG_LEVEL)
    logger.handlers = []

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)

    log_file = migration_dir / LOG_FILE
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(LOG_LEVEL)
    file_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)

    return logger

# ------------------------------------------------------------------
# SCHEMA OPERATIONS
# ------------------------------------------------------------------

def table_exists(engine, table_name: str, schema: str) -> bool:
    """Check if a table exists."""
    try:
        inspector = inspect(engine)
        tables = inspector.get_table_names(schema=schema)
        return table_name in tables
    except Exception:
        return False


def get_table_info(engine, table_name: str, schema: str) -> Optional[Dict]:
    """Get column information for a table."""
    try:
        inspector = inspect(engine)
        columns = inspector.get_columns(table_name, schema=schema)
        primary_keys = inspector.get_pk_constraint(table_name, schema=schema)

        return {
            "columns": {col['name']: col for col in columns},
            "primary_keys": primary_keys.get('constrained_columns', []) if primary_keys else []
        }
    except Exception:
        return None


def _clean_default(default) -> Optional[str]:
    """Clean a column default for use in generated DDL."""
    if default is None:
        return None
    default = str(default)

    # Skip sequence defaults (they reference source-only sequences)
    if "nextval" in default:
        return None

    # Strip PostgreSQL type casts, e.g. 'value'::"Type" -> 'value'
    if "::" in default:
        default = default.split("::")[0].strip()

    # ARRAY[...] literal -> '{...}' for PostgreSQL compatibility
    if default.upper().startswith("ARRAY["):
        default = default.replace("ARRAY[", "{").replace("]", "}")

    if default.upper() in ("NULL", ""):
        return None
    return default


def create_table_like_source(
    source_engine,
    target_engine,
    table_name: str,
    source_schema: str,
    target_schema: str
) -> bool:
    """Create a table in target with the same schema as source."""
    logger = logging.getLogger("supabase_migration")
    create_stmt = None

    try:
        table_info = get_table_info(source_engine, table_name, source_schema)
        if not table_info:
            logger.error(f"  Could not read source table info for {table_name}")
            return False

        column_defs = []
        for col_name, col_data in table_info['columns'].items():
            col_type_str = str(col_data['type']).upper()

            # Fix generic ARRAY type to TEXT[]
            if col_type_str == 'ARRAY':
                col_type = 'TEXT[]'
            else:
                col_type = str(col_data['type'])

            nullable = "NULL" if col_data['nullable'] else "NOT NULL"

            default = _clean_default(col_data.get('default'))
            default_clause = f" DEFAULT {default}" if default else ""

            column_defs.append(f'"{col_name}" {col_type} {nullable}{default_clause}')

        create_stmt = (
            f'CREATE TABLE IF NOT EXISTS {target_schema}."{table_name}" (\n  '
            + ',\n  '.join(column_defs)
        )

        if table_info['primary_keys']:
            pk_columns = ', '.join(f'"{col}"' for col in table_info['primary_keys'])
            create_stmt += f',\n  PRIMARY KEY ({pk_columns})'

        create_stmt += '\n)'

        with target_engine.begin() as conn:
            conn.execute(text(create_stmt))

        return True

    except Exception as e:
        logger.error(f"  Failed to create table {table_name}: {e}")
        if create_stmt:
            logger.error(f"  Generated DDL was:\n{create_stmt}")
        return False


# ------------------------------------------------------------------
# DATA COPYING
# ------------------------------------------------------------------

def _json_safe(value):
    """Serialize dicts/lists to JSON strings for insertion."""
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


# ------------------------------------------------------------------
# DATA COPYING
# ------------------------------------------------------------------

def copy_table_data(
    source_engine,
    target_engine,
    table_name: str,
    source_schema: str,
    target_schema: str,
    batch_size: int = BATCH_SIZE
) -> Dict:
    """Copy data from source table to target table."""
    logger = logging.getLogger("supabase_migration")

    try:
        table_info = get_table_info(source_engine, table_name, source_schema)
        if not table_info:
            return {"status": "failed", "table": table_name, "error": "Source table info not available"}

        column_names = list(table_info['columns'].keys())
        cols = ", ".join(f'"{c}"' for c in column_names)

        # Classify columns: array vs json/jsonb
        array_cols = set()
        json_cols = set()
        for name, col in table_info['columns'].items():
            type_str = str(col['type']).upper()
            if type_str.endswith('[]') or type_str == 'ARRAY':
                array_cols.add(name)
            elif 'JSON' in type_str:
                json_cols.add(name)

        def _dump_json(v):
            """Serialize dicts/lists to JSON strings for JSON/JSONB columns."""
            if isinstance(v, (dict, list)):
                return json.dumps(v)
            return v

        offset = 0
        total_rows = 0

        while True:
            query = (
                f'SELECT {cols} FROM {source_schema}."{table_name}" '
                f'LIMIT {batch_size} OFFSET {offset}'
            )
            df = pd.read_sql_query(query, con=source_engine)

            if df.empty:
                break

            for col in df.columns:
                if col in json_cols:
                    # JSONB/JSON: serialize dicts and lists to JSON strings
                    df[col] = df[col].apply(_dump_json)
                # ARRAY columns: leave Python lists untouched —
                # psycopg2 adapts them to proper {...} literals natively.
                # Dicts in other object columns are handled by the
                # global adapter registered at module level.

            df.to_sql(
                name=table_name,
                con=target_engine,
                schema=target_schema,
                if_exists='append',
                index=False,
                chunksize=batch_size
            )

            total_rows += len(df)
            offset += batch_size
            logger.info(f"  Copied batch: {offset} rows so far...")

        logger.info(f"  [OK] {table_name}: {total_rows:,} rows copied")
        return {"status": "success", "table": table_name, "row_count": total_rows}

    except Exception as e:
        error_msg = str(e)
        logger.error(f"  [FAILED] {table_name}: {error_msg[:200]}")
        return {"status": "failed", "table": table_name, "error": error_msg}
# ------------------------------------------------------------------
# MIGRATION FUNCTIONS
# ------------------------------------------------------------------

def migrate_table(
    source_engine,
    target_engine,
    table_name: str,
    source_schema: str,
    target_schema: str,
    create_tables: bool = True,
    force_overwrite: bool = False,
) -> Dict:
    """Migrate a single table from source to target."""
    logger = logging.getLogger("supabase_migration")

    try:
        target_exists = table_exists(target_engine, table_name, target_schema)

        if not target_exists and create_tables:
            logger.info(f"  Creating table: {table_name}")
            if not create_table_like_source(
                source_engine, target_engine, table_name,
                source_schema, target_schema
            ):
                return {"status": "failed", "table": table_name, "error": "Failed to create table"}
            target_exists = True

        if target_exists and not create_tables:
            logger.info(f"  Using existing table: {table_name}")

        if target_exists and force_overwrite:
            logger.info(f"  Truncating existing table: {table_name}")
            with target_engine.begin() as conn:
                conn.execute(text(f'TRUNCATE TABLE {target_schema}."{table_name}" CASCADE'))

        logger.info(f"  Copying data for: {table_name}")
        result = copy_table_data(
            source_engine, target_engine, table_name,
            source_schema, target_schema
        )

        if result["status"] == "success":
            logger.info(f"  [OK] {table_name}: {result['row_count']:,} rows migrated")

        return result

    except Exception as e:
        error_msg = str(e)
        logger.error(f"  [FAILED] {table_name}: {error_msg[:200]}")
        return {"status": "failed", "table": table_name, "error": error_msg}


def generate_migration_report(results: List[Dict], start_time: datetime) -> str:
    """Generate a summary report."""
    end_time = datetime.now()
    duration = end_time - start_time

    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    skipped_count = sum(1 for r in results if r["status"] == "skipped")
    total_tables = len(results)
    total_rows = sum(r.get("row_count", 0) for r in results)

    report = f"""
================================================================================
                      SUPABASE DATABASE MIGRATION REPORT
================================================================================

Start Time:       {start_time.strftime("%Y-%m-%d %H:%M:%S")}
End Time:         {end_time.strftime("%Y-%m-%d %H:%M:%S")}
Duration:         {duration}

Source Database:  {SOURCE_DATABASE_URL[:50]}...
Target Database:  {TARGET_DATABASE_URL[:50]}...
Source Schema:    {SOURCE_SCHEMA}
Target Schema:    {TARGET_SCHEMA}

--------------------------------------------------------------------------------
MIGRATION STATISTICS
--------------------------------------------------------------------------------
Total Tables:     {total_tables}
Successful:       {success_count} [OK]
Failed:           {failed_count} [FAILED]
Skipped:          {skipped_count} [SKIPPED]
Success Rate:     {((success_count / total_tables * 100) if total_tables > 0 else 0):.1f}%

Total Rows:       {total_rows:,}

--------------------------------------------------------------------------------
DETAILED RESULTS
--------------------------------------------------------------------------------
"""

    for r in sorted(results, key=lambda x: x["table"]):
        if r["status"] == "success":
            report += f"  [OK] {r['table']:40s} {r['row_count']:>10,} rows\n"
        elif r["status"] == "failed":
            report += f"  [FAILED] {r['table']:40s}\n"
            report += f"         Error: {r.get('error', 'Unknown')[:120]}\n"
        elif r["status"] == "skipped":
            report += f"  [SKIPPED] {r['table']:40s} (Already exists)\n"

    report += "\n" + "=" * 80 + "\n"
    return report

# ------------------------------------------------------------------
# MAIN MIGRATION FUNCTION
# ------------------------------------------------------------------

def migrate_database() -> None:
    """Main migration function."""

    if not SOURCE_DATABASE_URL:
        print("[ERROR] DATABASE_URL (source) is not configured in .env file")
        sys.exit(1)

    if not TARGET_DATABASE_URL:
        print("[ERROR] TARGET_DATABASE_URL is not configured in .env file")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    migration_dir = Path(f"migration_{timestamp}")
    migration_dir.mkdir(exist_ok=True)

    logger = setup_logging(migration_dir)

    logger.info("=" * 80)
    logger.info("SUPABASE DATABASE MIGRATION STARTED")
    logger.info("=" * 80)
    logger.info(f"Source Database: {SOURCE_DATABASE_URL[:60]}...")
    logger.info(f"Target Database: {TARGET_DATABASE_URL[:60]}...")
    logger.info(f"Source Schema: {SOURCE_SCHEMA}")
    logger.info(f"Target Schema: {TARGET_SCHEMA}")
    logger.info(f"Create Tables: {CREATE_TABLES}")
    logger.info(f"Force Overwrite: {FORCE_OVERWRITE}")
    if SKIP_TABLES:
        logger.info(f"Skipping: {', '.join(SKIP_TABLES)}")
    logger.info("-" * 80)

    start_time = datetime.now()
    results = []
    source_engine = None
    target_engine = None

    try:
        logger.info("Connecting to source and target databases...")
        source_engine = create_engine(SOURCE_DATABASE_URL, pool_pre_ping=True)
        target_engine = create_engine(TARGET_DATABASE_URL, pool_pre_ping=True)

        inspector = inspect(source_engine)
        source_tables = inspector.get_table_names(schema=SOURCE_SCHEMA)

        if not source_tables:
            logger.error("[ERROR] No tables found in source database!")
            return

        tables_to_migrate = [t for t in source_tables if t not in SKIP_TABLES]

        logger.info(f"Found {len(source_tables)} table(s) in source, {len(tables_to_migrate)} to migrate")
        logger.info("-" * 80)

        for i, table_name in enumerate(tables_to_migrate, 1):
            logger.info(f"[{i}/{len(tables_to_migrate)}] Migrating: {table_name}")
            result = migrate_table(
                source_engine=source_engine,
                target_engine=target_engine,
                table_name=table_name,
                source_schema=SOURCE_SCHEMA,
                target_schema=TARGET_SCHEMA,
                create_tables=CREATE_TABLES,
                force_overwrite=FORCE_OVERWRITE
            )
            results.append(result)
            logger.info("-" * 80)

        report = generate_migration_report(results, start_time)
        report_file = migration_dir / "MIGRATION_REPORT.txt"
        report_file.write_text(report, encoding="utf-8")

        logger.info("\n" + "=" * 80)
        logger.info("MIGRATION COMPLETED")
        logger.info("=" * 80)
        logger.info(report)

        failed = [r for r in results if r["status"] == "failed"]
        if failed:
            logger.warning(f"\n[WARNING] {len(failed)} table(s) failed to migrate!")
        else:
            logger.info("\n[OK] All tables migrated successfully!")

    except Exception as e:
        logger.error(f"\n[ERROR] CRITICAL ERROR: {e}")
        logger.error("Migration was interrupted!")

        report = generate_migration_report(results, start_time)
        report_file = migration_dir / "MIGRATION_REPORT_PARTIAL.txt"
        report_file.write_text(report, encoding="utf-8")

        raise

    finally:
        if source_engine:
            source_engine.dispose()
        if target_engine:
            target_engine.dispose()
        logger.info("Database connections closed")

# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Migrate Supabase database directly from source to target"
    )
    parser.add_argument("--force", action="store_true",
                        help="Force overwrite existing tables in target (truncate first)")
    parser.add_argument("--no-create", action="store_true",
                        help="Don't create tables, only copy data to existing ones")
    parser.add_argument("--target-url", help="Override TARGET_DATABASE_URL from .env")
    parser.add_argument("--tables", help="Comma-separated list of tables to migrate (default: all)")

    args = parser.parse_args()

    global FORCE_OVERWRITE, CREATE_TABLES, TARGET_DATABASE_URL, SKIP_TABLES

    if args.force:
        FORCE_OVERWRITE = True
    if args.no_create:
        CREATE_TABLES = False
    if args.target_url:
        TARGET_DATABASE_URL = args.target_url
    if args.tables:
        # Migrate ONLY these tables (inverts the skip logic)
        only = {t.strip() for t in args.tables.split(",") if t.strip()}
        SKIP_TABLES = []  # not used; filter applied below
        migrate_selected(only)
        return

    migrate_database()


def migrate_selected(only_tables) -> None:
    """Migrate only specific tables (useful for retrying failed ones)."""
    global SOURCE_DATABASE_URL, TARGET_DATABASE_URL, SOURCE_SCHEMA, TARGET_SCHEMA
    global CREATE_TABLES, FORCE_OVERWRITE

    if not SOURCE_DATABASE_URL or not TARGET_DATABASE_URL:
        print("[ERROR] DATABASE_URL and TARGET_DATABASE_URL must be configured")
        sys.exit(1)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    migration_dir = Path(f"migration_{timestamp}")
    migration_dir.mkdir(exist_ok=True)
    logger = setup_logging(migration_dir)

    logger.info(f"Retrying specific tables: {', '.join(sorted(only_tables))}")

    start_time = datetime.now()
    results = []
    source_engine = None
    target_engine = None

    try:
        source_engine = create_engine(SOURCE_DATABASE_URL, pool_pre_ping=True)
        target_engine = create_engine(TARGET_DATABASE_URL, pool_pre_ping=True)

        tables = [t for t in only_tables]
        for i, table_name in enumerate(tables, 1):
            logger.info(f"[{i}/{len(tables)}] Migrating: {table_name}")
            results.append(migrate_table(
                source_engine, target_engine, table_name,
                SOURCE_SCHEMA, TARGET_SCHEMA,
                CREATE_TABLES, FORCE_OVERWRITE
            ))

        report = generate_migration_report(results, start_time)
        (migration_dir / "MIGRATION_REPORT.txt").write_text(report, encoding="utf-8")
        logger.info(report)

    finally:
        if source_engine:
            source_engine.dispose()
        if target_engine:
            target_engine.dispose()
        logger.info("Database connections closed")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logging.getLogger("supabase_migration").error("\n[ERROR] Migration interrupted by user!")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        logging.getLogger("supabase_migration").error(f"\n[ERROR] Migration failed: {e}")
        sys.exit(1)