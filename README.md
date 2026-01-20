# iTunes to Navidrome Migration Scripts

Migrate your play counts, ratings, and play dates from iTunes/Apple Music to Navidrome.

**Tested with:** Navidrome 0.55+ and 0.58+ (including multi-library support)

## Prerequisites

- Python 3.7+
- No additional packages required (uses only standard library)
- A backup of your `navidrome.db` file (CRITICAL!)

## Files

- `itunes_to_navidrome.py` - Main migration script
- `inspect_navidrome.py` - Helper to examine your Navidrome database

## Quick Start

### Step 1: Export your iTunes Library

In iTunes/Apple Music on your Mac:
1. Go to **File → Library → Export Library...**
2. Save as `Library.xml`
3. Copy this file to where you'll run the scripts

### Step 2: Get your Navidrome database

1. **Stop Navidrome** (important!)
2. Copy `navidrome.db` from your Navidrome data directory
3. **Make a backup copy** before proceeding

### Step 3: Inspect your database

```bash
python inspect_navidrome.py navidrome.db
```

This will show you:
- Your Navidrome user ID (you'll need this)
- Sample file paths in Navidrome
- Current annotation counts

### Step 4: Determine your path mapping

Your iTunes Library.xml contains paths like:
```
file://localhost/C:/Users/jason/Music/iTunes/iTunes%20Media/Music/Artist/Album/Song.mp3
```
(Mac version:)
```
file:///Users/jason/Music/iTunes/iTunes%20Media/Music/Artist/Album/Song.mp3
```

Your Navidrome has paths like:
```
/mnt/music/Artist/Album/Song.mp3
```

You need to identify:
- **iTunes base path**: The common prefix to strip (e.g., `/Users/jason/Music/iTunes/iTunes Media/Music`)
- **Navidrome base path**: What to replace it with (e.g., `/mnt/music`)

### Step 5: Dry run (test without changes)

```bash
python itunes_to_navidrome.py navidrome.db Library.xml YOUR_USER_ID \
    "/Users/jason/Music/iTunes/iTunes Media/Music" \
    "/mnt/music" \
    --dry-run
```

The script will:
1. Show sample path conversions (verify these look right!)
2. Ask for confirmation
3. Report how many tracks matched

Check `not_found.log` for tracks that couldn't be matched.

### Step 6: Run the actual migration

Once you're happy with the dry run results:

```bash
python itunes_to_navidrome.py navidrome.db Library.xml YOUR_USER_ID \
    "/Users/jason/Music/iTunes/iTunes Media/Music" \
    "/mnt/music"
```

### Step 7: Restore the database

1. Stop Navidrome
2. Replace the original `navidrome.db` with your modified copy
3. Start Navidrome
4. Verify your play counts and ratings appear!

## Command Line Options

```
usage: itunes_to_navidrome.py [-h] [--dry-run] [--replace] [--verbose] [--yes] [--sample N]
                              navidrome_db itunes_xml user_id itunes_path navidrome_path

Arguments:
  navidrome_db     Path to navidrome.db file
  itunes_xml       Path to iTunes Library.xml file
  user_id          Navidrome user ID (find with inspect_navidrome.py)
  itunes_path      Base path in iTunes to replace
  navidrome_path   Corresponding path in Navidrome

Options:
  --dry-run        Show what would be done without making changes
  --replace        Replace existing play counts instead of adding (see below)
  --verbose, -v    Enable verbose logging (shows each matched track)
  --yes, -y        Skip confirmation prompt (for scripted/non-interactive use)
  --sample N       Number of sample paths to show (default: 5)
```

## What Gets Migrated

For each track in your iTunes library:

| iTunes Field | Navidrome Field | Notes |
|-------------|-----------------|-------|
| Play Count | play_count | Added or replaced depending on mode |
| Rating (0-100) | rating (0-5) | Converted (100→5, 80→4, etc.) |
| Play Date UTC | play_date | Keeps the more recent date |

The script updates annotations for:
- **media_file** (individual tracks)
- **album** (aggregate album stats)
- **artist** (aggregate artist stats)

## Important: Add vs Replace Mode

### Default (Add Mode)
By default, play counts are **ADDED** to existing values:
- Existing play count: 10
- iTunes play count: 5
- Result: 15

**⚠️ WARNING:** Running the script twice will DOUBLE your play counts!

### Replace Mode (`--replace`)
With `--replace`, play counts **OVERWRITE** existing values:
- Existing play count: 10
- iTunes play count: 5
- Result: 5

Use `--replace` if:
- You need to re-run the migration after fixing paths
- You want iTunes to be the source of truth

## Album/Artist Play Count Aggregation

When migrating, album and artist play counts are **aggregated** from all their tracks:

If Album X has:
- Track 1: 10 plays
- Track 2: 5 plays

After migration, Album X will show **15 plays** (10 + 5).

This matches how Navidrome normally calculates album/artist statistics.

## Troubleshooting

### "User ID not found"

Run `inspect_navidrome.py` to see available users and their IDs.

### Low match rate

Check `not_found.log` for the paths that couldn't be matched. Common issues:

1. **Path prefix mismatch**: Adjust your iTunes/Navidrome base paths
2. **Case sensitivity**: Navidrome on Linux is case-sensitive
3. **Special characters**: Files with `&`, `'`, or unicode characters
4. **Unicode normalization**: macOS uses NFD, Linux uses NFC (script handles this)
5. **Missing files**: Files in iTunes that aren't in Navidrome yet

### "no such table: library"

You're running Navidrome < 0.58. The script should still work—the library table is only used for inspection.

### Duplicate play counts after re-run

You ran the script twice without `--replace`. Restore from backup and use `--replace` flag.

### Script crashes mid-way

The script uses database transactions. If it crashes, changes are rolled back automatically. Your database should be unchanged.

## How It Works

1. Parses iTunes `Library.xml` (a plist file) to extract track metadata
2. Converts iTunes file:// URLs to Navidrome-style paths
3. Normalizes Unicode (NFD → NFC) for cross-platform compatibility
4. For each track with play data:
   - Finds the matching `media_file` in Navidrome by path
   - Creates or updates an `annotation` record for the track
   - Also updates album and artist annotations (so artist play counts work!)
5. All changes are committed in a single transaction (atomic)

## Schema Compatibility

The script automatically detects your Navidrome schema:
- Handles `rated_at` column (added in newer versions)
- Works with both single-library and multi-library setups (0.58+)
- Uses Navidrome's PID-based IDs (0.55+)

## Output Files

- `migration.log` - Detailed log of the migration process
- `not_found.log` - List of iTunes paths that couldn't be matched in Navidrome

## License

Public Domain - Use freely, modify as needed.

## Credits

- Inspired by [Stampede's itunes-navidrome-migration](https://github.com/Stampede/itunes-navidrome-migration)
- Schema research from [Race Dorsey's migration guide](https://racedorsey.com/posts/2025/itunes-navidrome-migration/)
- Written by Claude (Anthropic) - January 2026
