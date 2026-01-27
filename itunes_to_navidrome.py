#!/usr/bin/env python3
"""
iTunes to Navidrome Migration Script
Transfers play counts, ratings, play dates, playlists, and date-added timestamps
from iTunes Library.xml to Navidrome.

Tested with: Navidrome 0.55+ / 0.58+ (with multi-library support)
Author: Claude (Anthropic) - January 2026
License: Public Domain

Usage:
    python itunes_to_navidrome.py [navidrome.db] [Library.xml] [navidrome_user_id]

    If arguments are omitted, the script will:
    1. Auto-scan the current directory for navidrome.db and iTunes XML files
    2. Prompt you to confirm or enter different paths
    3. Show an interactive options screen to select what to import

Example:
    python itunes_to_navidrome.py navidrome.db "iTunes Library.xml" abc123

    # Import playlists (non-interactive)
    python itunes_to_navidrome.py navidrome.db "Library.xml" abc123 --import-playlists

    # Import date-added timestamps
    python itunes_to_navidrome.py navidrome.db "Library.xml" abc123 --import-date-added

IMPORT OPTIONS (all selectable individually):
1. Play counts - Number of times each track was played
2. Ratings - Star ratings (1-5 stars)
3. Play dates - Last played timestamps
4. Date added - When tracks were added to library
5. Playlists - Creates playlists in Navidrome (smart playlists skipped)

IMPORTANT NOTES:
- Play counts are ADDED to existing counts by default. Use --replace to overwrite instead.
- Album/artist play counts are aggregated from all their tracks.
- Running twice without --replace will DOUBLE your play counts!
- Always backup navidrome.db before running.
- Path matching uses suffix matching - iTunes paths are matched to Navidrome paths
  by finding the longest matching path suffix.
- Smart playlists are not imported (they cannot be converted to static playlists).
- Existing playlists with the same name are skipped.
"""

import sqlite3
import plistlib
import sys
import os
import unicodedata
import html
import string
import random
from dataclasses import dataclass
from datetime import datetime
from urllib.parse import unquote, urlparse
import argparse
import logging
from collections import defaultdict


@dataclass
class ImportOptions:
    """Options for what data to import from iTunes to Navidrome."""
    import_play_counts: bool = True
    import_ratings: bool = True
    import_play_dates: bool = True
    import_date_added: bool = False
    import_playlists: bool = False

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

    # File: detailed with timestamps
    file_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    # Console: clean, just the message
    console_formatter = logging.Formatter('%(message)s')

    file_handler.setFormatter(file_formatter)
    stream_handler.setFormatter(console_formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    logger.setLevel(log_level)


def log_and_print(message: str):
    """Output message to both console and log file."""
    print(message)
    # Strip formatting chars for cleaner log output
    clean_message = message.lstrip('= -').rstrip('= -').strip()
    if clean_message:
        logger.info(clean_message)


def parse_itunes_library(xml_path: str, include_playlists: bool = False):
    """
    Parse iTunes Library.xml and return track dictionary.

    If include_playlists is True, returns (tracks, playlists) tuple.
    Otherwise returns just tracks dict.
    """
    logger.info(f"Parsing iTunes library: {xml_path}")

    with open(xml_path, 'rb') as f:
        library = plistlib.load(f)

    tracks = library.get('Tracks', {})
    logger.info(f"Found {len(tracks)} tracks in iTunes library")

    if include_playlists:
        playlists = extract_playlists(library, tracks)
        return tracks, playlists

    return tracks


def is_smart_playlist(playlist: dict) -> bool:
    """Check if a playlist is a smart playlist (auto-generated based on rules)."""
    return 'Smart Info' in playlist or 'Smart Criteria' in playlist


def is_system_playlist(playlist: dict) -> bool:
    """
    Check if a playlist is a system/built-in playlist.

    System playlists include:
    - Master library (Master=True)
    - Distinguished Kind playlists (Music, Movies, TV Shows, Podcasts, etc.)
    """
    if playlist.get('Master'):
        return True
    if 'Distinguished Kind' in playlist:
        return True
    # Also skip "Library" named playlists that are folder-like
    if playlist.get('Folder'):
        return True
    return False


def extract_playlists(library: dict, tracks: dict) -> list:
    """
    Extract user playlists from iTunes library, skipping system and smart playlists.

    Returns list of dicts with playlist info and resolved track data.
    """
    raw_playlists = library.get('Playlists', [])
    user_playlists = []

    skipped_smart = 0
    skipped_system = 0

    for playlist in raw_playlists:
        name = playlist.get('Name', 'Unnamed')

        # Skip system playlists
        if is_system_playlist(playlist):
            skipped_system += 1
            logger.debug(f"Skipping system playlist: {name}")
            continue

        # Skip smart playlists
        if is_smart_playlist(playlist):
            skipped_smart += 1
            logger.debug(f"Skipping smart playlist: {name}")
            continue

        # Get playlist items (track IDs)
        items = playlist.get('Playlist Items', [])
        if not items:
            logger.debug(f"Skipping empty playlist: {name}")
            continue

        # Resolve track IDs to track data
        playlist_tracks = []
        for item in items:
            track_id = str(item.get('Track ID'))
            if track_id in tracks:
                playlist_tracks.append(tracks[track_id])

        if playlist_tracks:
            user_playlists.append({
                'name': name,
                'tracks': playlist_tracks,
                'playlist_id': playlist.get('Playlist ID'),
                'persistent_id': playlist.get('Playlist Persistent ID')
            })

    logger.info(f"Found {len(user_playlists)} user playlists "
                f"(skipped {skipped_smart} smart, {skipped_system} system)")

    return user_playlists


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

    # Decode XML/HTML entities (e.g., &#38; -> &, &#39; -> ')
    # iTunes Library.xml uses these for special characters
    path = html.unescape(path)

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
    import_options: ImportOptions,
    replace_mode: bool = False,
    has_rated_at: bool = False
):
    """Migrate a single track's metadata from iTunes to Navidrome."""

    # Get iTunes data
    location = itunes_track.get('Location')
    if not location:
        stats['no_location'] += 1
        return False

    # Get data based on import options
    play_count = itunes_track.get('Play Count', 0) if import_options.import_play_counts else 0
    rating = convert_itunes_rating(itunes_track.get('Rating', 0)) if import_options.import_ratings else 0
    play_date = convert_itunes_date(itunes_track.get('Play Date UTC')) if import_options.import_play_dates else None

    # Skip if no meaningful data to migrate (based on selected options)
    if play_count == 0 and rating == 0 and play_date is None:
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
    ambiguous_matches: list,
    import_options: ImportOptions
):
    """Check if a track would match during dry run (mirrors migrate_track logic)."""

    location = itunes_track.get('Location')
    if not location:
        stats['no_location'] += 1
        return

    # Get data based on import options
    play_count = itunes_track.get('Play Count', 0) if import_options.import_play_counts else 0
    rating = convert_itunes_rating(itunes_track.get('Rating', 0)) if import_options.import_ratings else 0
    play_date = itunes_track.get('Play Date UTC') if import_options.import_play_dates else None

    if play_count == 0 and rating == 0 and play_date is None:
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


# =============================================================================
# Date Added Import Functions
# =============================================================================

def update_media_file_date_added(conn: sqlite3.Connection, media_file_id: str, date_added: str):
    """
    Update media_file.created_at with iTunes 'Date Added' timestamp.

    Args:
        conn: Database connection
        media_file_id: Navidrome media file ID
        date_added: ISO format datetime string
    """
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE media_file SET created_at = ? WHERE id = ?
    """, (date_added, media_file_id))


def migrate_date_added(
    conn: sqlite3.Connection,
    tracks: dict,
    path_index: dict,
    dry_run: bool = False
) -> dict:
    """
    Import 'Date Added' timestamps from iTunes to Navidrome.

    Returns stats dict with counts.
    """
    stats = {
        'total': 0,
        'updated': 0,
        'no_date': 0,
        'not_found': 0
    }

    for track in tracks.values():
        stats['total'] += 1

        # Get date added from iTunes
        date_added = track.get('Date Added')
        if not date_added:
            stats['no_date'] += 1
            continue

        # Convert to ISO format
        date_added_str = convert_itunes_date(date_added)
        if not date_added_str:
            stats['no_date'] += 1
            continue

        # Get file location
        location = track.get('Location')
        if not location:
            continue

        itunes_path = extract_path_from_itunes_location(location)
        if not itunes_path:
            continue

        # Find matching file in Navidrome
        media_file = find_matching_media_file(itunes_path, path_index)
        if not media_file:
            stats['not_found'] += 1
            continue

        # Update the date
        if not dry_run:
            update_media_file_date_added(conn, media_file['id'], date_added_str)

        stats['updated'] += 1

        if stats['total'] % 500 == 0:
            logger.info(f"Processed {stats['total']} tracks...")

    return stats


# =============================================================================
# Playlist Import Functions
# =============================================================================

# Base62 characters for Navidrome ID generation (same as Navidrome uses)
BASE62_CHARS = string.ascii_letters + string.digits


def generate_playlist_id() -> str:
    """
    Generate a 22-character base62 ID like Navidrome uses.

    Navidrome uses nanoid-style IDs with base62 encoding.
    """
    return ''.join(random.choices(BASE62_CHARS, k=22))


def playlist_exists(conn: sqlite3.Connection, name: str, owner_id: str) -> bool:
    """Check if a playlist with the given name already exists for the user."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id FROM playlist WHERE name = ? AND owner_id = ?
    """, (name, owner_id))
    return cursor.fetchone() is not None


def create_playlist(
    conn: sqlite3.Connection,
    name: str,
    owner_id: str,
    media_file_ids: list,
    comment: str = ""
) -> str:
    """
    Create a playlist and its track entries in Navidrome.

    Args:
        conn: Database connection
        name: Playlist name
        owner_id: Navidrome user ID
        media_file_ids: List of media file IDs in order
        comment: Optional playlist comment/description

    Returns:
        The created playlist ID
    """
    cursor = conn.cursor()
    playlist_id = generate_playlist_id()
    now = datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ')

    # Calculate duration by summing track durations
    if media_file_ids:
        placeholders = ','.join('?' * len(media_file_ids))
        cursor.execute(f"""
            SELECT COALESCE(SUM(duration), 0) FROM media_file WHERE id IN ({placeholders})
        """, media_file_ids)
        total_duration = cursor.fetchone()[0] or 0
    else:
        total_duration = 0

    # Insert playlist record
    cursor.execute("""
        INSERT INTO playlist (id, name, comment, owner_id, public, song_count, duration, created_at, updated_at)
        VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?)
    """, (playlist_id, name, comment, owner_id, len(media_file_ids), total_duration, now, now))

    # Insert playlist tracks with positions (id is the track order, 0-indexed)
    for position, media_file_id in enumerate(media_file_ids):
        cursor.execute("""
            INSERT INTO playlist_tracks (id, playlist_id, media_file_id)
            VALUES (?, ?, ?)
        """, (position, playlist_id, media_file_id))

    return playlist_id


def migrate_playlist(
    conn: sqlite3.Connection,
    user_id: str,
    playlist: dict,
    path_index: dict,
    stats: dict,
    dry_run: bool = False
) -> bool:
    """
    Import a single playlist from iTunes to Navidrome.

    Args:
        conn: Database connection
        user_id: Navidrome user ID
        playlist: Playlist dict with 'name' and 'tracks' keys
        path_index: Navidrome path index for matching
        stats: Stats dict to update
        dry_run: If True, don't make changes

    Returns:
        True if playlist was created, False otherwise
    """
    name = playlist['name']
    tracks = playlist['tracks']

    # Check if playlist already exists
    if not dry_run and playlist_exists(conn, name, user_id):
        logger.warning(f"Playlist already exists, skipping: {name}")
        stats['skipped_exists'] += 1
        return False

    # Match tracks to Navidrome media files
    media_file_ids = []
    unmatched = []

    for track in tracks:
        location = track.get('Location')
        if not location:
            continue

        itunes_path = extract_path_from_itunes_location(location)
        if not itunes_path:
            continue

        media_file = find_matching_media_file(itunes_path, path_index)
        if media_file:
            media_file_ids.append(media_file['id'])
        else:
            unmatched.append(itunes_path)

    # Skip if no tracks matched
    if not media_file_ids:
        logger.warning(f"No tracks matched for playlist: {name} ({len(tracks)} tracks)")
        stats['skipped_no_tracks'] += 1
        return False

    # Log partial matches
    if unmatched:
        logger.info(f"Playlist '{name}': {len(media_file_ids)}/{len(tracks)} tracks matched")
        for path in unmatched[:5]:  # Log first 5 unmatched
            logger.debug(f"  Unmatched: {path}")
        if len(unmatched) > 5:
            logger.debug(f"  ... and {len(unmatched) - 5} more unmatched tracks")

    # Create the playlist
    if not dry_run:
        create_playlist(conn, name, user_id, media_file_ids)
        logger.info(f"Created playlist: {name} ({len(media_file_ids)} tracks)")
    else:
        logger.info(f"Would create playlist: {name} ({len(media_file_ids)} tracks)")

    stats['created'] += 1
    stats['tracks_matched'] += len(media_file_ids)
    stats['tracks_unmatched'] += len(unmatched)

    return True


def migrate_all_playlists(
    conn: sqlite3.Connection,
    user_id: str,
    playlists: list,
    path_index: dict,
    dry_run: bool = False
) -> dict:
    """
    Import all playlists from iTunes to Navidrome.

    Args:
        conn: Database connection
        user_id: Navidrome user ID
        playlists: List of playlist dicts from extract_playlists()
        path_index: Navidrome path index for matching
        dry_run: If True, don't make changes

    Returns:
        Stats dict with import results
    """
    stats = {
        'total': len(playlists),
        'created': 0,
        'skipped_exists': 0,
        'skipped_no_tracks': 0,
        'tracks_matched': 0,
        'tracks_unmatched': 0
    }

    for playlist in playlists:
        migrate_playlist(conn, user_id, playlist, path_index, stats, dry_run)

    return stats


def print_path_samples(tracks: dict, path_index: dict, count: int = 5):
    """Print sample path matches for verification."""
    print("\n" + "="*80)
    print("SAMPLE PATH MATCHES (verify these look correct before proceeding)")
    print("="*80)

    samples = 0
    for _, track in tracks.items():
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
    sys.stdout.flush()  # Ensure output appears before subsequent log messages


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


def find_navidrome_db(directory: str = ".") -> list:
    """
    Scan directory for potential Navidrome database files.

    Returns list of paths to .db files, prioritizing 'navidrome.db'.
    """
    db_files = []
    try:
        for filename in os.listdir(directory):
            if filename.endswith('.db'):
                filepath = os.path.join(directory, filename)
                if os.path.isfile(filepath):
                    # Prioritize navidrome.db by putting it first
                    if filename.lower() == 'navidrome.db':
                        db_files.insert(0, filepath)
                    else:
                        db_files.append(filepath)
    except OSError:
        pass
    return db_files


def find_itunes_xml(directory: str = ".") -> list:
    """
    Scan directory for potential iTunes Library XML files.

    Returns list of paths to .xml files, prioritizing common iTunes names.
    """
    xml_files = []
    priority_names = ['itunes music library.xml', 'itunes library.xml', 'library.xml']

    try:
        for filename in os.listdir(directory):
            if filename.endswith('.xml'):
                filepath = os.path.join(directory, filename)
                if os.path.isfile(filepath):
                    # Prioritize common iTunes library names
                    if filename.lower() in priority_names:
                        xml_files.insert(0, filepath)
                    else:
                        xml_files.append(filepath)
    except OSError:
        pass
    return xml_files


def prompt_file_with_autoscan(
    prompt_name: str,
    scan_func,
    file_description: str
) -> str:
    """
    Prompt for a file path with auto-scan support.

    First scans current directory for matching files, lets user confirm or
    enter a different path.
    """
    found_files = scan_func(".")

    if found_files:
        print(f"\n--- {prompt_name} ---")
        print(f"Found {file_description} in current directory:")
        for i, filepath in enumerate(found_files, 1):
            print(f"  {i}. {filepath}")

        while True:
            if len(found_files) == 1:
                response = input(f"\nUse this file? (Y/n) or enter path: ").strip()
                if response.lower() in ['', 'y', 'yes']:
                    return found_files[0]
                elif response.lower() == 'n':
                    # User wants to enter their own path
                    return prompt_for_value(
                        f"Path to {file_description}",
                        validator=validate_file_exists
                    )
                elif os.path.exists(response):
                    return response
                else:
                    print(f"  File not found: {response}")
            else:
                response = input(f"\nSelect number, or enter path: ").strip()

                # Check if it's a number selecting from the list
                try:
                    idx = int(response)
                    if 1 <= idx <= len(found_files):
                        return found_files[idx - 1]
                    else:
                        print(f"  Please enter 1-{len(found_files)} or a file path.")
                        continue
                except ValueError:
                    pass

                # Check if it's a valid path
                if response and os.path.exists(response):
                    return response
                elif response:
                    print(f"  File not found: {response}")
    else:
        # No files found, prompt for path
        print(f"\n--- {prompt_name} ---")
        print(f"No {file_description} found in current directory.")
        return prompt_for_value(
            f"Path to {file_description}",
            validator=validate_file_exists
        )


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
    """Interactively prompt for any missing arguments with auto-scan support."""

    # Navidrome database - auto-scan current directory
    if not args.navidrome_db:
        args.navidrome_db = prompt_file_with_autoscan(
            "Navidrome Database",
            find_navidrome_db,
            "navidrome.db"
        )

    # iTunes XML - auto-scan current directory
    if not args.itunes_xml:
        args.itunes_xml = prompt_file_with_autoscan(
            "iTunes Library",
            find_itunes_xml,
            "iTunes Library.xml"
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


# =============================================================================
# Options Screen
# =============================================================================

def display_options_screen() -> ImportOptions:
    """
    Display interactive options screen and return selected options.

    Returns ImportOptions with user selections.
    """
    options = ImportOptions()

    def clear_screen():
        """Clear terminal screen."""
        os.system('cls' if os.name == 'nt' else 'clear')

    def draw_screen():
        """Draw the options screen."""
        clear_screen()
        print("=" * 80)
        print("iTunes to Navidrome Migration Tool")
        print("=" * 80)
        print()
        print("Select import options (toggle with number key, Enter to proceed):")
        print()

        # Option 1: Play counts
        check1 = "X" if options.import_play_counts else " "
        print(f"  [{check1}] 1. Play counts")
        print("         Transfers how many times each track was played")
        print()

        # Option 2: Ratings
        check2 = "X" if options.import_ratings else " "
        print(f"  [{check2}] 2. Ratings")
        print("         Transfers star ratings (1-5 stars)")
        print()

        # Option 3: Play dates
        check3 = "X" if options.import_play_dates else " "
        print(f"  [{check3}] 3. Play dates")
        print("         Transfers last played timestamps")
        print()

        # Option 4: Date added
        check4 = "X" if options.import_date_added else " "
        print(f"  [{check4}] 4. Date added")
        print("         Transfers when tracks were added to library")
        print()

        # Option 5: Playlists
        check5 = "X" if options.import_playlists else " "
        print(f"  [{check5}] 5. Playlists")
        print("         Creates playlists in Navidrome (smart playlists skipped)")
        print()

        print("-" * 80)
        print("  [A] Toggle all    [Enter] Continue    [Q] Quit")
        print("=" * 80)

    def toggle_all():
        """Toggle all options on or off."""
        # If any option is off, turn all on. Otherwise turn all off.
        all_on = (options.import_play_counts and options.import_ratings and
                  options.import_play_dates and options.import_playlists and
                  options.import_date_added)
        if not all_on:
            options.import_play_counts = True
            options.import_ratings = True
            options.import_play_dates = True
            options.import_playlists = True
            options.import_date_added = True
        else:
            options.import_play_counts = False
            options.import_ratings = False
            options.import_play_dates = False
            options.import_playlists = False
            options.import_date_added = False

    # Try to use getch for single-key input, fall back to input() if not available
    try:
        import termios
        import tty

        def getch():
            """Read a single character without requiring Enter."""
            fd = sys.stdin.fileno()
            old_settings = termios.tcgetattr(fd)
            try:
                tty.setraw(fd)
                ch = sys.stdin.read(1)
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            return ch

        use_getch = True
    except ImportError:
        use_getch = False

    while True:
        draw_screen()

        if use_getch:
            key = getch().lower()
        else:
            key = input("\nEnter choice: ").strip().lower()
            if not key:
                key = '\r'  # Treat empty input as Enter

        if key == '1':
            options.import_play_counts = not options.import_play_counts
        elif key == '2':
            options.import_ratings = not options.import_ratings
        elif key == '3':
            options.import_play_dates = not options.import_play_dates
        elif key == '4':
            options.import_date_added = not options.import_date_added
        elif key == '5':
            options.import_playlists = not options.import_playlists
        elif key == 'a':
            toggle_all()
        elif key in ('\r', '\n', ''):
            # Enter key - proceed
            any_selected = (options.import_play_counts or options.import_ratings or
                           options.import_play_dates or options.import_playlists or
                           options.import_date_added)
            if not any_selected:
                # No options selected - show message
                print("\nNo options selected. Please select at least one option.")
                if use_getch:
                    print("Press any key to continue...")
                    getch()
                else:
                    input("Press Enter to continue...")
                continue
            break
        elif key == 'q':
            print("\nAborted.")
            sys.exit(0)

    # Clear screen before returning
    clear_screen()

    return options


def main():
    parser = argparse.ArgumentParser(
        description='Migrate iTunes play counts, ratings, playlists, and dates to Navidrome',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Interactive mode (shows options screen)
  python itunes_to_navidrome.py

  # Import only ratings (non-interactive)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 --import-ratings

  # Import play counts and ratings (no play dates)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 \\
      --import-play-counts --import-ratings

  # Import playlists
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 --import-playlists

  # Import everything (non-interactive)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 \\
      --import-play-counts --import-ratings --import-play-dates \\
      --import-playlists --import-date-added

  # Replace mode (OVERWRITES existing play counts)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 \\
      --import-play-counts --replace

  # Dry run (no changes)
  python itunes_to_navidrome.py navidrome.db "Library.xml" user123 --dry-run

Notes:
  - ALWAYS backup your navidrome.db before running!
  - Path matching uses intelligent suffix matching - no path configuration needed
  - Use --dry-run first to verify path matching
  - Check not_found.log for tracks that couldn't be matched
  - Without --replace, running twice will DOUBLE play counts!
  - Album/artist play counts are aggregated from their tracks
  - Smart playlists are skipped (cannot be converted to static)
  - Existing playlists with same name are skipped
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

    # New import options
    parser.add_argument('--import-play-counts', action='store_true',
                        help='Import play counts')
    parser.add_argument('--import-ratings', action='store_true',
                        help='Import star ratings')
    parser.add_argument('--import-play-dates', action='store_true',
                        help='Import last played timestamps')
    parser.add_argument('--import-playlists', action='store_true',
                        help='Import playlists from iTunes (smart playlists skipped)')
    parser.add_argument('--import-date-added', action='store_true',
                        help='Import "Date Added" timestamps')
    parser.add_argument('--no-interactive', action='store_true',
                        help='Skip interactive options screen (use CLI flags only)')

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

    # Determine import options
    # If specific import flags are set, use those directly (non-interactive)
    # Otherwise, show the options screen (unless --no-interactive)
    any_import_flag = (args.import_play_counts or args.import_ratings or
                       args.import_play_dates or args.import_playlists or
                       args.import_date_added)

    if any_import_flag or args.no_interactive:
        # CLI mode - use flags directly
        if any_import_flag:
            # Use exactly what was specified
            import_options = ImportOptions(
                import_play_counts=args.import_play_counts,
                import_ratings=args.import_ratings,
                import_play_dates=args.import_play_dates,
                import_playlists=args.import_playlists,
                import_date_added=args.import_date_added
            )
        else:
            # --no-interactive with no specific flags: default to play counts/ratings/dates
            import_options = ImportOptions(
                import_play_counts=True,
                import_ratings=True,
                import_play_dates=True,
                import_playlists=False,
                import_date_added=False
            )
    else:
        # Interactive mode - show options screen
        import_options = display_options_screen()

    # Log selected options
    logger.info(f"Import options: play_counts={import_options.import_play_counts}, "
                f"ratings={import_options.import_ratings}, "
                f"play_dates={import_options.import_play_dates}, "
                f"playlists={import_options.import_playlists}, "
                f"date_added={import_options.import_date_added}")

    # Parse iTunes library (include playlists if needed)
    if import_options.import_playlists:
        tracks, playlists = parse_itunes_library(args.itunes_xml, include_playlists=True)
    else:
        tracks = parse_itunes_library(args.itunes_xml)
        playlists = []

    # Initialize stats variables (will be populated during migration)
    stats = {'total': 0, 'matched': 0, 'not_found': 0, 'no_location': 0, 'no_data': 0, 'path_error': 0}
    date_added_stats = None
    playlist_stats = None
    not_found_paths = []
    ambiguous_matches = []

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

        # =====================================================================
        # 1. Play counts, ratings, and play dates
        # =====================================================================
        if import_options.import_play_counts or import_options.import_ratings or import_options.import_play_dates:
            # Build description of what's being imported
            importing = []
            if import_options.import_play_counts:
                importing.append("Play Counts")
            if import_options.import_ratings:
                importing.append("Ratings")
            if import_options.import_play_dates:
                importing.append("Play Dates")

            print(f"\n--- Importing {', '.join(importing)} ---")
            logger.info(f"Starting migration of: {', '.join(importing)}...")

            for _, track in tracks.items():
                stats['total'] += 1

                if not args.dry_run:
                    migrate_track(
                        conn, args.user_id, track,
                        path_index, stats, not_found_paths, ambiguous_matches,
                        import_options=import_options,
                        replace_mode=args.replace,
                        has_rated_at=has_rated_at
                    )
                else:
                    # Dry run - use the same logic as migrate_track for accurate stats
                    check_track_for_dry_run(
                        track, path_index, stats, not_found_paths, ambiguous_matches,
                        import_options=import_options
                    )

                # Progress indicator
                if stats['total'] % 500 == 0:
                    logger.info(f"Processed {stats['total']} tracks...")

        # =====================================================================
        # 2. Date added timestamps
        # =====================================================================
        if import_options.import_date_added:
            print("\n--- Importing Date Added ---")
            logger.info("Starting migration of: date added timestamps...")
            date_added_stats = migrate_date_added(
                conn, tracks, path_index, dry_run=args.dry_run
            )

        # =====================================================================
        # 3. Playlists
        # =====================================================================
        if import_options.import_playlists and playlists:
            print("\n--- Importing Playlists ---")
            logger.info("Starting migration of: playlists...")
            playlist_stats = migrate_all_playlists(
                conn, args.user_id, playlists, path_index, dry_run=args.dry_run
            )

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

    # Print and log summary
    log_and_print("\n" + "=" * 80)
    log_and_print("MIGRATION SUMMARY")
    log_and_print("=" * 80)

    # Play counts/ratings/play dates summary
    if import_options.import_play_counts or import_options.import_ratings or import_options.import_play_dates:
        # Build header showing what was imported
        imported_items = []
        if import_options.import_play_counts:
            imported_items.append("Play Counts")
        if import_options.import_ratings:
            imported_items.append("Ratings")
        if import_options.import_play_dates:
            imported_items.append("Play Dates")
        log_and_print(f"\n--- {', '.join(imported_items)} ---")
        log_and_print(f"Total tracks in iTunes:     {stats['total']}")
        log_and_print(f"Successfully matched:       {stats['matched']}")
        log_and_print(f"Not found in Navidrome:     {stats['not_found']}")
        log_and_print(f"No location in iTunes:      {stats['no_location']}")
        log_and_print(f"No data to migrate:         {stats['no_data']}")
        log_and_print(f"Path conversion errors:     {stats['path_error']}")
        log_and_print(f"Ambiguous matches:          {len(ambiguous_matches)}")

        # Calculate match rate based on tracks that actually have data to migrate
        tracks_with_data = stats['total'] - stats['no_location'] - stats['no_data']
        if tracks_with_data > 0:
            match_rate = (stats['matched'] / tracks_with_data) * 100
            log_and_print(f"Match rate (tracks w/data): {match_rate:.1f}%")

    # Date added summary
    if import_options.import_date_added and date_added_stats:
        log_and_print("\n--- Date Added Timestamps ---")
        log_and_print(f"Total tracks processed:     {date_added_stats['total']}")
        log_and_print(f"Timestamps updated:         {date_added_stats['updated']}")
        log_and_print(f"No date in iTunes:          {date_added_stats['no_date']}")
        log_and_print(f"Not found in Navidrome:     {date_added_stats['not_found']}")

    # Playlist summary
    if import_options.import_playlists and playlist_stats:
        log_and_print("\n--- Playlists ---")
        log_and_print(f"Total playlists found:      {playlist_stats['total']}")
        log_and_print(f"Playlists created:          {playlist_stats['created']}")
        log_and_print(f"Skipped (already exists):   {playlist_stats['skipped_exists']}")
        log_and_print(f"Skipped (no tracks found):  {playlist_stats['skipped_no_tracks']}")
        log_and_print(f"Total tracks matched:       {playlist_stats['tracks_matched']}")
        log_and_print(f"Total tracks unmatched:     {playlist_stats['tracks_unmatched']}")

    log_and_print(f"\nLogs saved to: {log_dir}/")

    if len(not_found_paths) > 0:
        log_and_print(f"  - not_found.log ({len(not_found_paths)} unmatched tracks)")

    if len(ambiguous_matches) > 0:
        log_and_print(f"  - ambiguous_matches.log ({len(ambiguous_matches)} tracks with multiple possible matches)")

    if args.dry_run:
        log_and_print("\n*** This was a DRY RUN - no changes were made ***")
        log_and_print("Run without --dry-run to apply changes")
    log_and_print("=" * 80)


if __name__ == '__main__':
    main()
