#!/usr/bin/env python3
"""
Navidrome Database Inspector
Helps you find the information needed for iTunes migration

Usage:
    python inspect_navidrome.py <navidrome.db>
"""

import sqlite3
import sys
import os


def inspect_database(db_path: str):
    """Inspect a Navidrome database and show useful information."""
    
    if not os.path.exists(db_path):
        print(f"Error: Database not found: {db_path}")
        sys.exit(1)
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("="*80)
    print("NAVIDROME DATABASE INSPECTOR")
    print("="*80)
    
    # Check Navidrome version info (if available)
    try:
        cursor.execute("SELECT * FROM property WHERE id = 'LastScan' OR id LIKE '%version%'")
        props = cursor.fetchall()
        if props:
            print("\n📋 Database Properties:")
            for prop in props:
                print(f"   {prop[0]}: {prop[1]}")
    except:
        pass
    
    # List users
    print("\n👤 Users:")
    cursor.execute("SELECT id, user_name, is_admin FROM user")
    users = cursor.fetchall()
    for user in users:
        admin_flag = " (admin)" if user[2] else ""
        print(f"   ID: {user[0]}")
        print(f"   Username: {user[1]}{admin_flag}")
        print()
    
    # List libraries (for 0.58+)
    try:
        cursor.execute("SELECT id, name, path FROM library")
        libraries = cursor.fetchall()
        if libraries:
            print("📚 Libraries:")
            for lib in libraries:
                print(f"   ID: {lib[0]}, Name: {lib[1]}")
                print(f"   Path: {lib[2]}")
                print()
    except sqlite3.OperationalError:
        print("📚 Libraries: (single library mode - pre 0.58)")
    
    # Count media files
    cursor.execute("SELECT COUNT(*) FROM media_file")
    media_count = cursor.fetchone()[0]
    print(f"🎵 Total media files: {media_count}")
    
    # Sample paths from media_file
    print("\n📁 Sample file paths in Navidrome:")
    cursor.execute("SELECT path FROM media_file LIMIT 5")
    paths = cursor.fetchall()
    for path in paths:
        print(f"   {path[0]}")
    
    # Check existing annotations
    cursor.execute("SELECT COUNT(*) FROM annotation WHERE item_type = 'media_file'")
    annotation_count = cursor.fetchone()[0]
    print(f"\n📊 Existing media_file annotations: {annotation_count}")
    
    cursor.execute("SELECT COUNT(*) FROM annotation WHERE play_count > 0")
    play_count = cursor.fetchone()[0]
    print(f"   With play counts: {play_count}")
    
    cursor.execute("SELECT COUNT(*) FROM annotation WHERE rating > 0")
    rated_count = cursor.fetchone()[0]
    print(f"   With ratings: {rated_count}")
    
    # Show annotation table schema
    print("\n🔧 Annotation table schema:")
    cursor.execute("PRAGMA table_info(annotation)")
    columns = cursor.fetchall()
    for col in columns:
        print(f"   {col[1]} ({col[2]})")
    
    # Show media_file table schema (key columns)
    print("\n🔧 Media file table (key columns):")
    cursor.execute("PRAGMA table_info(media_file)")
    columns = cursor.fetchall()
    key_cols = ['id', 'path', 'title', 'artist', 'album', 'album_id', 'artist_id', 'library_id']
    for col in columns:
        if col[1] in key_cols:
            print(f"   {col[1]} ({col[2]})")
    
    conn.close()
    
    print("\n" + "="*80)
    print("NEXT STEPS:")
    print("="*80)
    print("""
1. Copy the User ID from above (you'll need it for the migration script)

2. Compare the 'Sample file paths' above with your iTunes paths
   - iTunes paths look like: file://localhost/C:/Users/.../Music/Artist/Album/Song.mp3
   - Navidrome paths look like: /mnt/music/Artist/Album/Song.mp3

3. Determine your path mapping:
   - iTunes base:    The common prefix in your iTunes Library.xml
   - Navidrome base: The corresponding prefix in Navidrome

4. Run the migration with --dry-run first:
   python itunes_to_navidrome.py navidrome.db Library.xml <USER_ID> \\
       "<ITUNES_BASE>" "<NAVIDROME_BASE>" --dry-run

5. Check 'not_found.log' and adjust paths if needed

6. Run for real (without --dry-run) once paths are correct
""")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print("Usage: python inspect_navidrome.py <navidrome.db>")
        sys.exit(1)
    
    inspect_database(sys.argv[1])
