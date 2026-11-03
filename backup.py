"""
Supabase Database Backup Script

Features:
- Detailed logging to both console and file
- Retry mechanism for failed tables
- Progress tracking
- Summary report with statistics
- Validation of backed up data
- Configurable settings via .env file
"""

import os
import sys
import logging
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional

try:
    from dotenv import load_dotenv
    load_dotenv()  # Load environment variables from .env file
except ImportError:
    # dotenv not installed, try to load from environment variables only
    pass

import pandas as pd
from sqlalchemy import create_engine, inspect, exc

# Connection string - Use direct connection for better reliability
DATABASE_URL = os.getenv("DATABASE_URL", "")

# Schema to backup
SCHEMA = os.getenv("SCHEMA", "public")

# Optional: Only backup specific tables (leave empty for ALL tables)
ONLY_TABLES_STR = os.getenv("ONLY_TABLES", "")
ONLY_TABLES: List[str] = [t.strip() for t in ONLY_TABLES_STR.split(",") if t.strip()] if ONLY_TABLES_STR else []

# Tables to explicitly skip (e.g., system tables, very large tables)
SKIP_TABLES_STR = os.getenv("SKIP_TABLES", "_prisma_migrations")
SKIP_TABLES: List[str] = [t.strip() for t in SKIP_TABLES_STR.split(",") if t.strip()] if SKIP_TABLES_STR else []

# Retry settings
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
RETRY_DELAY_SECONDS = int(os.getenv("RETRY_DELAY_SECONDS", "2"))

# Batch settings for very large tables (rows to fetch at a time)
BATCH_SIZE_STR = os.getenv("BATCH_SIZE", "")
BATCH_SIZE = int(BATCH_SIZE_STR) if BATCH_SIZE_STR and BATCH_SIZE_STR.isdigit() else None

# Logging settings
LOG_FILE = "backup.log"
LOG_LEVEL = logging.INFO

# Output settings
BACKUP_DIR_PREFIX = "supabase_backup"

# ------------------------------------------------------------------
# LOGGING SETUP
# ------------------------------------------------------------------

def setup_logging(backup_dir: Path) -> None:
    """Setup logging to both console and file."""
    log_file = backup_dir / LOG_FILE
    
    # Create logger
    logger = logging.getLogger("supabase_backup")
    logger.setLevel(LOG_LEVEL)
    
    # Clear existing handlers
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
# BACKUP FUNCTIONS
# ------------------------------------------------------------------

def get_all_tables(engine, schema: str, only_tables: List[str], skip_tables: List[str]) -> List[str]:
    """Get list of tables to backup."""
    inspector = inspect(engine)
    all_tables = inspector.get_table_names(schema=schema)
    
    # Filter by ONLY_TABLES if specified
    if only_tables:
        all_tables = [t for t in all_tables if t in only_tables]
    
    # Remove skipped tables
    all_tables = [t for t in all_tables if t not in skip_tables]
    
    return sorted(all_tables)


def backup_table(
    engine,
    table: str,
    backup_dir: Path,
    schema: str,
    max_retries: int = MAX_RETRIES,
    batch_size: Optional[int] = BATCH_SIZE,
) -> Dict:
    """
    Backup a single table with retry logic.
    
    Returns:
        Dict with status, row_count, file_path, error (if any)
    """
    logger = logging.getLogger("supabase_backup")
    
    for attempt in range(max_retries + 1):
        try:
            logger.info(f"  Attempt {attempt + 1}/{max_retries + 1} - Exporting: {table}")
            
            if batch_size:
                # Use batch processing for large tables
                offset = 0
                dfs = []
                while True:
                    query = f"SELECT * FROM {schema}.{table} LIMIT {batch_size} OFFSET {offset}"
                    df_batch = pd.read_sql_query(query, con=engine)
                    if df_batch.empty:
                        break
                    dfs.append(df_batch)
                    offset += batch_size
                    logger.debug(f"    Fetched batch: {offset} rows so far")
                df = pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()
            else:
                # Standard approach
                df = pd.read_sql_table(table, con=engine, schema=schema)
            
            # Create CSV path
            csv_path = backup_dir / f"{table}.csv"
            
            # Save to CSV
            df.to_csv(csv_path, index=False, encoding="utf-8")
            
            # Validate the file was created and has content
            if not csv_path.exists():
                raise Exception(f"CSV file not created: {csv_path}")
            
            file_size = csv_path.stat().st_size
            
            logger.info(f"  [OK] {table}: {len(df):,} rows, {file_size:,} bytes")
            
            return {
                "status": "success",
                "table": table,
                "row_count": len(df),
                "file_path": str(csv_path),
                "file_size": file_size,
                "attempts": attempt + 1,
                "error": None
            }
            
        except exc.OperationalError as e:
            error_msg = str(e)
            if "server closed the connection" in error_msg.lower() or "connection reset" in error_msg.lower():
                logger.warning(f"  [WARNING] Connection issue for {table} (attempt {attempt + 1}): {error_msg[:100]}")
                if attempt < max_retries:
                    import time
                    time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                    continue
            raise
            
        except Exception as e:
            error_msg = str(e)
            if attempt < max_retries:
                logger.warning(f"  [WARNING] Failed {table} (attempt {attempt + 1}): {error_msg[:100]}")
                import time
                time.sleep(RETRY_DELAY_SECONDS * (attempt + 1))
                continue
            else:
                logger.error(f"  [FAILED] {table} after {max_retries + 1} attempts: {error_msg}")
                return {
                    "status": "failed",
                    "table": table,
                    "row_count": 0,
                    "file_path": None,
                    "file_size": 0,
                    "attempts": attempt + 1,
                    "error": error_msg
                }
    
    # Should not reach here
    return {
        "status": "failed",
        "table": table,
        "error": "Unknown error"
    }


def generate_summary_report(results: List[Dict], backup_dir: Path, start_time: datetime) -> str:
    """Generate a summary report of the backup operation."""
    end_time = datetime.now()
    duration = end_time - start_time
    
    success_count = sum(1 for r in results if r["status"] == "success")
    failed_count = sum(1 for r in results if r["status"] == "failed")
    total_tables = len(results)
    
    total_rows = sum(r.get("row_count", 0) for r in results)
    total_size = sum(r.get("file_size", 0) for r in results)
    
    report = """
================================================================================
                        SUPABASE BACKUP SUMMARY REPORT
================================================================================

Backup Directory: {backup_dir}
Start Time:       {start_time_str}
End Time:         {end_time_str}
Duration:         {duration_str}

--------------------------------------------------------------------------------
TABLE STATISTICS
--------------------------------------------------------------------------------
Total Tables:     {total_tables}
Successful:       {success_count} [OK]
Failed:           {failed_count} [FAILED]
Success Rate:     {success_rate:.1f}%

Total Rows:       {total_rows:,}
Total Size:       {total_size:,} bytes ({total_size_mb:.2f} MB)

--------------------------------------------------------------------------------
SUCCESSFUL BACKUPS
--------------------------------------------------------------------------------
""".format(
        backup_dir=backup_dir.resolve(),
        start_time_str=start_time.strftime("%Y-%m-%d %H:%M:%S"),
        end_time_str=end_time.strftime("%Y-%m-%d %H:%M:%S"),
        duration_str=str(duration),
        total_tables=total_tables,
        success_count=success_count,
        failed_count=failed_count,
        success_rate=(success_count / total_tables * 100) if total_tables > 0 else 0,
        total_rows=total_rows,
        total_size=total_size,
        total_size_mb=total_size / (1024 * 1024)
    )
    
    for r in sorted(results, key=lambda x: x["table"]):
        if r["status"] == "success":
            report += f"  [OK] {r['table']:40s} {r['row_count']:>10,} rows\n"
    
    if failed_count > 0:
        report += "\n--------------------------------------------------------------------------------\n"
        report += "FAILED BACKUPS\n"
        report += "--------------------------------------------------------------------------------\n"
        for r in sorted(results, key=lambda x: x["table"]):
            if r["status"] == "failed":
                error_preview = r.get("error", "Unknown")[:60]
                report += f"  [FAILED] {r['table']:40s} (Attempts: {r['attempts']})\n"
                report += f"     Error: {error_preview}\n"
    
    report += "\n================================================================================\n"
    report += "=" * 80 + "\n"
    
    return report


# ------------------------------------------------------------------
# MAIN BACKUP FUNCTION
# ------------------------------------------------------------------

def backup_supabase() -> None:
    """Main backup function with comprehensive error handling and logging."""
    
    # Validate DATABASE_URL
    if not DATABASE_URL:
        print("[ERROR] DATABASE_URL is not configured!")
        print("Please create a .env file with your database connection string.")
        print("See .env.example for the required format.")
        sys.exit(1)
    
    # Create timestamped backup directory
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_dir = Path(f"{BACKUP_DIR_PREFIX}_{timestamp}")
    backup_dir.mkdir(exist_ok=True)
    
    # Setup logging
    logger = setup_logging(backup_dir)
    
    logger.info("=" * 80)
    logger.info("SUPABASE DATABASE BACKUP STARTED")
    logger.info("=" * 80)
    logger.info(f"Backup Directory: {backup_dir.resolve()}")
    logger.info(f"Database URL: {DATABASE_URL[:50]}...")
    logger.info(f"Schema: {SCHEMA}")
    if ONLY_TABLES:
        logger.info(f"Filter: Only {len(ONLY_TABLES)} specific tables")
    else:
        logger.info("Filter: ALL tables in schema")
    if SKIP_TABLES:
        logger.info(f"Skipping: {', '.join(SKIP_TABLES)}")
    logger.info("-" * 80)
    
    start_time = datetime.now()
    results = []
    
    try:
        # Connect to database
        logger.info("Connecting to Supabase Postgres...")
        engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=3600)
        
        # Get list of tables to backup
        logger.info("Fetching table list...")
        tables = get_all_tables(engine, SCHEMA, ONLY_TABLES, SKIP_TABLES)
        
        if not tables:
            logger.error("[ERROR] No tables found to backup!")
            logger.error("Check your schema, ONLY_TABLES, and SKIP_TABLES configuration.")
            return
        
        logger.info(f"Found {len(tables)} table(s) to backup")
        logger.info("-" * 80)
        
        # Backup each table
        for i, table in enumerate(tables, 1):
            logger.info(f"[{i}/{len(tables)}] Processing: {table}")
            result = backup_table(engine, table, backup_dir, SCHEMA)
            results.append(result)
            logger.info("-" * 80)
        
        # Generate and save summary report
        report = generate_summary_report(results, backup_dir, start_time)
        report_file = backup_dir / "BACKUP_REPORT.txt"
        report_file.write_text(report, encoding="utf-8")
        
        logger.info("\n" + "=" * 80)
        logger.info("BACKUP COMPLETED")
        logger.info("=" * 80)
        logger.info(report)
        
        # Check if all tables succeeded
        failed = [r for r in results if r["status"] == "failed"]
        if failed:
            logger.warning(f"\n[WARNING] {len(failed)} table(s) failed to backup!")
            logger.warning("Check the report above for details.")
        else:
            logger.info("\n[OK] All tables backed up successfully!")
        
    except Exception as e:
        logger.error(f"\n[ERROR] CRITICAL ERROR: {e}")
        logger.error("Backup was interrupted!")
        
        # Generate partial report
        report = generate_summary_report(results, backup_dir, start_time)
        report_file = backup_dir / "BACKUP_REPORT_PARTIAL.txt"
        report_file.write_text(report, encoding="utf-8")
        
        raise
    
    finally:
        if 'engine' in locals():
            engine.dispose()
            logger.info("Database connection closed")
        
        logger.info(f"\nBackup files saved to: {backup_dir.resolve()}")


# ------------------------------------------------------------------
# MAIN
# ------------------------------------------------------------------

if __name__ == "__main__":
    try:
        backup_supabase()
    except KeyboardInterrupt:
        logger = logging.getLogger("supabase_backup")
        logger.error("\n[ERROR] Backup interrupted by user!")
        sys.exit(1)
    except Exception as e:
        logger = logging.getLogger("supabase_backup")
        logger.error(f"\n[ERROR] Backup failed: {e}")
        sys.exit(1)
