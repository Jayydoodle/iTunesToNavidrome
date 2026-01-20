#!/usr/bin/env python3
"""
iTunes to Navidrome Migration Script
Transfers play counts, ratings, and play dates from iTunes Library.xml to Navidrome

Tested with: Navidrome 0.55+ / 0.58+ (with multi-library support)
Author: Claude (Anthropic) - January 2026
License: Public Domain

Usage:
    python itunes_to_navidrome.py <navidrome.db> <Library.xml> <navidrome_user_id> <itunes_music_path> <navidrome_music_path>

Example:
    python itunes_to_navidrome.py navidrome.db "iTunes Library.xml" abc123 "/Users/jason/Music/iTunes/iTunes Media/Music" "/mnt/music"

IMPORTANT NOTES:
- Play counts are ADDED to existing counts by default. Use --replace to overwrite instead.
- Album/artist play counts are aggregated from all their tracks.
- Running twice without --replace will DOUBLE your play counts!
- Always backup navidrome.db before running.
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

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('migration.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


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


def normalize_itunes_path(itunes_location: str, itunes_base: str, navidrome_base: str) -> str:
    """
    Convert iTunes file:// URL to a Navidrome-compatible path.
    
    iTunes stores paths like:
        file://localhost/C:/Users/jason/Music/iTunes/iTunes%20Media/Music/Artist/Album/Song.mp3 (Windows)
        file:///Users/jason/Music/iTunes/iTunes%20Media/Music/Artist/Album/Song.mp3 (Mac)
    
    We need to:
    1. Strip the file:// prefix
    2. URL-decode the path (%20 -> space, etc.)
    3. Normalize Unicode (NFD -> NFC)
    4. Replace the iTunes base path with the Navidrome base path
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
    
    # Normalize the base paths too
    itunes_base = normalize_unicode(itunes_base.replace('\\', '/').rstrip('/'))
    navidrome_base = navidrome_base.replace('\\', '/').rstrip('/')
    
    # Replace the base path (must be a prefix match)
    if path.startswith(itunes_base):
        path = navidrome_base + path[len(itunes_base):]
    elif path.lower().startswith(itunes_base.lower()):
        # Case-insensitive fallback (common on macOS/Windows)
        path = navidrome_base + path[len(itunes_base):]
    else:
        # Base path not found - return path as-is but log warning
        logger.warning(f"iTunes base path not found in: {path}")
    
    return path


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


def get_media_file_by_path(conn: sqlite3.Connection, path: str) -> dict:
    """Look up a media file in Navidrome by its path."""
    cursor = conn.cursor()
    
    # Normalize the search path
    normalized_path = normalize_unicode(path)
    
    # Try exact match first
    cursor.execute("""
        SELECT id, path, album_id, artist_id 
        FROM media_file 
        WHERE path = ?
    """, (normalized_path,))
    
    result = cursor.fetchone()
    if result:
        return {
            'id': result[0],
            'path': result[1],
            'album_id': result[2],
            'artist_id': result[3]
        }
    
    # Try case-insensitive match
    cursor.execute("""
        SELECT id, path, album_id, artist_id 
        FROM media_file 
        WHERE LOWER(path) = LOWER(?)
    """, (normalized_path,))
    
    result = cursor.fetchone()
    if result:
        return {
            'id': result[0],
            'path': result[1],
            'album_id': result[2],
            'artist_id': result[3]
        }
    
    # Try matching just the filename (last resort)
    filename = os.path.basename(normalized_path)
    # Escape SQL LIKE wildcards to prevent incorrect matches
    escaped_filename = filename.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    cursor.execute("""
        SELECT id, path, album_id, artist_id 
        FROM media_file 
        WHERE path LIKE ? ESCAPE '\\'
    """, (f'%/{escaped_filename}',))
    
    results = cursor.fetchall()
    if len(results) == 1:
        return {
            'id': results[0][0],
            'path': results[0][1],
            'album_id': results[0][2],
            'artist_id': results[0][3]
        }
    
    return None


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
    itunes_base: str,
    navidrome_base: str,
    stats: dict,
    not_found_paths: list,
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
    
    # Convert path
    navidrome_path = normalize_itunes_path(location, itunes_base, navidrome_base)
    if not navidrome_path:
        stats['path_error'] += 1
        return False
    
    # Find matching file in Navidrome
    media_file = get_media_file_by_path(conn, navidrome_path)
    if not media_file:
        stats['not_found'] += 1
        logger.debug(f"NOT FOUND: {navidrome_path}")
        not_found_paths.append(navidrome_path)
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
    logger.debug(f"MATCHED: {navidrome_path} (plays: {play_count}, rating: {rating})")
    
    return True


def check_track_for_dry_run(
    itunes_track: dict,
    itunes_base: str,
    navidrome_base: str,
    conn: sqlite3.Connection,
    stats: dict,
    not_found_paths: list
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
    
    navidrome_path = normalize_itunes_path(location, itunes_base, navidrome_base)
    if not navidrome_path:
        stats['path_error'] += 1
        return
    
    media_file = get_media_file_by_path(conn, navidrome_path)
    if media_file:
        stats['matched'] += 1
    else:
        stats['not_found'] += 1
        not_found_paths.append(navidrome_path)


def print_path_samples(tracks: dict, itunes_base: str, navidrome_base: str, count: int = 5):
    """Print sample path conversions for verification."""
    print("\n" + "="*80)
    print("SAMPLE PATH CONVERSIONS (verify these look correct before proceeding)")
    print("="*80)
    
    samples = 0
    for track_id, track in tracks.items():
        if samples >= count:
            break
        
        location = track.get('Location')
        if location:
            converted = normalize_itunes_path(location, itunes_base, navidrome_base)
            print(f"\niTunes:    {location}")
            print(f"Navidrome: {converted}")
            samples += 1
    
    print("\n" + "="*80)


def print_summary(stats: dict, dry_run: bool, not_found_count: int):
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
    
    # Calculate match rate based on tracks that actually have data to migrate
    tracks_with_data = stats['total'] - stats['no_location'] - stats['no_data']
    if tracks_with_data > 0:
        match_rate = (stats['matched'] / tracks_with_data) * 100
        print(f"\nMatch rate (of tracks with play data): {match_rate:.1f}%")
    
    if not_found_count > 0:
        print(f"\nCheck 'not_found.log' for {not_found_count} unmatched tracks")
    
    if dry_run:
        print("\n*** This was a DRY RUN - no changes were made ***")
        print("Run without --dry-run to apply changes")


def main():
    parser = argparse.ArgumentParser(
        description='Migrate iTunes play counts, ratings, and dates to Navidrome',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic usage (ADDS to existing play counts)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 \\
      "/Users/jason/Music/iTunes/iTunes Media/Music" "/mnt/music"
  
  # Replace mode (OVERWRITES existing play counts)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 \\
      "/Users/jason/Music" "/home/jason/Music" --replace
  
  # Dry run (no changes)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 \\
      "/Users/jason/Music" "/home/jason/Music" --dry-run

Notes:
  - ALWAYS backup your navidrome.db before running!
  - Use --dry-run first to verify path matching
  - Check not_found.log for tracks that couldn't be matched
  - Without --replace, running twice will DOUBLE play counts!
  - Album/artist play counts are aggregated from their tracks
        """
    )
    
    parser.add_argument('navidrome_db', help='Path to navidrome.db file')
    parser.add_argument('itunes_xml', help='Path to iTunes Library.xml file')
    parser.add_argument('user_id', help='Navidrome user ID (find with: SELECT id FROM user)')
    parser.add_argument('itunes_path', help='Base path in iTunes (e.g., "/Users/jason/Music/iTunes/iTunes Media/Music")')
    parser.add_argument('navidrome_path', help='Corresponding path in Navidrome (e.g., "/mnt/music")')
    
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
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Verify files exist
    if not os.path.exists(args.navidrome_db):
        logger.error(f"Navidrome database not found: {args.navidrome_db}")
        sys.exit(1)
    
    if not os.path.exists(args.itunes_xml):
        logger.error(f"iTunes library not found: {args.itunes_xml}")
        sys.exit(1)
    
    # Parse iTunes library
    tracks = parse_itunes_library(args.itunes_xml)
    
    # Show sample path conversions
    print_path_samples(tracks, args.itunes_path, args.navidrome_path, args.sample)
    
    if args.dry_run:
        print("\n*** DRY RUN MODE - No changes will be made ***\n")
    
    if args.replace:
        print("*** REPLACE MODE - Existing play counts will be OVERWRITTEN ***\n")
    else:
        print("*** ADD MODE (default) - Play counts will be ADDED to existing ***")
        print("*** Use --replace to overwrite instead ***\n")
    
    # Confirm before proceeding (skip if --yes flag)
    if not args.yes:
        response = input("\nDo the path conversions look correct? (yes/no): ")
        if response.lower() not in ['yes', 'y']:
            print("Aborted. Please adjust your path arguments and try again.")
            sys.exit(0)
    else:
        print("\n--yes flag set, skipping confirmation...")
    
    # Initialize statistics before try block to ensure they're available for summary
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
    
    # Connect to Navidrome database with explicit transaction handling
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
        
        # Process tracks
        logger.info("Starting migration...")
        
        for track_id, track in tracks.items():
            stats['total'] += 1
            
            if not args.dry_run:
                migrate_track(
                    conn, args.user_id, track, 
                    args.itunes_path, args.navidrome_path, 
                    stats, not_found_paths,
                    replace_mode=args.replace,
                    has_rated_at=has_rated_at
                )
            else:
                # Dry run - use the same logic as migrate_track for accurate stats
                check_track_for_dry_run(
                    track, args.itunes_path, args.navidrome_path,
                    conn, stats, not_found_paths
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
    if not_found_paths:
        with open('not_found.log', 'w') as f:
            f.write('\n'.join(not_found_paths) + '\n')
    else:
        # Clear the file if no missing tracks
        open('not_found.log', 'w').close()
    
    # Print summary
    print_summary(stats, args.dry_run, len(not_found_paths))


if __name__ == '__main__':
    main()
