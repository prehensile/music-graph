#!/usr/bin/env python3

import sqlite3
import os
import glob
import time
from alive_progress import alive_bar
import click

def get_row_count(db_path):
    """Get total number of rows in a database"""
    conn = sqlite3.connect(db_path)
    count = conn.execute("SELECT COUNT(*) FROM releases").fetchone()[0]
    conn.close()
    return count

def merge_databases_streaming(part_dbs, output_db, batch_size=10000):
    """
    Merge multiple SQLite databases using streaming approach to minimize memory usage
    """
    
    # Create output database
    output_conn = sqlite3.connect(output_db)
    output_cursor = output_conn.cursor()
    
    # Create table structure
    output_cursor.execute('''
        CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY,
            content TEXT
        )
    ''')
    output_conn.commit()
    
    # Calculate total rows for progress tracking
    total_rows = sum(get_row_count(db) for db in part_dbs)
    print(f"Merging {len(part_dbs)} databases with {total_rows:,} total rows")
    
    processed_rows = 0
    start_time = time.time()
    
    with alive_bar(total_rows, dual_line=True, receipt_text=True) as bar:
        
        for part_db in part_dbs:
            print(f"Processing {os.path.basename(part_db)}...")
            
            # Open source database
            source_conn = sqlite3.connect(part_db)
            source_conn.row_factory = sqlite3.Row  # Enable dict-like access
            source_cursor = source_conn.cursor()
            
            # Stream data in batches
            offset = 0
            while True:
                # Fetch batch from source
                source_cursor.execute(
                    "SELECT id, content FROM releases LIMIT ? OFFSET ?", 
                    (batch_size, offset)
                )
                batch = source_cursor.fetchall()
                
                if not batch:
                    break
                
                # Insert batch into output database
                batch_data = [(row['id'], row['content']) for row in batch]
                output_cursor.executemany(
                    "INSERT OR REPLACE INTO releases (id, content) VALUES (?, ?)",
                    batch_data
                )
                output_conn.commit()
                
                # Update progress
                batch_count = len(batch)
                processed_rows += batch_count
                offset += batch_size
                
                # Calculate ETA
                elapsed_time = time.time() - start_time
                if processed_rows > 0 and elapsed_time > 0:
                    rows_per_second = processed_rows / elapsed_time
                    remaining_rows = total_rows - processed_rows
                    eta_seconds = remaining_rows / rows_per_second
                    
                    if eta_seconds < 60:
                        eta_str = f"{eta_seconds:.0f}s"
                    elif eta_seconds < 3600:
                        eta_str = f"{eta_seconds//60:.0f}m {eta_seconds%60:.0f}s"
                    else:
                        eta_str = f"{eta_seconds//3600:.0f}h {(eta_seconds%3600)//60:.0f}m"
                    
                    bar.text(f"Processed {processed_rows:,}/{total_rows:,} rows (ETA: {eta_str})")
                else:
                    bar.text(f"Processed {processed_rows:,}/{total_rows:,} rows")
                
                # Update progress bar
                for _ in range(batch_count):
                    bar()
            
            source_conn.close()
    
    # Optimize final database
    print("Optimizing database...")
    output_cursor.execute("VACUUM")
    output_cursor.execute("ANALYZE")
    output_conn.commit()
    output_conn.close()
    
    elapsed_time = time.time() - start_time
    print(f"Merge completed in {elapsed_time:.2f}s")
    print(f"Final database: {output_db}")

def merge_databases_attach(part_dbs, output_db):
    """
    Alternative merge using ATTACH - more memory efficient for schema operations
    but may hit SQLite's attachment limit (default 10)
    """
    
    output_conn = sqlite3.connect(output_db)
    output_cursor = output_conn.cursor()
    
    # Create table structure
    output_cursor.execute('''
        CREATE TABLE IF NOT EXISTS releases (
            id INTEGER PRIMARY KEY,
            content TEXT
        )
    ''')
    
    # Process databases in groups to avoid attachment limit
    max_attachments = 8  # Conservative limit
    
    for i in range(0, len(part_dbs), max_attachments):
        batch_dbs = part_dbs[i:i + max_attachments]
        
        # Attach databases
        for j, db_path in enumerate(batch_dbs):
            output_cursor.execute(f"ATTACH '{db_path}' AS db{j}")
        
        # Insert data from attached databases
        for j in range(len(batch_dbs)):
            print(f"Copying from {os.path.basename(batch_dbs[j])}...")
            output_cursor.execute(f"INSERT OR REPLACE INTO releases SELECT * FROM db{j}.releases")
        
        # Detach databases
        for j in range(len(batch_dbs)):
            output_cursor.execute(f"DETACH db{j}")
        
        output_conn.commit()
    
    # Optimize final database
    print("Optimizing database...")
    output_cursor.execute("VACUUM")
    output_cursor.execute("ANALYZE")
    output_conn.commit()
    output_conn.close()

@click.command()
@click.option('--pattern', required=True, help='Glob pattern for part databases (e.g., "releases_part*.db")')
@click.option('--output', required=True, help='Output merged database path')
@click.option('--method', type=click.Choice(['streaming', 'attach']), default='streaming', 
              help='Merge method: streaming (memory efficient) or attach (faster for small files)')
@click.option('--batch-size', default=10000, help='Batch size for streaming method')
def main(pattern, output, method, batch_size):
    
    # Find all part databases
    part_dbs = sorted(glob.glob(pattern))
    
    if not part_dbs:
        print(f"No databases found matching pattern: {pattern}")
        return
    
    print(f"Found {len(part_dbs)} databases to merge:")
    for db in part_dbs:
        size_mb = os.path.getsize(db) / (1024 * 1024)
        print(f"  {db} ({size_mb:.1f} MB)")
    
    if os.path.exists(output):
        click.confirm(f"Output file {output} exists. Overwrite?", abort=True)
        os.remove(output)
    
    if method == 'streaming':
        merge_databases_streaming(part_dbs, output, batch_size)
    else:
        merge_databases_attach(part_dbs, output)

if __name__ == "__main__":
    main()