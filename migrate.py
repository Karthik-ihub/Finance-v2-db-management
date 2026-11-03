"""
Supabase Database Migration Script

Directly migrates data from a source database to a target database.
No intermediate CSV files - data is copied directly between databases.

Features:
- Direct database-to-database migration
- Creates tables with proper schema in target
- Handles data type preservation
- Supports incremental migration (only missing tables)
- Detailed logging and progress tracking
- Configurable via .env file
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import pandas as pd
from sqlalchemy import create_engine, inspect, exc, text, MetaData

# ------------------------------------------------------------------
# CONFIGURATION
# ------------------------------------------------------------------

# Source database (where data comes from)
SOURCE_DATABASE_URL = os.getenv("DATABASE_URL", "")
SOURCE_SCHEMA = os.getenv("SCHEMA", "public")

# Target database (where data goes to)
TARGET_DATABASE_URL = os.getenv("TARGET_DATABASE_URL", "")
TARGET_SCHEMA = os.getenv("TARGET_SCHEMA", "public")

# Options
SKIP_TABLES_STR = os.getenv("SKIP_TABLES", "")
SKIP_TABLES: List[str] = [t.strip() for t in SKIP_TABLES_STR.split(",") if t.strip()]

# Migration settings
BATCH_SIZE = 10000  # Rows per batch for large tables
CREATE_TABLES = True
FORCE_OVERWRITE = False

# Logging
LOG_FILE = "migration.log"
LOG_LEVEL = logging.INFO

# ------------------------------------------------------------------
# LOGGING SETUP
# ------------------------------------------------------------------

def setup_logging(migration_dir: Path) -> logging.Logger:
    """Setup logging to both console and file."""
    logger = logging.getLogger("supabase_migration")
    logger.setLevel(LOG_LEVEL)
    logger.handlers = []
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(LOG_LEVEL)
    console_formatter = logging.Formatter(
        "%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # File handler
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


def create_table_like_source(
    source_engine,
    target_engine,
    table_name: str,
    source_schema: str,
    target_schema: str
) -> bool:
    """Create a table in target with the same schema as source."""
    try:
        # Get source table info
        table_info = get_table_info(source_engine, table_name, source_schema)
        if not table_info:
            return False
        
        # Build column definitions
        column_defs = []
        for col_name, col_data in table_info['columns'].items():
            col_type = col_data['type']
            nullable = "NULL" if col_data['nullable'] else "NOT NULL"
            default = f" DEFAULT {col_data['default']}" if col_data['default'] else ""
            column_defs.append(f'"{col_name}" {col_type} {nullable}{default}')
        
        # Create table statement
        create_stmt = f'CREATE TABLE IF NOT EXISTS {target_schema}.\"{table_name}\" (\n  ' + ',\n  '.join(column_defs) + '\n)'
        
        # Add primary key
        if table_info['primary_keys']:
            pk_columns = ', '.join(f'"{col}"' for col in table_info['primary_keys'])
            create_stmt += f',\n  PRIMARY KEY ({pk_columns})'
        
        # Execute
        with target_engine.connect() as conn:
            conn.execute(text(create_stmt))
            conn.commit()
        
        return True
        
    except Exception as e:
        print(f"[ERROR] Failed to create table {table_name}: {e}")
        return False


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
        # Get column names from source
        table_info = get_table_info(source_engine, table_name, source_schema)
        if not table_info:
            return {"status": "failed", "table": table_name, "error": "Source table info not available"}
        
        column_names = list(table_info['columns'].keys())
        
        # Read from source in batches
        offset = 0
        total_rows = 0
        
        while True:
            query = f'SELECT {", ".join(f"\"{col}\"" for col in column_names)} FROM {source_schema}.\"{table_name}\" LIMIT {batch_size} OFFSET {offset}'
            df = pd.read_sql_query(query, con=source_engine)
            
            if df.empty:
                break
            
            # Write to target
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
        logger.error(f"  [FAILED] {table_name}: {error_msg[:100]}")
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
        # Check if target table exists
        target_exists = table_exists(target_engine, table_name, target_schema)
        
        # Create table if needed
        if not target_exists and create_tables:
            logger.info(f"  Creating table: {table_name}")
            if not create_table_like_source(source_engine, target_engine, table_name, source_schema, target_schema):
                return {"status": "failed", "table": table_name, "error": "Failed to create table"}
            target_exists = True
        
        # Check if we should overwrite
        if target_exists and not force_overwrite:
            logger.info(f"  [SKIPPED] Table {table_name} already exists in target (use FORCE_OVERWRITE=true to replace)")
            return {"status": "skipped", "table": table_name}
        
        # Truncate if forcing overwrite
        if target_exists and force_overwrite:
            logger.info(f"  Truncating existing table: {table_name}")
            with target_engine.connect() as conn:
                conn.execute(text(f'TRUNCATE TABLE {target_schema}.\"{table_name}\" CASCADE'))
                conn.commit()
        
        # Copy data
        logger.info(f"  Copying data for: {table_name}")
        result = copy_table_data(source_engine, target_engine, table_name, source_schema, target_schema)
        
        if result["status"] == "success":
            logger.info(f"  [OK] {table_name}: {result['row_count']:,} rows migrated")
        
        return result
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"  [FAILED] {table_name}: {error_msg[:100]}")
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
            report += f"         Error: {r.get('error', 'Unknown')[:60]}\n"
        elif r["status"] == "skipped":
            report += f"  [SKIPPED] {r['table']:40s} (Already exists)\n"
    
    report += "\n================================================================================\n"
    report += "=" * 80 + "\n"
    
    return report


# ------------------------------------------------------------------
# MAIN MIGRATION FUNCTION
# ------------------------------------------------------------------

def migrate_database() -> None:
    """Main migration function."""
    
    # Validate configuration
    if not SOURCE_DATABASE_URL:
        print("[ERROR] DATABASE_URL (source) is not configured in .env file")
        sys.exit(1)
    
    if not TARGET_DATABASE_URL:
        print("[ERROR] TARGET_DATABASE_URL is not configured in .env file")
        print("Please add your new database connection URL to .env")
        sys.exit(1)
    
    # Create migration directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    migration_dir = Path(f"migration_{timestamp}")
    migration_dir.mkdir(exist_ok=True)
    
    # Setup logging
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
    
    try:
        # Connect to both databases
        logger.info("Connecting to source and target databases...")
        source_engine = create_engine(SOURCE_DATABASE_URL, pool_pre_ping=True)
        target_engine = create_engine(TARGET_DATABASE_URL, pool_pre_ping=True)
        
        # Get list of tables from source
        inspector = inspect(source_engine)
        source_tables = inspector.get_table_names(schema=SOURCE_SCHEMA)
        
        if not source_tables:
            logger.error("[ERROR] No tables found in source database!")
            return
        
        # Filter out skipped tables
        tables_to_migrate = [t for t in source_tables if t not in SKIP_TABLES]
        
        logger.info(f"Found {len(source_tables)} table(s) in source, {len(tables_to_migrate)} to migrate")
        logger.info("-" * 80)
        
        # Migrate each table
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
        
        # Generate and save report
        report = generate_migration_report(results, start_time)
        report_file = migration_dir / "MIGRATION_REPORT.txt"
        report_file.write_text(report, encoding="utf-8")
        
        logger.info("\n" + "=" * 80)
        logger.info("MIGRATION COMPLETED")
        logger.info("=" * 80)
        logger.info(report)
        
        # Check results
        failed = [r for r in results if r["status"] == "failed"]
        if failed:
            logger.warning(f"\n[WARNING] {len(failed)} table(s) failed to migrate!")
            logger.warning("Check the report above for details.")
        else:
            logger.info("\n[OK] All tables migrated successfully!")
        
    except Exception as e:
        logger.error(f"\n[ERROR] CRITICAL ERROR: {e}")
        logger.error("Migration was interrupted!")
        
        # Generate partial report
        report = generate_migration_report(results, start_time)
        report_file = migration_dir / "MIGRATION_REPORT_PARTIAL.txt"
        report_file.write_text(report, encoding="utf-8")
        
        raise
    
    finally:
        if 'source_engine' in locals():
            source_engine.dispose()
        if 'target_engine' in locals():
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
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force overwrite existing tables in target"
    )
    parser.add_argument(
        "--no-create",
        action="store_true",
        help="Don't create tables, only copy data to existing ones"
    )
    parser.add_argument(
        "--target-url",
        help="Override TARGET_DATABASE_URL from .env"
    )
    
    args = parser.parse_args()
    
    # Override settings from command-line
    if args.force:
        global FORCE_OVERWRITE
        FORCE_OVERWRITE = True
    
    if args.no_create:
        global CREATE_TABLES
        CREATE_TABLES = False
    
    if args.target_url:
        global TARGET_DATABASE_URL
        TARGET_DATABASE_URL = args.target_url
    
    migrate_database()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger = logging.getLogger("supabase_migration")
        logger.error("\n[ERROR] Migration interrupted by user!")
        sys.exit(1)
    except SystemExit:
        raise
    except Exception as e:
        logger = logging.getLogger("supabase_migration")
        logger.error(f"\n[ERROR] Migration failed: {e}")
        sys.exit(1)
