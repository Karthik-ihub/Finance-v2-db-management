# Supabase Database Backup & Restore Tool

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

A comprehensive Python tool for backing up and restoring Supabase PostgreSQL databases with robust error handling, retry logic, and detailed reporting.

**Backup** your existing database to CSV files, then **Restore** to a new database with a single command.

## Features

### Backup Features
- ✅ **Comprehensive Backup**: Backs up all tables in your Supabase database to CSV files
- ✅ **Retry Mechanism**: Automatic retries for failed tables (configurable)
- ✅ **Detailed Logging**: Both console and file logging with timestamps
- ✅ **Progress Tracking**: Shows progress as [X/Y] for each table
- ✅ **Summary Report**: Generates a detailed BACKUP_REPORT.txt with statistics
- ✅ **Validation**: Verifies files were created and contain data
- ✅ **Configuration**: Easy setup via `.env` file
- ✅ **Resilient**: Handles connection issues and large datasets

### Migration Features
- ✅ **Direct Database-to-Database**: No intermediate CSV files needed
- ✅ **Schema Preservation**: Copies exact table structure (columns, types, constraints)
- ✅ **Type Preservation**: Maintains exact PostgreSQL data types
- ✅ **Flexible Options**: Force overwrite, skip existing, or only copy data
- ✅ **Incremental Migration**: Only migrates missing tables
- ✅ **Batch Copy**: Efficient data transfer with chunking (10,000 rows per batch)
- ✅ **Progress Tracking**: Real-time feedback during migration

## Quick Start

### 1. Clone and Setup

```bash
# Clone the repository
git clone <repository-url>
cd Finance-V2-DB-Management

# Copy the example environment file
cp .env.example .env

# Edit .env with your Supabase credentials
nano .env  # or use your preferred editor
```

### 2. Install Dependencies

```bash
pip install pandas sqlalchemy python-dotenv
```

### 3. Run Backup

```bash
# Run the backup script
python backup.py
```

This will:
- Create a new timestamped backup directory (e.g., `supabase_backup_20261103_153000`)
- Backup all tables from your database
- Generate a detailed report (`BACKUP_REPORT.txt`)
- Log everything to `backup.log`

## Configuration

Edit the `.env` file to configure your backup:

```env
# Required: Your Supabase database connection URL
DATABASE_URL=postgresql://postgres:[YOUR-PASSWORD]@aws-0-[REGION].pooler.supabase.com:6543/postgres?

# Database schema to backup (default: public)
SCHEMA=public

# Tables to skip (comma-separated, default: _prisma_migrations)
SKIP_TABLES=_prisma_migrations

# Only backup specific tables (comma-separated, leave empty for ALL)
ONLY_TABLES=

# Retry settings
MAX_RETRIES=3
RETRY_DELAY_SECONDS=2

# Batch size for large tables (leave empty for no batching)
BATCH_SIZE=
```

### Getting Your DATABASE_URL

1. Go to your Supabase project dashboard
2. Navigate to **Project Settings** → **Database**
3. Under **Connection String**, copy the **Connection string** (not the URI)
4. Replace the placeholder in `.env` with your actual connection string

## Usage Examples

### Backup All Tables

```bash
python backup.py
```

### Backup Specific Tables Only

```bash
# Edit .env and set:
ONLY_TABLES=customer_master,invoice_master,sales_adjustment_master

python backup.py
```

### Skip System Tables

```bash
# Edit .env and set:
SKIP_TABLES=_prisma_migrations,ap_balance_snapshot,ar_balance_snapshot

python backup.py
```

### Custom Schema

```bash
# Edit .env and set:
SCHEMA=custom_schema

python backup.py
```

### Migrate to New Database

```bash
# 1. Set your target database URL in .env
TARGET_DATABASE_URL=postgresql://postgres:new_password@new-host:5432/postgres?

# 2. Run direct migration from source to target
python migrate.py

# Or pass target URL as argument
python migrate.py --target-url postgresql://...
```

### Migrate with Force Overwrite

```bash
# Overwrite existing tables in target database
python migrate.py --force
```

### Migrate Without Creating Tables

```bash
# Only copy data to existing tables (don't create new ones)
python migrate.py --no-create
```

## Output Structure

After running the backup, you'll find:

```
Finance-V2-DB-Management/
├── backup.py              # Main backup script
├── .env                   # Your configuration (gitignored)
├── .env.example           # Example configuration
├── .gitignore             # Git ignore rules
├── README.md              # This file
└── supabase_backup_YYYYMMDD_HHMMSS/  # Backup directory
    ├── table1.csv         # Table data
    ├── table2.csv
    ├── ...
    ├── backup.log         # Log file
    └── BACKUP_REPORT.txt  # Summary report
```

## Backup Report

The `BACKUP_REPORT.txt` file contains:

- Start and end timestamps
- Duration of backup
- Table statistics (total, successful, failed)
- Row counts and file sizes for each table
- List of failed tables (if any) with error details

Example:
```
================================================================================
                        SUPABASE BACKUP SUMMARY REPORT
================================================================================

Backup Directory: /path/to/supabase_backup_20261103_153000
Start Time:       2026-11-03 15:30:00
End Time:         2026-11-03 15:32:15
Duration:         0:02:15.234567

--------------------------------------------------------------------------------
TABLE STATISTICS
--------------------------------------------------------------------------------
Total Tables:     49
Successful:       48 [OK]
Failed:           1 [FAILED]
Success Rate:     97.9%

Total Rows:       123,456
Total Size:       45,678,901 bytes (43.56 MB)

--------------------------------------------------------------------------------
SUCCESSFUL BACKUPS
--------------------------------------------------------------------------------
  [OK] customer_master                       1,234 rows
  [OK] invoice_master                        5,678 rows
  ...

--------------------------------------------------------------------------------
FAILED BACKUPS
--------------------------------------------------------------------------------
  [FAILED] large_table (Attempts: 3)
     Error: Connection timeout after 60 seconds
```

## Error Handling

The script handles various error scenarios:

- **Connection Issues**: Automatic reconnection with retry logic
- **Large Tables**: Optional batch processing (configure `BATCH_SIZE`)
- **Permission Errors**: Logs and continues with other tables
- **Keyboard Interrupt**: Graceful shutdown with partial report
- **Missing Configuration**: Clear error message if `.env` is incomplete

## Project Structure

```
Finance-V2-DB-Management/
├── backup.py              # Backup to CSV files
├── migrate.py             # Direct database-to-database migration
├── .env                   # Configuration (gitignored)
├── .env.example           # Example configuration
├── .gitignore             # Git ignore rules
├── LICENSE                # MIT License
├── README.md              # Documentation
└── requirements.txt       # Python dependencies
```

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## Acknowledgments

- Built with Python, pandas, and SQLAlchemy
- Designed for Supabase PostgreSQL databases
- Inspired by the need for reliable database backups

---

**Note**: This tool is designed for backing up Supabase databases. Always verify your backups and store them securely. The authors are not responsible for data loss or backup failures.
