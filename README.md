# Supabase Database Backup & Migration Tool

This project provides a PostgreSQL backup and migration workflow that uses the official `pg_dump` and `pg_restore` utilities to produce reliable custom-format archives.

**IMPORTANT:** This is a production migration tool. It does NOT run any restore, migration, or destructive operation automatically. All restore operations require explicit configuration, validation, and approval.

## What Was Wrong Before

The original script exported each table into CSV files. That method only captured table data and silently ignored database structure such as:

- ENUM types and custom PostgreSQL types
- Foreign keys and primary keys
- Indexes, sequences, and triggers
- Views and materialized views
- Functions, procedures, and policies
- Extension dependencies and schema objects

Because those objects were missing, a migration based only on CSV files often failed to recreate a working application.

## Current Backup Approach

The script creates a **complete custom-format PostgreSQL archive** using `pg_dump` and verifies the archive with `pg_restore --list` before reporting success.

**This is the primary backup**, and CSV export remains optional for table-level inspection only.

## PostgreSQL Backup Behavior

A full `pg_dump --format=c` archive includes the database objects and data that are accessible to the connecting role, including:

- Schemas and tables
- ENUMs and custom types
- Primary keys and foreign keys
- Constraints and indexes
- Sequences
- Views and materialized views
- Functions and procedures
- Triggers
- Extension objects and relevant dependencies
- RLS policies

This is the **correct primary backup** for a Supabase migration. CSV files are only a supplementary inspection backup.

## Mandatory Safety Rules

### For All Operations

1. **Passwords MUST NOT be in database URLs** - Use PGPASSWORD or PGPASSFILE environment variables
2. **Validation restores MUST use a dedicated validation database** - Never use production for validation
3. **Validation database identity MUST be explicitly configured** - The script verifies target matches configured identity
4. **Archive readability does NOT prove restore success** - A separate validation restore test is REQUIRED
5. **NO automatic destructive operations** - DROP, TRUNCATE, DELETE, --clean, --create are NEVER used automatically

### For Validation Restores (`restore_validation.py`)

1. **Explicit identity required** - Set VALIDATION_DATABASE_HOST, VALIDATION_DATABASE_NAME, or VALIDATION_DATABASE_URL
2. **Target must match identity** - The script rejects any target that doesn't match the configured validation identity
3. **Target must NOT be source** - The script compares with DATABASE_URL and rejects if they match
4. **Application schemas must be empty** - The script checks for existing objects in application schemas
5. **Supabase-managed schemas handled** - Platform schemas (auth, storage, etc.) are excluded from emptiness checks
6. **Uses --exit-on-error** - Restore stops on any error; partial restores are detected
7. **Password-free URLs required** - The script rejects password-bearing connection strings

### For Production Migrations (`migrate.py`)

1. **Explicit approval required** - Must use both `--target-mode=approved-target` AND `--allow-production`
2. **Validation mode is default** - Without explicit approval, only validation restores are allowed
3. **Target identity validation** - For validation mode, target must match configured validation identity
4. **Source/target comparison** - Script prevents restoring to the same database as source
5. **Conflict detection** - Target is inspected for conflicting application objects
6. **Uses --exit-on-error** - Restore stops on any error
7. **Detailed report** - Generates report distinguishing fully successful vs partial/failed restore

## Required Tools

Install PostgreSQL client tools on your machine so that `pg_dump` and `pg_restore` are available on PATH.

On Windows, install PostgreSQL 17 or 18 client tools from the official PostgreSQL installer. For your case, a PostgreSQL 18.6 client is expected to work with a PostgreSQL 17.11 source server for dump/restore operations.

Confirm with:

```powershell
pg_dump --version
pg_restore --version
```

If `pg_dump` or `pg_restore` is not on PATH, set `PG_DUMP_PATH` and `PG_RESTORE_PATH` in `.env`.

## Secure Credentials

**NEVER place a password in `DATABASE_URL` or any database URL that is passed to a command-line process.**

Use a password-free URI in `.env`, then set the secret in a secure environment variable or `.pgpass` file:

```powershell
$env:PGPASSWORD = "your-secret-password"
# or
$env:PGPASSFILE = "C:\path\to\.pgpass"
```

### .pgpass File (Recommended)

Create a `.pgpass` file with the following format:

```
hostname:port:database:username:password
```

Example:
```
db.your-project-ref.supabase.co:5432:postgres:postgres:your-secret-password
```

On Windows, place it at: `C:\Users\[YOUR-USER]\.pgpass`

Set file permissions to 600 (read/write only by owner).

Then set in `.env`:
```env
PGPASSFILE=C:\Users\YourUser\.pgpass
DATABASE_URL=postgresql://postgres@db.your-project-ref.supabase.co:5432/postgres
```

The scripts will automatically use PGPASSWORD or PGPASSFILE for authentication.

## Setup

```powershell
Copy-Item .env.example .env
notepad .env
```

### Configure Your Databases

Update the following in `.env`:

1. **SOURCE_DATABASE_URL** or **DATABASE_URL** - Your source database to back up
2. **TARGET_DATABASE_URL** - Your target database for migration
3. **VALIDATION_DATABASE_URL** - Your dedicated validation database
4. **VALIDATION_DATABASE_HOST** - Explicit host for validation DB
5. **VALIDATION_DATABASE_NAME** - Explicit database name for validation DB
6. **VALIDATION_PROJECT_REF** - Project reference for validation DB
7. **PGPASSWORD** or **PGPASSFILE** - Authentication (NEVER in URLs)

All database URLs must be **password-free**.

### Example Configuration

```env
# Source database
SOURCE_DATABASE_URL=postgresql://postgres@db.source-project.supabase.co:5432/postgres
DATABASE_URL=postgresql://postgres@db.source-project.supabase.co:5432/postgres
PGPASSFILE=C:\Users\YourUser\.pgpass

# Target database (for production migration)
TARGET_DATABASE_URL=postgresql://postgres@db.target-project.supabase.co:5432/postgres
TARGET_DATABASE_MODE=approved-target

# Validation database (REQUIRED for validation restores)
VALIDATION_DATABASE_URL=postgresql://postgres@db.validation-project.supabase.co:5432/supabase_restore_validation
VALIDATION_DATABASE_HOST=db.validation-project.supabase.co
VALIDATION_DATABASE_NAME=supabase_restore_validation
VALIDATION_PROJECT_REF=validation-project
```

## Install Dependencies

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

## Run the Backup

```powershell
python .\backup.py
```

The script will:

- Create a new timestamped backup folder
- Generate a schema inventory report (SCHEMA_INVENTORY.txt)
- Generate a custom-format `.dump` archive with `pg_dump`
- Verify the archive by running `pg_restore --list`
- Generate a SHA256 checksum and checksum file
- Create a manifest listing the archive contents
- Write a backup report and metadata JSON
- Optionally export selected tables to CSV if `EXPORT_CSV=true`

### Schema Inventory Report

The backup generates a `SCHEMA_INVENTORY.txt` file that:

- Identifies **application-owned schemas** separately from **Supabase-managed schemas**
- Shows object counts (tables, views, functions, sequences) for each schema
- Classifies schemas as: Application, Supabase-managed, or System
- **Warns if DUMP_SCHEMAS creates a partial backup**

**IMPORTANT:** Review this file before migration. The backup does NOT automatically exclude or include schemas based on assumptions. An explicit reviewed schema scope is **REQUIRED** before treating a migration backup as ready for migration.

## Verify a Backup

### 1) Inspect the backup folder

```powershell
Get-ChildItem .\supabase_backup_YYYYMMDD_HHMMSS
```

### 2) Confirm the archive exists and is not empty

```powershell
Get-Item .\supabase_backup_YYYYMMDD_HHMMSS\*.dump
```

### 3) Read the archive contents (read-only verification)

```powershell
& pg_restore --list .\supabase_backup_YYYYMMDD_HHMMSS\supabase_backup_YYYYMMDD_HHMMSS.dump
```

### 4) Generate a checksum

```powershell
Get-FileHash .\supabase_backup_YYYYMMDD_HHMMSS\supabase_backup_YYYYMMDD_HHMMSS.dump -Algorithm SHA256 | Format-List
```

### 5) View the manifest and reports

```powershell
Get-Content .\supabase_backup_YYYYMMDD_HHMMSS\BACKUP_MANIFEST.txt -TotalCount 60
Get-Content .\supabase_backup_YYYYMMDD_HHMMSS\BACKUP_REPORT.txt -TotalCount 200
Get-Content .\supabase_backup_YYYYMMDD_HHMMSS\SCHEMA_INVENTORY.txt -TotalCount 100
```

**WARNING:** The `pg_restore --list` step validates **archive readability only**. This does NOT prove that a restore will succeed. A separate restore test in a validation database is REQUIRED.

## Validation Restore (MANDATORY)

Before any production migration, you **MUST** perform a validation restore test in a dedicated validation database.

### Create a Validation Database

1. Create a **new, empty** database in your Supabase project or database server
2. **Do NOT use your production database** for validation
3. Configure the validation database identity in `.env`:
   ```env
   VALIDATION_DATABASE_URL=postgresql://postgres@db.your-project.supabase.co:5432/supabase_restore_validation
   VALIDATION_DATABASE_HOST=db.your-project.supabase.co
   VALIDATION_DATABASE_NAME=supabase_restore_validation
   VALIDATION_PROJECT_REF=your-project
   ```

### Run the Validation Restore

```powershell
python .\restore_validation.py `
  --archive .\supabase_backup_YYYYMMDD_HHMMSS\supabase_backup_YYYYMMDD_HHMMSS.dump `
  --database-url "postgresql://postgres@db.your-project.supabase.co:5432/supabase_restore_validation"
```

The script will:

1. **Verify validation database identity** - Rejects if target doesn't match configured identity
2. **Reject source database** - Prevents restoring to the source database
3. **Reject password-bearing URLs** - Prevents credential exposure
4. **Check for existing application objects** - Ensures application schemas are empty
5. **Handle Supabase-managed schemas** - Allows platform schemas to exist
6. **Use --exit-on-error** - Stops on any error
7. **Generate a report** - Documents the validation result

**The validation restore MUST complete successfully before proceeding to production migration.**

## Production Migration

After successful validation restore, you can perform a production migration.

**WARNING:** Production migrations require explicit approval.

```powershell
python .\migrate.py `
  --archive .\supabase_backup_YYYYMMDD_HHMMSS\supabase_backup_YYYYMMDD_HHMMSS.dump `
  --target-url "postgresql://postgres@db.target-project.supabase.co:5432/postgres" `
  --target-mode=approved-target `
  --allow-production
```

The script will:

1. **Require explicit approval** - Both `--target-mode=approved-target` AND `--allow-production` are required
2. **Validate target URL** - Rejects password-bearing URLs
3. **Compare source and target** - Prevents restoring to the same database
4. **Inspect target for conflicts** - Detects existing application objects
5. **Use --exit-on-error** - Stops on any error
6. **Generate detailed report** - Documents success or failure

### Dry Run Mode

To validate without actually running the restore:

```powershell
python .\migrate.py `
  --archive .\supabase_backup_YYYYMMDD_HHMMSS\supabase_backup_YYYYMMDD_HHMMSS.dump `
  --target-url "postgresql://postgres@db.target-project.supabase.co:5432/postgres" `
  --target-mode=approved-target `
  --allow-production `
  --dry-run
```

## Important Warnings

### About `DUMP_SCHEMAS`

For a **complete backup**, leave `DUMP_SCHEMAS` **empty**.

When `DUMP_SCHEMAS` is set to a subset, the archive is only a **partial backup** of selected schemas. This is **NOT** a complete migration backup.

```env
# For complete backup (RECOMMENDED):
DUMP_SCHEMAS=

# For partial backup (NOT RECOMMENDED for migration):
DUMP_SCHEMAS=public,custom_schema
```

**An explicit reviewed schema scope is REQUIRED before treating a migration backup as ready for migration.**

### Archive Readability vs Restore Success

**IMPORTANT:** A successful `pg_restore --list` (archive readability check) **does NOT prove** that a restore will succeed. 

The only way to prove restore success is to:
1. Create a dedicated validation database
2. Run a validation restore with `restore_validation.py`
3. Verify the restored database works correctly

### What Archive Readability Checks

- Archive file is valid PostgreSQL custom-format
- Archive can be parsed by pg_restore
- Archive contains recognizable database objects

### What Archive Readability Does NOT Check

- Whether objects can be created in the target database
- Whether there are permission issues
- Whether there are dependency issues
- Whether the target database has conflicting objects
- Whether the restore will complete without errors

## Separate Restore-Validation Procedure

A real restore test is **intentionally separate** and should be performed only against a **dedicated validation database**, not against your live target database.

```powershell
# Step 1: Create a fresh validation database in Supabase
# Step 2: Configure validation identity in .env
# Step 3: Run validation restore
python .\restore_validation.py `
  --archive .\backup\archive.dump `
  --database-url "postgresql://postgres@db.project.supabase.co:5432/supabase_restore_validation"

# Step 4: Verify the restored validation database
# Step 5: Only then proceed to production migration
```

## CSV Export Behavior

CSV export remains available, but only as a **supplementary backup**. It is **NOT** the main loss-prevention mechanism.

If you want to keep the older table-by-table inspection workflow:

```powershell
$env:EXPORT_CSV = "true"
python .\backup.py
```

Or set in `.env`:

```env
EXPORT_CSV=true
ONLY_TABLES=customer_master,invoice_master
SKIP_TABLES=
```

## Supabase Objects Not Covered by PostgreSQL Dump

A PostgreSQL dump does **NOT** migrate everything in a Supabase project. The following require **separate migration steps**:

- Supabase Auth users and authentication metadata
- Storage objects and buckets
- Edge Functions and their deployed code
- Project secrets and environment variables
- Global roles, permissions, and project-level configuration
- Realtime subscriptions and project settings outside the database

These items must be handled **separately** from the database dump.

## What Changed in the Code

### restore_validation.py (Mandatory Safety Changes)

1. **Explicit validation identity required** - Must set VALIDATION_DATABASE_HOST, VALIDATION_DATABASE_NAME, or VALIDATION_DATABASE_URL
2. **Target must match identity** - Script rejects any target that doesn't match configured identity
3. **Target must NOT be source** - Script compares with DATABASE_URL and rejects if they match
4. **Application schemas checked** - Script verifies application schemas are empty before restore
5. **Supabase-managed schemas handled** - Platform schemas (auth, storage, etc.) are allowed to exist
6. **--exit-on-error added** - Restore stops on any error
7. **Password-bearing URLs rejected** - Script rejects any URL with embedded password
8. **No destructive operations** - --clean, --create, DROP, TRUNCATE, DELETE are never used
9. **Schema emptiness check** - Prevents accidental overwrite of existing data
10. **Never trusts description/database name alone** - Explicit identity configuration is required

### migrate.py (Mandatory Safety Changes)

1. **Explicit target mode required** - Must specify validation or approved-target
2. **Validation identity enforced** - For validation mode, target must match configured identity
3. **Explicit production approval** - Requires both --target-mode=approved-target AND --allow-production
4. **Source/target comparison** - Prevents restoring to same database
5. **Conflict detection** - Inspects target for existing application objects
6. **--exit-on-error added** - Restore stops on any error
7. **Detailed report** - Distinguishes fully successful from partial/failed restore
8. **No automatic overwrites** - Existing objects are never automatically dropped or overwritten
9. **Dry run mode** - Allows validation without executing restore

### backup.py (Improvements)

1. **Schema inventory report** - Generated SCHEMA_INVENTORY.txt identifying application vs Supabase-managed schemas
2. **Password rejection** - Explicitly rejects password-bearing DATABASE_URL values
3. **Query parameter preservation** - strip_password_from_url preserves query parameters while removing password
4. **Schema scope warning** - Warns that DUMP_SCHEMAS creates partial backup, requires explicit review for migration
5. **SOURCE_DATABASE_URL alias** - Supports both SOURCE_DATABASE_URL and DATABASE_URL
6. **Read-only preflight** - Schema inventory is generated without modifying database
7. **No automatic schema filtering** - Does not assume which schemas to include/exclude

### .env.example

1. **Separate configurations** - Documents SOURCE, TARGET, and VALIDATION databases separately
2. **Explicit identity settings** - Adds VALIDATION_DATABASE_HOST, VALIDATION_DATABASE_NAME, VALIDATION_PROJECT_REF
3. **Password-free URLs** - All examples show password-free connection strings
4. **PGPASSWORD/PGPASSFILE instructions** - Clear documentation on secure credential handling
5. **Deprecated settings marked** - FORCE_OVERWRITE, CREATE_TABLES marked as unused

### README.md

1. **Updated safety documentation** - Explains all mandatory safety rules
2. **Validation restore procedure** - Detailed steps for validation restore
3. **Archive readability warning** - Clearly explains that readability != restore success
4. **Schema inventory documentation** - Explains the new schema classification report
5. **Explicit warnings** - About DUMP_SCHEMAS, validation requirements, etc.

## Notes for Beginners

Think of a PostgreSQL dump like a **complete snapshot** of the database structure and data. CSV files are like a spreadsheet export of table content only; they are useful for inspection but they do not preserve the database design.

A `pg_dump` custom-format archive is the **right choice** for a migration because it keeps the database schema and data together.

### Key Concepts

- **pg_dump**: Creates a backup archive of the database
- **pg_restore**: Restores from the archive to a database
- **Custom format (-Fc)**: Most flexible format, supports parallel restore
- **--no-owner**: Objects restored without original ownership (for portability)
- **--no-privileges**: Permissions not transferred (for portability)
- **--exit-on-error**: Stops on first error (ensures complete success or clear failure)
- **--list**: Read-only archive inspection (verifies readability only)

## License

This project is licensed under the MIT License.
