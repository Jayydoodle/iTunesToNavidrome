# iTunes to Navidrome Migration Script

Migrate your play counts, ratings, and play dates from iTunes/Apple Music to Navidrome.

**Tested with:** Navidrome 0.55+ and 0.58+ (including multi-library support)

## Features

- **Intelligent path matching** - Automatically matches iTunes tracks to Navidrome files using suffix matching (no path configuration needed)
- **Interactive mode** - Prompts for missing arguments and shows available users
- **Dry run support** - Test the migration before making changes
- **Detailed logging** - Timestamped log directories for each run
- **Unicode handling** - Handles macOS NFD vs Linux NFC normalization

## Prerequisites

- Python 3.7+
- No additional packages required (uses only standard library)
- A backup of your `navidrome.db` file (CRITICAL!)

## Quick Start

### Step 1: Export your iTunes Library

In iTunes/Apple Music:
1. Go to **File → Library → Export Library...**
2. Save as `Library.xml`

### Step 2: Get your Navidrome database

1. **Stop Navidrome** (important!)
2. Copy `navidrome.db` from your Navidrome data directory
3. **Make a backup copy** before proceeding

### Step 3: Run a dry run

```bash
python itunes_to_navidrome.py navidrome.db Library.xml --dry-run
```

The script will:
1. Prompt you to select a Navidrome user
2. Show sample path matches (verify these look correct!)
3. Report how many tracks would be matched

### Step 4: Run the actual migration

Once you're happy with the dry run results:

```bash
python itunes_to_navidrome.py navidrome.db Library.xml
```

### Step 5: Restore the database

1. Stop Navidrome
2. Replace the original `navidrome.db` with your modified copy
3. Start Navidrome
4. Verify your play counts and ratings appear!

## Usage

```
python itunes_to_navidrome.py [navidrome_db] [itunes_xml] [user_id] [options]
```

All positional arguments are optional. If omitted, the script will prompt for them interactively.

### Arguments

| Argument | Description |
|----------|-------------|
| `navidrome_db` | Path to navidrome.db file |
| `itunes_xml` | Path to iTunes Library.xml file |
| `user_id` | Navidrome user ID (will show list if omitted) |

### Options

| Option | Description |
|--------|-------------|
| `--dry-run` | Show what would be done without making changes |
| `--replace` | Replace existing play counts instead of adding (see below) |
| `--verbose, -v` | Enable verbose logging (shows each matched track) |
| `--yes, -y` | Skip confirmation prompt |
| `--sample N` | Number of sample paths to show (default: 5) |

### Examples

```bash
# Fully interactive mode
python itunes_to_navidrome.py

# Specify database and XML, prompt for user
python itunes_to_navidrome.py navidrome.db Library.xml

# Full command with all arguments
python itunes_to_navidrome.py navidrome.db Library.xml abc123def

# Dry run with verbose output
python itunes_to_navidrome.py navidrome.db Library.xml abc123def --dry-run -v

# Replace mode (overwrite existing play counts)
python itunes_to_navidrome.py navidrome.db Library.xml abc123def --replace
```

## How Path Matching Works

The script uses **suffix matching** to find corresponding files between iTunes and Navidrome. This means you don't need to configure path mappings.

For example, if iTunes has:
```
file:///Users/jason/Music/iTunes/iTunes Media/Music/Artist/Album/Song.mp3
```

And Navidrome has:
```
/mnt/music/Artist/Album/Song.mp3
```

The script matches them by finding the longest common suffix (`Artist/Album/Song.mp3`).

### Handling Ambiguous Matches

If multiple Navidrome files match the same suffix (e.g., two files named `01 Track.mp3` in different albums), the script:
1. Tries progressively longer suffixes to find a unique match
2. If still ambiguous, logs the files to `ambiguous_matches.log` and uses the first match

## What Gets Migrated

| iTunes Field | Navidrome Field | Notes |
|-------------|-----------------|-------|
| Play Count | play_count | Added or replaced depending on mode |
| Rating (0-100) | rating (0-5) | Converted (100→5, 80→4, etc.) |
| Play Date UTC | play_date | Keeps the more recent date |

The script updates annotations for:
- **media_file** (individual tracks)
- **album** (aggregated from all tracks)
- **artist** (aggregated from all tracks)

## Add vs Replace Mode

### Default (Add Mode)
Play counts are **ADDED** to existing values:
- Existing: 10 plays + iTunes: 5 plays = **15 plays**

**Warning:** Running the script twice will DOUBLE your play counts!

### Replace Mode (`--replace`)
Play counts **OVERWRITE** existing values:
- Existing: 10 plays + iTunes: 5 plays = **5 plays**

Use `--replace` if:
- You need to re-run the migration
- You want iTunes to be the source of truth

## Output Files

Each run creates a timestamped log directory:

```
logs/
└── log_20260119_143052/
    ├── migration.log        # Detailed migration log
    ├── not_found.log        # iTunes tracks not found in Navidrome
    └── ambiguous_matches.log # Tracks with multiple possible matches
```

### not_found.log

Lists iTunes paths that couldn't be matched. Common reasons:
- File exists in iTunes but not in Navidrome
- Filename differences (e.g., `Song 1.mp3` vs `Song.mp3`)
- File format differences (e.g., `.m4p` protected files)

### ambiguous_matches.log

Lists tracks where multiple Navidrome files matched. Format:
```
/iTunes/path/to/song.mp3
Navidrome/path/option1.mp3
Navidrome/path/option2.mp3

/iTunes/path/to/another.mp3
Navidrome/different/option1.mp3
Navidrome/different/option2.mp3
```

## Troubleshooting

### Low match rate

Check `not_found.log` for paths that couldn't be matched. Common issues:

1. **Different filenames** - iTunes and Navidrome have different names for the same file
2. **Missing files** - Files in iTunes that aren't in Navidrome
3. **Protected files** - `.m4p` files from old iTunes Store purchases
4. **Video files** - `.m4v` files won't be in a music-only Navidrome library

### Duplicate play counts after re-run

You ran the script twice without `--replace`. To fix:
1. Restore your database from backup
2. Run with `--replace` flag

### "User ID not found"

The script will show available users. Select from the list or copy the correct ID.

### Script crashes mid-way

The script uses database transactions. If it crashes, changes are rolled back automatically.

## Schema Compatibility

The script automatically detects your Navidrome schema:
- Handles `rated_at` column (added in newer versions)
- Works with both single-library and multi-library setups (0.58+)

## License

Public Domain - Use freely, modify as needed.

## Credits

- Inspired by [Stampede's itunes-navidrome-migration](https://github.com/Stampede/itunes-navidrome-migration)
- Schema research from [Race Dorsey's migration guide](https://racedorsey.com/posts/2025/itunes-navidrome-migration/)
