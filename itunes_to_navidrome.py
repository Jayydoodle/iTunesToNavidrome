#!/usr/bin/env python3
"""
iTunes to Navidrome Migration Script
Transfers play counts, ratings, and play dates from iTunes Library.xml to Navidrome

Tested with: Navidrome 0.55+ / 0.58+ (with multi-library support)
Author: Claude (Anthropic) - January 2026
License: Public Domain

Usage:
    python itunes_to_navidrome.py [navidrome.db] [Library.xml] [navidrome_user_id]

    If arguments are omitted, the script will prompt for them interactively.

Example:
    python itunes_to_navidrome.py navidrome.db "iTunes Library.xml" abc123

IMPORTANT NOTES:
- Play counts are ADDED to existing counts by default. Use --replace to overwrite instead.
- Album/artist play counts are aggregated from all their tracks.
- Running twice without --replace will DOUBLE your play counts!
- Always backup navidrome.db before running.
- Path matching uses suffix matching - iTunes paths are matched to Navidrome paths
  by finding the longest matching path suffix.
"""

import sqlite3
import plistlib
import sys
import os
import unicodedata
from datetime import datetime
from urllib.parse import unquote, urlparse
import argparse
import logging
from collections import defaultdict

# Logger will be configured in main() after log directory is created
logger = logging.getLogger(__name__)


def create_log_directory() -> str:
    """
    Create a timestamped log directory for this run.

    Returns the path to the created directory.
    Structure: logs/log_YYYYMMDD_HHMMSS/
    """
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    log_dir = os.path.join('logs', f'log_{timestamp}')
    os.makedirs(log_dir, exist_ok=True)
    return log_dir


def setup_logging(log_dir: str, verbose: bool = False):
    """Configure logging to write to the specified log directory."""
    log_file = os.path.join(log_dir, 'migration.log')

    # Configure the root logger
    log_level = logging.DEBUG if verbose else logging.INFO

    # Clear any existing handlers
    logger.handlers = []

    # Set up handlers
    file_handler = logging.FileHandler(log_file)
    stream_handler = logging.StreamHandler()

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.setLevel(log_level)


def parse_itunes_library(xml_path: str) -> dict:
    """Parse iTunes Library.xml and return track dictionary."""
    logger.info(f"Parsing iTunes library: {xml_path}")

    with open(xml_path, 'rb') as f:
        library = plistlib.load(f)

    tracks = library.get('Tracks', {})
    logger.info(f"Found {len(tracks)} tracks in iTunes library")

    return tracks


def normalize_unicode(text: str) -> str:
    """
    Normalize Unicode to NFC form.
    macOS uses NFD (decomposed), Linux uses NFC (composed).
    'Café' can be stored as 'Cafe\u0301' (NFD) or 'Caf\u00e9' (NFC).
    """
    if text is None:
        return None
    return unicodedata.normalize('NFC', text)


def extract_path_from_itunes_location(itunes_location: str) -> str:
    """
    Extract and normalize the file path from an iTunes file:// URL.

    iTunes stores paths like:
        file://localhost/C:/Users/jason/Music/iTunes/iTunes%20Media/Music/Artist/Album/Song.mp3 (Windows)
        file:///Users/jason/Music/iTunes/iTunes%20Media/Music/Artist/Album/Song.mp3 (Mac)

    Returns the decoded, normalized path (without the file:// prefix).
    """
    if not itunes_location:
        return None

    # Parse the file:// URL
    parsed = urlparse(itunes_location)

    # Get the path portion and URL-decode it
    path = unquote(parsed.path)

    # Normalize Unicode (macOS NFD -> NFC)
    path = normalize_unicode(path)

    # On Windows, paths come through as /C:/Users/... so strip leading slash if followed by drive letter
    if len(path) > 2 and path[0] == '/' and path[2] == ':':
        path = path[1:]

    # Normalize path separators to forward slashes
    path = path.replace('\\', '/')

    return path


def build_navidrome_path_index(conn: sqlite3.Connection) -> dict:
    """
    Build an index of Navidrome media files for efficient suffix matching.

    Returns a dictionary where:
    - Keys are normalized path suffixes (e.g., "Artist/Album/Track.mp3")
    - Values are lists of (media_file_id, full_path, album_id, artist_id) tuples

    We index by progressively longer suffixes to enable efficient matching.
    """
    cursor = conn.cursor()
    cursor.execute("SELECT id, path, album_id, artist_id FROM media_file")

    # Build index by filename and by path components
    index = {
        'by_filename': defaultdict(list),      # filename -> [(id, path, album_id, artist_id), ...]
        'by_suffix': defaultdict(list),         # path_suffix -> [(id, path, album_id, artist_id), ...]
        'all_files': []                         # all files for fallback
    }

    row_count = 0
    for row in cursor.fetchall():
        file_id, path, album_id, artist_id = row
        row_count += 1

        # Normalize the path
        normalized_path = normalize_unicode(path)
        if normalized_path is None:
            continue

        file_info = (file_id, normalized_path, album_id, artist_id)
        index['all_files'].append(file_info)

        # Split path into components
        parts = normalized_path.replace('\\', '/').split('/')

        # Index by filename (last component)
        filename = parts[-1].lower()
        index['by_filename'][filename].append(file_info)

        # Index by progressively longer suffixes (from right to left)
        # e.g., "Track.mp3", "Album/Track.mp3", "Artist/Album/Track.mp3"
        for i in range(len(parts)):
            suffix = '/'.join(parts[i:]).lower()
            index['by_suffix'][suffix].append(file_info)

    logger.info(f"Indexed {row_count} media files from Navidrome")
    return index


def find_matching_media_file(itunes_path: str, index: dict, ambiguous_matches: list = None) -> dict:
    """
    Find a matching media file in Navidrome using suffix matching.

    The algorithm:
    1. Extract path components from the iTunes path
    2. Try to match progressively longer suffixes against the Navidrome index
    3. Return the match if exactly one file matches a suffix
    4. If multiple matches, try a longer suffix
    5. Fall back to filename-only matching if needed

    If ambiguous_matches list is provided and multiple indistinguishable matches
    are found, the iTunes path and all matching Navidrome paths are appended.
    """
    if not itunes_path:
        return None

    # Normalize and split the iTunes path
    normalized_path = normalize_unicode(itunes_path)
    parts = normalized_path.replace('\\', '/').split('/')

    # Try matching from the filename up to longer suffixes
    # Start with just filename, then add parent directories
    for i in range(len(parts) - 1, -1, -1):
        suffix = '/'.join(parts[i:]).lower()

        if suffix in index['by_suffix']:
            matches = index['by_suffix'][suffix]

            if len(matches) == 1:
                # Unique match found
                match = matches[0]
                return {
                    'id': match[0],
                    'path': match[1],
                    'album_id': match[2],
                    'artist_id': match[3]
                }
            elif len(matches) > 1 and i == 0:
                # We've tried the full path and still have multiple matches
                logger.debug(f"Multiple matches for full path suffix: {suffix}")

                # Log ambiguous matches if list provided
                if ambiguous_matches is not None:
                    ambiguous_entry = [itunes_path] + [m[1] for m in matches]
                    ambiguous_matches.append(ambiguous_entry)

                # Return the first match as a fallback
                match = matches[0]
                return {
                    'id': match[0],
                    'path': match[1],
                    'album_id': match[2],
                    'artist_id': match[3]
                }
            # If multiple matches, continue to try a longer suffix

    # No match found
    return None


def convert_itunes_rating(itunes_rating: int) -> int:
    """
    Convert iTunes rating (0-100 in steps of 20) to Navidrome rating (0-5).
    iTunes: 0=unrated, 20=1star, 40=2star, 60=3star, 80=4star, 100=5star
    Navidrome: 0=unrated, 1-5=stars
    """
    if itunes_rating is None or itunes_rating == 0:
        return 0
    return min(5, max(1, itunes_rating // 20))


def convert_itunes_date(itunes_date) -> str:
    """
    Convert iTunes datetime to Navidrome format.
    iTunes uses Python datetime objects in the plist.
    Navidrome uses ISO format: 2025-06-25T13:45:30Z
    """
    if itunes_date is None:
        return None

    if isinstance(itunes_date, datetime):
        return itunes_date.strftime('%Y-%m-%dT%H:%M:%SZ')

    return None


def get_annotation_columns(conn: sqlite3.Connection) -> list:
    """Get the column names from the annotation table to handle schema differences."""
    cursor = conn.cursor()
    cursor.execute("PRAGMA table_info(annotation)")
    columns = [row[1] for row in cursor.fetchall()]
    return columns


def update_or_insert_annotation(
    conn: sqlite3.Connection,
    user_id: str,
    item_id: str,
    item_type: str,
    play_count: int,
    play_date: str,
    rating: int,
    replace_mode: bool = False,
    has_rated_at: bool = False
):
    """Update or insert an annotation record."""
    cursor = conn.cursor()

    # Check if annotation exists
    cursor.execute("""
        SELECT play_count, play_date, rating
        FROM annotation
        WHERE user_id = ? AND item_id = ? AND item_type = ?
    """, (user_id, item_id, item_type))

    existing = cursor.fetchone()

    if existing:
        existing_play_count = existing[0] or 0
        existing_play_date = existing[1]
        existing_rating = existing[2] or 0

        if replace_mode:
            # Replace mode: use new values directly
            new_play_count = play_count or 0
            new_rating = rating or 0
        else:
            # Add mode (default): increment play count
            new_play_count = existing_play_count + (play_count or 0)
            # Use higher rating (or keep existing if new is 0)
            new_rating = rating if rating > 0 else existing_rating

        # Use newer play date
        new_play_date = play_date
        if existing_play_date and play_date:
            if existing_play_date > play_date:
                new_play_date = existing_play_date
        elif existing_play_date:
            new_play_date = existing_play_date

        cursor.execute("""
            UPDATE annotation
            SET play_count = ?, play_date = ?, rating = ?
            WHERE user_id = ? AND item_id = ? AND item_type = ?
        """, (new_play_count, new_play_date, new_rating, user_id, item_id, item_type))
    else:
        # Insert new annotation
        # Handle schema differences (rated_at column added in newer versions)
        if has_rated_at:
            rated_at = play_date if rating and rating > 0 else None
            cursor.execute("""
                INSERT INTO annotation (user_id, item_id, item_type, play_count, play_date, rating, starred, starred_at, rated_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?)
            """, (user_id, item_id, item_type, play_count or 0, play_date, rating or 0, rated_at))
        else:
            cursor.execute("""
                INSERT INTO annotation (user_id, item_id, item_type, play_count, play_date, rating, starred, starred_at)
                VALUES (?, ?, ?, ?, ?, ?, 0, NULL)
            """, (user_id, item_id, item_type, play_count or 0, play_date, rating or 0))


def migrate_track(
    conn: sqlite3.Connection,
    user_id: str,
    itunes_track: dict,
    path_index: dict,
    stats: dict,
    not_found_paths: list,
    ambiguous_matches: list,
    replace_mode: bool = False,
    has_rated_at: bool = False
):
    """Migrate a single track's metadata from iTunes to Navidrome."""

    # Get iTunes data
    location = itunes_track.get('Location')
    if not location:
        stats['no_location'] += 1
        return False

    play_count = itunes_track.get('Play Count', 0)
    rating = convert_itunes_rating(itunes_track.get('Rating', 0))
    play_date = convert_itunes_date(itunes_track.get('Play Date UTC'))

    # Skip if no meaningful data to migrate
    if play_count == 0 and rating == 0:
        stats['no_data'] += 1
        return False

    # Extract path from iTunes location
    itunes_path = extract_path_from_itunes_location(location)
    if not itunes_path:
        stats['path_error'] += 1
        return False

    # Find matching file in Navidrome using suffix matching
    media_file = find_matching_media_file(itunes_path, path_index, ambiguous_matches)
    if not media_file:
        stats['not_found'] += 1
        logger.debug(f"NOT FOUND: {itunes_path}")
        not_found_paths.append(itunes_path)
        return False

    # Update annotations for media_file
    update_or_insert_annotation(
        conn, user_id, media_file['id'], 'media_file',
        play_count, play_date, rating, replace_mode, has_rated_at
    )

    # Update annotations for album
    # NOTE: Album play counts are AGGREGATED from all tracks.
    # This means album play_count = sum of all track play_counts.
    if media_file['album_id']:
        update_or_insert_annotation(
            conn, user_id, media_file['album_id'], 'album',
            play_count, play_date, rating, replace_mode, has_rated_at
        )

    # Update annotations for artist
    # NOTE: Same aggregation applies to artists.
    if media_file['artist_id']:
        update_or_insert_annotation(
            conn, user_id, media_file['artist_id'], 'artist',
            play_count, play_date, rating, replace_mode, has_rated_at
        )

    stats['matched'] += 1
    logger.debug(f"MATCHED: {itunes_path} -> {media_file['path']} (plays: {play_count}, rating: {rating})")

    return True


def check_track_for_dry_run(
    itunes_track: dict,
    path_index: dict,
    stats: dict,
    not_found_paths: list,
    ambiguous_matches: list
):
    """Check if a track would match during dry run (mirrors migrate_track logic)."""

    location = itunes_track.get('Location')
    if not location:
        stats['no_location'] += 1
        return

    play_count = itunes_track.get('Play Count', 0)
    rating = convert_itunes_rating(itunes_track.get('Rating', 0))

    if play_count == 0 and rating == 0:
        stats['no_data'] += 1
        return

    itunes_path = extract_path_from_itunes_location(location)
    if not itunes_path:
        stats['path_error'] += 1
        return

    media_file = find_matching_media_file(itunes_path, path_index, ambiguous_matches)
    if media_file:
        stats['matched'] += 1
        logger.debug(f"WOULD MATCH: {itunes_path} -> {media_file['path']}")
    else:
        stats['not_found'] += 1
        not_found_paths.append(itunes_path)


def print_path_samples(tracks: dict, path_index: dict, count: int = 5):
    """Print sample path matches for verification."""
    print("\n" + "="*80)
    print("SAMPLE PATH MATCHES (verify these look correct before proceeding)")
    print("="*80)

    samples = 0
    for track_id, track in tracks.items():
        if samples >= count:
            break

        location = track.get('Location')
        play_count = track.get('Play Count', 0)
        rating = track.get('Rating', 0)

        # Only show tracks with play data
        if location and (play_count > 0 or rating > 0):
            itunes_path = extract_path_from_itunes_location(location)
            media_file = find_matching_media_file(itunes_path, path_index)

            print(f"\niTunes:    {itunes_path}")
            if media_file:
                print(f"Navidrome: {media_file['path']}")
                print(f"Status:    MATCHED (plays: {play_count}, rating: {rating})")
            else:
                print(f"Navidrome: NOT FOUND")
                print(f"Status:    WILL BE SKIPPED")
            samples += 1

    print("\n" + "="*80)


def print_summary(stats: dict, dry_run: bool, not_found_count: int, ambiguous_count: int, log_dir: str):
    """Print migration summary statistics."""
    print("\n" + "="*80)
    print("MIGRATION SUMMARY")
    print("="*80)
    print(f"Total tracks in iTunes:     {stats['total']}")
    print(f"Successfully matched:       {stats['matched']}")
    print(f"Not found in Navidrome:     {stats['not_found']}")
    print(f"No location in iTunes:      {stats['no_location']}")
    print(f"No play data to migrate:    {stats['no_data']}")
    print(f"Path conversion errors:     {stats['path_error']}")
    print(f"Ambiguous matches:          {ambiguous_count}")

    # Calculate match rate based on tracks that actually have data to migrate
    tracks_with_data = stats['total'] - stats['no_location'] - stats['no_data']
    if tracks_with_data > 0:
        match_rate = (stats['matched'] / tracks_with_data) * 100
        print(f"\nMatch rate (of tracks with play data): {match_rate:.1f}%")

    print(f"\nLogs saved to: {log_dir}/")

    if not_found_count > 0:
        print(f"  - not_found.log ({not_found_count} unmatched tracks)")

    if ambiguous_count > 0:
        print(f"  - ambiguous_matches.log ({ambiguous_count} tracks with multiple possible matches)")

    if dry_run:
        print("\n*** This was a DRY RUN - no changes were made ***")
        print("Run without --dry-run to apply changes")


def prompt_for_value(prompt: str, validator=None, default=None) -> str:
    """Prompt the user for a value with optional validation."""
    while True:
        if default:
            user_input = input(f"{prompt} [{default}]: ").strip()
            if not user_input:
                user_input = default
        else:
            user_input = input(f"{prompt}: ").strip()

        if not user_input:
            print("  This field is required. Please enter a value.")
            continue

        if validator:
            error = validator(user_input)
            if error:
                print(f"  {error}")
                continue

        return user_input


def validate_file_exists(path: str) -> str:
    """Validator that checks if a file exists."""
    if not os.path.exists(path):
        return f"File not found: {path}"
    return None


def list_navidrome_users(db_path: str) -> list:
    """List all users in the Navidrome database."""
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id, user_name FROM user")
        users = cursor.fetchall()
        conn.close()
        return users
    except Exception as e:
        logger.error(f"Error reading users: {e}")
        return []


def interactive_get_arguments(args):
    """Interactively prompt for any missing arguments."""

    # Navidrome database
    if not args.navidrome_db:
        print("\n--- Navidrome Database ---")
        args.navidrome_db = prompt_for_value(
            "Path to navidrome.db",
            validator=validate_file_exists
        )

    # iTunes XML
    if not args.itunes_xml:
        print("\n--- iTunes Library ---")
        args.itunes_xml = prompt_for_value(
            "Path to iTunes Library.xml",
            validator=validate_file_exists
        )

    # User ID
    if not args.user_id:
        print("\n--- Navidrome User ---")
        users = list_navidrome_users(args.navidrome_db)

        if users:
            print("Available users:")
            for i, (user_id, username) in enumerate(users, 1):
                print(f"  {i}. {username} (ID: {user_id})")

            while True:
                selection = input("\nSelect user number or enter user ID directly: ").strip()

                # Check if it's a number selecting from the list
                try:
                    idx = int(selection)
                    if 1 <= idx <= len(users):
                        args.user_id = users[idx - 1][0]
                        print(f"  Selected user: {users[idx - 1][1]}")
                        break
                except ValueError:
                    pass

                # Otherwise treat it as a direct user ID
                if selection:
                    # Verify the user ID exists
                    matching = [u for u in users if u[0] == selection]
                    if matching:
                        args.user_id = selection
                        print(f"  Selected user: {matching[0][1]}")
                        break
                    else:
                        print("  User ID not found. Please try again.")
                else:
                    print("  Please enter a selection.")
        else:
            args.user_id = prompt_for_value("Navidrome user ID")

    return args


def main():
    parser = argparse.ArgumentParser(
        description='Migrate iTunes play counts, ratings, and dates to Navidrome',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode (prompts for all required values)
  python itunes_to_navidrome.py

  # Basic usage (ADDS to existing play counts)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123

  # Replace mode (OVERWRITES existing play counts)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 --replace

  # Dry run (no changes)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 --dry-run

Notes:
  - ALWAYS backup your navidrome.db before running!
  - Path matching uses intelligent suffix matching - no path configuration needed
  - Use --dry-run first to verify path matching
  - Check not_found.log for tracks that couldn't be matched
  - Without --replace, running twice will DOUBLE play counts!
  - Album/artist play counts are aggregated from their tracks
        """
    )

    parser.add_argument('navidrome_db', nargs='?', help='Path to navidrome.db file')
    parser.add_argument('itunes_xml', nargs='?', help='Path to iTunes Library.xml file')
    parser.add_argument('user_id', nargs='?', help='Navidrome user ID (will show list if omitted)')

    parser.add_argument('--dry-run', action='store_true',
                        help='Show what would be done without making changes')
    parser.add_argument('--replace', action='store_true',
                        help='Replace existing play counts instead of adding to them')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Enable verbose logging')
    parser.add_argument('--yes', '-y', action='store_true',
                        help='Skip confirmation prompt (for non-interactive use)')
    parser.add_argument('--sample', type=int, default=5,
                        help='Number of sample paths to show (default: 5)')

    args = parser.parse_args()

    # Create timestamped log directory for this run
    log_dir = create_log_directory()

    # Set up logging to the new directory
    setup_logging(log_dir, verbose=args.verbose)

    logger.info(f"Log directory: {log_dir}")

    # Interactive prompts for missing arguments
    args = interactive_get_arguments(args)

    # Verify files exist (for non-interactive cases)
    if not os.path.exists(args.navidrome_db):
        logger.error(f"Navidrome database not found: {args.navidrome_db}")
        sys.exit(1)

    if not os.path.exists(args.itunes_xml):
        logger.error(f"iTunes library not found: {args.itunes_xml}")
        sys.exit(1)

    # Parse iTunes library
    tracks = parse_itunes_library(args.itunes_xml)

    # Connect to Navidrome database
    conn = sqlite3.connect(args.navidrome_db, isolation_level='DEFERRED')

    try:
        # Check annotation table schema for rated_at column
        annotation_columns = get_annotation_columns(conn)
        has_rated_at = 'rated_at' in annotation_columns
        logger.debug(f"Annotation columns: {annotation_columns}")
        logger.debug(f"Has rated_at column: {has_rated_at}")

        # Verify user exists
        cursor = conn.cursor()
        cursor.execute("SELECT user_name FROM user WHERE id = ?", (args.user_id,))
        user_result = cursor.fetchone()
        if not user_result:
            logger.error(f"User ID not found: {args.user_id}")
            logger.info("Available users:")
            cursor.execute("SELECT id, user_name FROM user")
            for row in cursor.fetchall():
                logger.info(f"  ID: {row[0]}, Username: {row[1]}")
            sys.exit(1)

        logger.info(f"Migrating to user: {user_result[0]} (ID: {args.user_id})")

        # Build path index for efficient matching
        logger.info("Building path index from Navidrome database...")
        path_index = build_navidrome_path_index(conn)

        # Show sample path matches
        print_path_samples(tracks, path_index, args.sample)

        if args.dry_run:
            print("\n*** DRY RUN MODE - No changes will be made ***\n")

        if args.replace:
            print("*** REPLACE MODE - Existing play counts will be OVERWRITTEN ***\n")
        else:
            print("*** ADD MODE (default) - Play counts will be ADDED to existing ***")
            print("*** Use --replace to overwrite instead ***\n")

        # Confirm before proceeding (skip if --yes flag)
        if not args.yes:
            response = input("\nDo the path matches look correct? Proceed with migration? (yes/no): ")
            if response.lower() not in ['yes', 'y']:
                print("Aborted.")
                sys.exit(0)
        else:
            print("\n--yes flag set, skipping confirmation...")

        # Initialize statistics
        stats = {
            'total': 0,
            'matched': 0,
            'not_found': 0,
            'no_location': 0,
            'no_data': 0,
            'path_error': 0
        }

        # Collect not-found paths in memory, write once at end
        not_found_paths = []

        # Collect ambiguous matches (tracks with multiple indistinguishable Navidrome matches)
        ambiguous_matches = []

        # Process tracks
        logger.info("Starting migration...")

        for track_id, track in tracks.items():
            stats['total'] += 1

            if not args.dry_run:
                migrate_track(
                    conn, args.user_id, track,
                    path_index, stats, not_found_paths, ambiguous_matches,
                    replace_mode=args.replace,
                    has_rated_at=has_rated_at
                )
            else:
                # Dry run - use the same logic as migrate_track for accurate stats
                check_track_for_dry_run(
                    track, path_index, stats, not_found_paths, ambiguous_matches
                )

            # Progress indicator
            if stats['total'] % 500 == 0:
                logger.info(f"Processed {stats['total']} tracks...")

        # Commit changes
        if not args.dry_run:
            conn.commit()
            logger.info("Changes committed to database")

    except Exception as e:
        logger.error(f"Error during migration: {e}")
        if not args.dry_run:
            logger.info("Rolling back changes...")
            conn.rollback()
        raise

    finally:
        conn.close()

    # Write not_found.log once at the end (much more efficient than per-track)
    not_found_log = os.path.join(log_dir, 'not_found.log')
    if not_found_paths:
        with open(not_found_log, 'w') as f:
            f.write('\n'.join(not_found_paths) + '\n')

    # Write ambiguous_matches.log for tracks with multiple indistinguishable matches
    # Format: iTunes path, then each Navidrome match, with blank line between entries
    ambiguous_log = os.path.join(log_dir, 'ambiguous_matches.log')
    if ambiguous_matches:
        with open(ambiguous_log, 'w') as f:
            entries = []
            for match_group in ambiguous_matches:
                # match_group is [itunes_path, navidrome_path1, navidrome_path2, ...]
                entries.append('\n'.join(match_group))
            f.write('\n\n'.join(entries) + '\n')

    # Print summary
    print_summary(stats, args.dry_run, len(not_found_paths), len(ambiguous_matches), log_dir)


if __name__ == '__main__':
    main()
